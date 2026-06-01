"""guards.py — the deterministic safety cage around every deal post.

Agentic principle: the agent (Hermes) decides WHAT to post, WHEN, and the COPY;
this module decides whether that's ALLOWED. Every path that can post a deal —
the deterministic `_run_pipeline` and the agentic `schedule_deal` primitive —
MUST route the deal through `enforce_guards()` first. The agent cannot bypass
these, because they run server-side in code, not in the agent's reasoning.

Two tiers, deliberately separate:

  eligibility(deal, ctx)      SOFT signals — discount %, deal score, ASIN
                              cooldown. These are the deterministic pipeline's
                              opinion of "worth posting". The AGENT may override
                              them with judgment (e.g. a thin-discount premium
                              deal). Informational for the agentic path.

  enforce_guards(deal, ctx)   HARD invariants — must hold no matter who decided
                              to post: not already posted, affiliate tag present
                              (revenue), daily cap, per-category cap, content
                              present + confident, and a LIVE Amazon price
                              re-verify. Non-negotiable. The cage.

Both return a GuardResult(ok, code, reason) so callers can apply policy per
failure code (e.g. the pipeline tracks consecutive price-verify failures).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GuardContext:
    """Per-run state + thresholds the guards read. Built once per pipeline/agent
    run and mutated as posts land (posts_today / cat_counts increment)."""
    posts_today: int = 0
    cat_counts: dict[str, int] = field(default_factory=dict)
    recent_asins: set[str] = field(default_factory=set)
    # thresholds (snapshotted from config.settings at construction)
    min_discount: float = 0.0
    min_score: float = 0.0
    min_confidence: float = 0.0
    max_daily: int = 0
    max_per_category: int = 0


@dataclass
class GuardResult:
    ok: bool
    code: str       # machine-readable: "ok" | "already_posted" | "no_affiliate_tag" | ...
    reason: str     # human-readable, safe to log / show the agent


def _ctx_from_settings(**overrides) -> GuardContext:
    """Build a GuardContext with thresholds pulled from config.settings.

    min_score uses the ADAPTIVE gate (database.current_score_gate) so the
    eligibility bar self-tunes with data maturity rather than a fixed number.
    """
    from config.settings import (
        PIPELINE_MIN_DISCOUNT, PIPELINE_MIN_CONFIDENCE,
        PIPELINE_MAX_DAILY_POSTS, PIPELINE_MAX_PER_CATEGORY_PER_DAY,
    )
    from src.database import current_score_gate
    ctx = GuardContext(
        min_discount=PIPELINE_MIN_DISCOUNT,
        min_score=current_score_gate(),
        min_confidence=PIPELINE_MIN_CONFIDENCE,
        max_daily=PIPELINE_MAX_DAILY_POSTS,
        max_per_category=PIPELINE_MAX_PER_CATEGORY_PER_DAY,
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


def affiliate_tag_present(deal: dict) -> bool:
    """True if the affiliate URL carries our Amazon Associates tag.

    This is the revenue invariant — a post without the tag earns $0, so it must
    never reach a live channel regardless of how good the deal is.
    """
    from config.settings import AMAZON_AFFILIATE_TAG
    tag = (AMAZON_AFFILIATE_TAG or "").strip()
    if not tag:
        return True  # no tag configured — nothing to enforce
    url = (deal.get("affiliate_url") or "")
    return f"tag={tag}" in url


def eligibility(deal: dict, ctx: GuardContext, _perf_records=None) -> GuardResult:
    """SOFT signals — the deterministic pipeline's 'is this worth posting'.

    The agent may override these (that's the point of judgment). Returns the
    first failing signal, or ok=True with the computed score in the reason.
    """
    from src.database import score_deal

    discount = deal.get("discount_pct") or 0
    if discount < 1:
        return GuardResult(False, "no_discount", "no discount on deal")
    if discount < ctx.min_discount:
        return GuardResult(False, "below_min_discount",
                           f"discount {discount:.0f}% < floor {ctx.min_discount:.0f}%")

    deal_score = score_deal(deal, _perf_records=_perf_records)
    if deal_score < ctx.min_score:
        return GuardResult(False, "below_min_score",
                           f"score {deal_score:.0f} < gate {ctx.min_score:.0f}")

    asin = deal.get("asin", "")
    if asin and asin in ctx.recent_asins:
        return GuardResult(False, "asin_cooldown", "ASIN posted within cooldown window")

    return GuardResult(True, "ok", f"eligible (score {deal_score:.0f}, {discount:.0f}% off)")


def enforce_guards(deal: dict, content: dict | None, ctx: GuardContext,
                   verify_price: bool = True, check_caps: bool = True) -> GuardResult:
    """HARD invariants — the cage. Run before ANY post, by ANY caller.

    Order is cheap-checks-first so we only pay the network price-verify when
    everything else already passed. Returns the first violation.

    check_caps=False for the PROPOSE path (human-approval): proposing more cards
    than the daily cap is fine — the human approves at most `max_daily` of them,
    and the caps are re-enforced at approve/schedule time. Caps gate POSTS, not
    proposals.
    """
    # 1. Idempotency — never repost the same deal.
    if deal.get("is_posted"):
        return GuardResult(False, "already_posted", "deal already marked posted")

    # 2. Revenue — affiliate tag must be present.
    if not affiliate_tag_present(deal):
        return GuardResult(False, "no_affiliate_tag", "affiliate tag missing from URL")

    # 3+4. Volume caps — only at post time, not propose time.
    if check_caps:
        if ctx.max_daily > 0 and ctx.posts_today >= ctx.max_daily:
            return GuardResult(False, "daily_cap",
                               f"daily cap reached ({ctx.posts_today}/{ctx.max_daily})")
        cat = deal.get("category") or "tech"
        if ctx.max_per_category > 0 and ctx.cat_counts.get(cat, 0) >= ctx.max_per_category:
            return GuardResult(False, "category_cap",
                               f"category '{cat}' cap reached ({ctx.cat_counts.get(cat,0)}/{ctx.max_per_category})")

    # 5. Content present + confident.
    if content is not None:
        if not content.get("tweet_1"):
            return GuardResult(False, "no_content", "no tweet_1 content generated")
        conf = content.get("confidence", 1.0)
        if conf < ctx.min_confidence:
            return GuardResult(False, "low_confidence",
                               f"content confidence {conf:.2f} < {ctx.min_confidence:.2f}")

    # 6. LIVE price re-verify against Amazon (the expensive guard, last).
    #    Fails OPEN on network error inside verify_deal_price (won't block posts);
    #    only a confirmed mismatch returns ok=False here.
    if verify_price:
        from src.price_verifier import verify_deal_price
        is_valid, reason = verify_deal_price(deal)
        if not is_valid:
            return GuardResult(False, "price_unverified", f"price verify failed — {reason}")

    return GuardResult(True, "ok", "all guards passed")
