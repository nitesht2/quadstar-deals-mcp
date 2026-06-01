"""
agent.py - Deal pipeline tools + deterministic command entrypoint

Holds the 14 _tool functions (scrape, score, schedule, etc.) and run_agent(),
which routes natural-language commands through tool_router.dispatch() — one
classify call, then a direct tool call. No agent framework here: planning and
multi-step reasoning live in the Hermes agent, which drives this backend via
the /webhook/hermes endpoint. Used by the Discord bot buttons and Hermes.

Tools:
  scrape_deals        - Scrape Amazon + aggregator deals for a category
  ingest_deals        - Persist Hermes-extracted structured deals (scrape replacement)
  get_unposted_deals  - Fetch top ranked unposted deals
  generate_and_send_cards - Generate tweet content + send Discord approval cards
  schedule_to_postiz  - Schedule an approved deal to social platforms
  cancel_price_drop   - Cancel a pending price drop repost before the 15-min timer fires
  manage_watchlist    - Add/remove/list ASINs for permanent price monitoring
  read_feedback       - Process Discord reactions/comments into preferences
  get_status          - Pipeline stats: active deals, posted, categories
  add_category        - Register a new product category at runtime
  browse_with_openclaw - Delegate browser tasks to OpenClaw for bot-blocked sites

LLM: DeepSeek Flash (primary) with OpenRouter free-tier fallback, via src/llm.py.
"""

import json
import os
import threading

# bot_loop is set by discord_bot.py after the event loop starts.
# Protected by bot_loop_lock because agent tools (running in scheduler /
# FastAPI executor threads) read it while discord_bot.on_ready() (running
# on the bot's event loop) writes it.
bot_loop = None
bot_loop_lock = threading.Lock()

# ── A/B counter persistence ────────────────────────────────────────────────────
# Persisted across server restarts so the "every 3rd post" cadence survives crashes.
_AB_COUNTER_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ab_counter.json")


def _load_ab_counter() -> int:
    try:
        if os.path.exists(_AB_COUNTER_FILE):
            with open(_AB_COUNTER_FILE) as f:
                return int(json.load(f).get("counter", 0))
    except Exception:
        pass
    return 0


def _save_ab_counter(counter: int) -> None:
    os.makedirs(os.path.dirname(_AB_COUNTER_FILE), exist_ok=True)
    tmp = _AB_COUNTER_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"counter": counter}, f)
    os.replace(tmp, _AB_COUNTER_FILE)


def _get_bot_loop():
    """Thread-safe accessor for bot_loop. Returns None if bot isn't ready yet."""
    with bot_loop_lock:
        return bot_loop


# --- Tools ---

def _scrape_deals(category: str = "tech") -> str:
    """Scrape deals from Amazon + aggregators (Slickdeals/DealNews via Firecrawl)."""
    from src.amazon_scraper import run_amazon_scraper
    amazon_count = run_amazon_scraper(category_name=category)
    agg_count = 0
    try:
        from src.scraper import run_scraper
        agg_count = run_scraper()
    except Exception:
        pass  # Firecrawl key may not be set; graceful fallback
    return f"Scraped {amazon_count} Amazon + {agg_count} aggregator deals ({category})"


def _ingest_deals(deals) -> str:
    """Ingest Hermes-extracted structured deals into the pipeline DB.

    The Phase-3 seam: Hermes owns scraping + vision extraction, then hands
    structured deals to this backend, which owns the mechanics — dedup, price
    math, scoring gates, posting. This REPLACES the backend's own scrape step
    (_scrape_deals): the agent supplies data, code verifies and persists it.

    `deals` may be a JSON string, a single deal dict, or a list of dicts.
    Each deal accepts flexible (Hermes-friendly) field names:
        title                          (required)
        asin                           (required — affiliate URL, dedup, history)
        price / deal_price             (required)
        list_price / original_price    (optional)
        discount / discount_pct        (optional — recomputed when prices exist)
        image_url / image              (recommended — no-image deals get dropped)
        url / source_url               (optional — affiliate URL rebuilt from ASIN)
        rating / star_rating, review_count, category   (optional)

    Discount is ALWAYS computed by code when both prices are present — the
    agent's arithmetic is never trusted (agentic principle: code for mechanics).
    save_deal() then enforces ASIN/URL/fuzzy-title dedup, image quality, the
    tech-keyword filter, and the discount sanity cap. The downstream pipeline's
    live price-verify gate is the final guard against bad agent data.

    Returns a summary: total / saved / duplicate-or-filtered / invalid counts.
    """
    import re as _re
    from src.amazon_scraper import _build_affiliate_url
    from src.database import save_deal

    if isinstance(deals, str):
        try:
            deals = json.loads(deals)
        except (ValueError, TypeError) as exc:
            return f"Ingest failed: deals payload is not valid JSON ({exc})"
    if isinstance(deals, dict):
        deals = [deals]  # tolerate a single-deal object
    if not isinstance(deals, list):
        return "Ingest failed: expected a JSON list of deal objects."

    saved = filtered = invalid = updated = 0

    for raw in deals:
        if not isinstance(raw, dict):
            invalid += 1
            continue

        title = (raw.get("title") or "").strip()
        asin = (raw.get("asin") or "").strip().upper()

        # Required trio: without these a deal can't be scored, deduped, or linked.
        deal_price = raw.get("deal_price", raw.get("price"))
        try:
            deal_price = float(deal_price) if deal_price is not None else None
        except (ValueError, TypeError):
            deal_price = None
        if not title or not asin or not deal_price or deal_price <= 0:
            invalid += 1
            continue

        # Optional list price.
        original_price = raw.get("original_price", raw.get("list_price"))
        try:
            original_price = float(original_price) if original_price is not None else None
        except (ValueError, TypeError):
            original_price = None
        if original_price is not None and original_price <= deal_price:
            original_price = None  # nonsensical "discount" — discard

        # Discount: compute from prices when possible (code does the math, not
        # the agent). Only fall back to an agent-reported discount when there's
        # no list price to compute from.
        if original_price and original_price > deal_price:
            discount_pct = round(((original_price - deal_price) / original_price) * 100, 2)
        else:
            try:
                discount_pct = float(raw.get("discount", raw.get("discount_pct", 0)) or 0)
            except (ValueError, TypeError):
                discount_pct = 0.0

        # Affiliate URL: always rebuilt from ASIN so the affiliate tag is
        # guaranteed present (revenue), regardless of the URL Hermes passes.
        href = raw.get("source_url", raw.get("url", "")) or ""
        source_url, affiliate_url = _build_affiliate_url(href, asin)

        # Image (optional input; save_deal drops deals without a real image).
        image_url = raw.get("image_url", raw.get("image"))
        if image_url and "._" in image_url:
            image_url = _re.sub(r'\._[^.]+\.', '._AC_SL500_.', image_url)

        # Rating / reviews (optional).
        rating_raw = raw.get("star_rating", raw.get("rating"))
        try:
            star_rating = float(rating_raw) if rating_raw is not None else None
        except (ValueError, TypeError):
            star_rating = None
        try:
            review_count = int(raw["review_count"]) if raw.get("review_count") is not None else None
        except (ValueError, TypeError):
            review_count = None

        deal = {
            "title": title[:500],
            "asin": asin,
            "original_price": original_price,
            "deal_price": deal_price,
            "discount_pct": discount_pct,
            "retailer": "Amazon",
            "source_url": source_url,
            "affiliate_url": affiliate_url,
            "image_url": image_url,
            "coupon_code": raw.get("coupon_code"),
            "extra_savings": raw.get("extra_savings"),
            "category": (raw.get("category") or "tech"),
            "star_rating": star_rating,
            "review_count": review_count,
            "source": raw.get("source", "hermes"),
        }

        # Hermes-written copy (optional). When present, the approval card uses
        # THIS copy verbatim instead of regenerating via the backend LLM — so the
        # voice is Hermes's (loaded from ~/.voice/), not the backend notifier's.
        t1 = (raw.get("tweet_1") or raw.get("copy") or "").strip()
        t2 = (raw.get("tweet_2") or "").strip()
        # Hermes-picked posting time (optional): ISO 8601 UTC + a one-line reason.
        sched_at = (raw.get("schedule_at") or "").strip()
        sched_reason = (raw.get("schedule_reason") or "").strip()[:200]

        if t1:
            deal["hermes_tweet_1"] = t1[:280]
            deal["hermes_tweet_2"] = t2[:280]
            deal["hermes_linkedin"] = (raw.get("linkedin_post") or "").strip()
            deal["copy_source"] = "hermes"
        if sched_at:
            deal["schedule_at"] = sched_at
            deal["schedule_reason"] = sched_reason

        # Lean-split bridge: if the backend already scraped this ASIN, attach
        # Hermes's copy + chosen time to that existing un-posted deal (don't dup-filter).
        if (t1 or sched_at) and asin:
            from src.database import _load_deals, update_deal
            existing = next(
                (d for d in _load_deals()
                 if d.get("asin") == asin and d.get("is_active") and not d.get("is_posted")),
                None,
            )
            if existing:
                patch = {}
                if t1:
                    patch.update({
                        "hermes_tweet_1": deal["hermes_tweet_1"],
                        "hermes_tweet_2": deal["hermes_tweet_2"],
                        "hermes_linkedin": deal["hermes_linkedin"],
                        "copy_source": "hermes",
                    })
                if sched_at:
                    patch["schedule_at"] = sched_at
                    patch["schedule_reason"] = sched_reason
                update_deal(existing["id"], patch)
                updated += 1
                continue

        try:
            if save_deal(deal):
                saved += 1
            else:
                filtered += 1
        except Exception as exc:
            invalid += 1
            print(f"  [ingest] save_deal error for '{title[:40]}': {exc}")

    return (f"Ingested {len(deals)} deal(s): {saved} saved, {updated} copy-updated, "
            f"{filtered} duplicate/filtered, {invalid} invalid.")


def _generate_and_send_cards(limit: int = 5) -> str:
    """Generate tweet content for top unposted deals and send Discord approval cards."""
    import asyncio
    from src.database import get_top_unposted_deals
    from src.notifier import generate_deal_content
    from src.discord_bot import send_deal_card

    deals = get_top_unposted_deals(limit=limit)
    if not deals:
        return "No unposted deals available."

    loop = _get_bot_loop()
    if not loop:
        return "Discord bot not ready — cards cannot be sent right now."

    sent = 0
    for deal in deals:
        try:
            # Prefer Hermes-written copy (its own voice); fall back to backend LLM.
            if deal.get("hermes_tweet_1"):
                content = {
                    "tweet_1": deal["hermes_tweet_1"],
                    "tweet_2": deal.get("hermes_tweet_2", ""),
                    "linkedin_post": deal.get("hermes_linkedin", ""),
                    "confidence": 1.0,
                }
            else:
                content = generate_deal_content(deal)
            asyncio.run_coroutine_threadsafe(send_deal_card(deal, content), loop)
            sent += 1
        except Exception as e:
            print(f"  [agent] card send failed for deal {deal.get('id')}: {e}")

    return f"Sent {sent} Discord approval card(s)."


def _get_unposted_deals(limit: int = 5) -> str:
    """Get top ranked unposted deals ready for review. Returns JSON list."""
    from src.database import get_top_unposted_deals
    deals = get_top_unposted_deals(limit=limit)
    return json.dumps([
        {
            "id": d["id"],
            "asin": d.get("asin", ""),
            "title": d["title"][:120],
            "deal_price": d.get("deal_price"),
            "original_price": d.get("original_price"),
            "discount_pct": d.get("discount_pct", 0),
            "star_rating": d.get("star_rating"),
            "category": d.get("category", "tech"),
        }
        for d in deals
    ])


def _get_candidate_deals(limit: int = 10) -> str:
    """Agentic primitive: the scored MENU of unposted deals — NO gating.

    Unlike get_unposted_deals (which only sorts), this returns each deal's score,
    the lowest-ever flag, and the deterministic pipeline's *soft* eligibility
    verdict (discount/score/cooldown) WITHOUT dropping anything. The agent reads
    this and decides which to post — it may override eligibility with judgment
    (e.g. a thin-discount premium deal). The hard cage runs later in schedule_deal.
    """
    from src.database import (
        get_top_unposted_deals, score_deal, get_watchlist_asins, _safe_load_json,
    )
    from src import guards
    from config.settings import ASIN_REPOST_COOLDOWN_DAYS
    import os

    deals = get_top_unposted_deals(limit=limit, min_discount=0.0)
    if not deals:
        return json.dumps([])

    perf = _safe_load_json(os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "tweet_performance.json"), [])
    ctx = guards._ctx_from_settings(
        recent_asins=set(get_watchlist_asins(days=ASIN_REPOST_COOLDOWN_DAYS)))

    out = []
    for d in deals:
        elig = guards.eligibility(d, ctx, _perf_records=perf)
        out.append({
            "id": d["id"],
            "asin": d.get("asin", ""),
            "title": d["title"][:120],
            "deal_price": d.get("deal_price"),
            "original_price": d.get("original_price"),
            "discount_pct": d.get("discount_pct", 0),
            "score": round(score_deal(d, _perf_records=perf), 1),
            "is_lowest_ever": bool(d.get("is_lowest_ever")),
            "star_rating": d.get("star_rating"),
            "category": d.get("category", "tech"),
            "eligible": elig.ok,
            "eligibility_note": elig.reason,
        })
    return json.dumps(out)


def _schedule_deal(deal_id: int, platforms: str = "", scheduled_at: str = "",
                   copy_json: str = "") -> str:
    """Agentic primitive: post ONE agent-chosen deal — through the hard cage.

    The agent decides WHICH deal, WHEN (scheduled_at ISO, or smart default), the
    PLATFORMS, and may supply its own voice COPY (copy_json = {"tweet_1","tweet_2",
    "linkedin_post"}). This function does NOT trust that judgment blindly: it runs
    guards.enforce_guards() server-side first (dedup, affiliate tag, daily +
    per-category caps, content confidence, LIVE price re-verify). On any violation
    it REFUSES and returns the machine code + reason so the agent can learn and
    pick a different deal. The agent cannot bypass the cage.
    """
    from src.database import (
        get_deal_by_id, mark_as_posted, get_posts_today_count,
        get_category_posts_today, get_watchlist_asins,
    )
    from src.notifier import generate_deal_content
    from src.platform_router import select_platforms
    from src.postiz_client import get_smart_time
    from src import postiz_client, guards
    from config.settings import ASIN_REPOST_COOLDOWN_DAYS

    deal = get_deal_by_id(deal_id)
    if not deal:
        return json.dumps({"ok": False, "code": "not_found", "reason": f"deal {deal_id} not found"})

    # Agent's own voice copy if supplied, else backend LLM.
    content = None
    if copy_json:
        try:
            c = json.loads(copy_json) if isinstance(copy_json, str) else copy_json
            if c.get("tweet_1"):
                content = {"tweet_1": c["tweet_1"], "tweet_2": c.get("tweet_2", ""),
                           "linkedin_post": c.get("linkedin_post", ""), "confidence": 1.0}
        except (ValueError, TypeError):
            content = None
    if content is None:
        content = generate_deal_content(deal)

    # Build the cage context from live state.
    ctx = guards._ctx_from_settings(
        posts_today=get_posts_today_count(),
        cat_counts=get_category_posts_today(),
        recent_asins=set(get_watchlist_asins(days=ASIN_REPOST_COOLDOWN_DAYS)),
    )

    verdict = guards.enforce_guards(deal, content, ctx)
    if not verdict.ok:
        return json.dumps({"ok": False, "code": verdict.code, "reason": verdict.reason,
                           "deal_id": deal_id, "title": deal.get("title", "")[:60]})

    # Cage passed — the agent's decision is allowed. Post it.
    platform_list = [p.strip() for p in platforms.split(",") if p.strip()] or select_platforms(deal)
    sched = scheduled_at.strip() or get_smart_time()[0]
    result = postiz_client.schedule_post(deal, content, platform_list, scheduled_at=sched)
    if result.get("status") != "ok":
        return json.dumps({"ok": False, "code": "postiz_failed",
                           "reason": result.get("reason", "schedule failed"), "deal_id": deal_id})

    mark_as_posted(deal_id)
    from src.tweet_learner import record_tweet
    record_tweet(deal_id, content["tweet_1"], postiz_client.extract_postiz_id(result), sched)

    # Best-effort Discord card (never blocks the post).
    loop = _get_bot_loop()
    if loop:
        import asyncio
        from src.discord_bot import send_auto_approved_notification
        try:
            asyncio.run_coroutine_threadsafe(
                send_auto_approved_notification(deal, content, sched, platform_list), loop)
        except Exception as exc:
            print(f"  [schedule_deal] card send failed: {exc}")

    return json.dumps({"ok": True, "code": "scheduled", "deal_id": deal_id,
                       "title": deal.get("title", "")[:60], "platforms": platform_list,
                       "scheduled_at": sched})


def _run_pipeline(limit: int = 10) -> str:
    """Unified deal pipeline. Scrape → score → auto-post if gates pass → silent skip otherwise.

    No Discord approval cards. No human involvement. A deal posts only if it
    clears every gate: discount >= PIPELINE_MIN_DISCOUNT, score >=
    PIPELINE_MIN_SCORE, not in ASIN cooldown, under the per-category daily cap,
    content confidence >= PIPELINE_MIN_CONFIDENCE, and live price verification.
    Daily and per-category caps bound total volume.
    """
    import asyncio
    from src.database import get_top_unposted_deals, cleanup_deals, mark_as_posted, update_deal
    from src.notifier import generate_deal_content
    from src.discord_bot import send_auto_approved_notification, reset_batch_times
    from src.database import score_deal
    from src.platform_router import select_platforms
    from src.postiz_client import get_smart_time
    from src import postiz_client
    from config.settings import (
        PIPELINE_MIN_DISCOUNT, PIPELINE_MIN_SCORE, PIPELINE_MIN_CONFIDENCE,
        ASIN_REPOST_COOLDOWN_DAYS, PIPELINE_MAX_DAILY_POSTS,
        PIPELINE_MAX_PER_CATEGORY_PER_DAY,
    )
    from src.database import (
        get_watchlist_asins, get_posts_today_count, get_category_posts_today,
    )

    stats = cleanup_deals()
    if stats.get("expired", 0) > 0:
        print(f"  Expired {stats['expired']} stale deals")

    # Daily cap check — deals are already sorted best-score-first by get_top_unposted_deals
    if PIPELINE_MAX_DAILY_POSTS > 0:
        posts_today = get_posts_today_count()
        if posts_today >= PIPELINE_MAX_DAILY_POSTS:
            print(f"  [pipeline] Daily cap reached ({posts_today}/{PIPELINE_MAX_DAILY_POSTS}) — skipping run")
            return ""

    deals = get_top_unposted_deals(limit=limit)
    if not deals:
        return ""  # Nothing to do — stay silent

    reset_batch_times()
    recent_asins = set(get_watchlist_asins(days=ASIN_REPOST_COOLDOWN_DAYS))
    # Per-category daily counts — seeded from today's posts, bumped as we post
    # this run, so the category cap holds across both restarts and within a run.
    cat_counts = get_category_posts_today()

    posted = 0
    skipped = 0
    _ab_counter = _load_ab_counter()  # Persisted — survives server restarts

    for deal in deals:
        discount = deal.get("discount_pct") or 0

        # Gate 0: must have a discount
        if discount < 1:
            skipped += 1
            continue

        # Gate 1: discount threshold
        if discount < PIPELINE_MIN_DISCOUNT:
            skipped += 1
            continue

        # Gate 2: deal score
        deal_score = score_deal(deal)
        if deal_score < PIPELINE_MIN_SCORE:
            skipped += 1
            continue

        # Gate 3: ASIN cooldown — same product posted recently, skip silently
        deal_asin = deal.get("asin", "")
        if deal_asin and deal_asin in recent_asins:
            print(f"  [pipeline] ASIN cooldown skip: {deal['title'][:50]}")
            skipped += 1
            continue

        # Gate 3.5: per-category daily cap — keep the feed varied
        deal_cat = deal.get("category") or "tech"
        if PIPELINE_MAX_PER_CATEGORY_PER_DAY > 0 and \
                cat_counts.get(deal_cat, 0) >= PIPELINE_MAX_PER_CATEGORY_PER_DAY:
            print(f"  [pipeline] Category cap skip ({deal_cat}): {deal['title'][:50]}")
            skipped += 1
            continue

        # All score gates passed — generate content
        content = generate_deal_content(deal)
        content_conf = content.get("confidence", 1.0)

        # Gate 4: content quality
        if content_conf < PIPELINE_MIN_CONFIDENCE:
            skipped += 1
            print(f"  [pipeline] Low confidence skip ({content_conf:.2f}): {deal['title'][:50]}")
            continue

        # Gate 5: live price verification against Amazon
        # Catches scraper parse errors, product variations with wrong ASIN,
        # and stale DB prices. Fails open on network errors (won't block posts).
        from src.price_verifier import verify_deal_price
        is_valid, reason = verify_deal_price(deal)
        if not is_valid:
            skipped += 1
            print(f"  [pipeline] Price verify skip: {deal['title'][:50]} — {reason}")
            # Track consecutive failures — purge after 3 strikes to clear stuck deals
            failures = deal.get("verify_failures", 0) + 1
            if failures >= 3:
                mark_as_posted(deal["id"])
                print(f"  [pipeline] Purged after {failures} verify failures: {deal['title'][:50]}")
            else:
                update_deal(deal["id"], {"verify_failures": failures})
            continue
        print(f"  [pipeline] Price verified: {reason}")
        if deal.get("verify_failures"):
            update_deal(deal["id"], {"verify_failures": 0})

        # All gates passed — schedule to Postiz
        # Every 5th post runs as A/B test (two variants, different times)
        platforms = select_platforms(deal)
        _ab_counter += 1
        _save_ab_counter(_ab_counter)  # persist immediately — survives crash mid-run
        use_ab = (_ab_counter % 5 == 0)

        if use_ab:
            ab_result = _schedule_to_postiz(deal["id"], ",".join(platforms) if isinstance(platforms, list) else platforms, ab_test=True)
            print(f"  [pipeline] A/B test: {ab_result}")
            posted += 1
            cat_counts[deal_cat] = cat_counts.get(deal_cat, 0) + 1
            if PIPELINE_MAX_DAILY_POSTS > 0 and (posts_today + posted) >= PIPELINE_MAX_DAILY_POSTS:
                print(f"  [pipeline] Daily cap reached ({posts_today + posted}/{PIPELINE_MAX_DAILY_POSTS}) — stopping run")
                break
            loop = _get_bot_loop()
            if loop:
                def _on_card_done(fut, title=deal["title"][:40]):
                    exc = fut.exception()
                    if exc:
                        print(f"  [pipeline] Discord card failed for '{title}': {exc}", flush=True)
                    else:
                        print(f"  [pipeline] Discord card sent for '{title}'", flush=True)
                fut = asyncio.run_coroutine_threadsafe(
                    send_auto_approved_notification(deal, content, "A/B test", platforms),
                    loop,
                )
                fut.add_done_callback(_on_card_done)
            continue

        scheduled_time, scheduled_label = get_smart_time()
        result = postiz_client.schedule_post(deal, content, platforms, scheduled_at=scheduled_time)

        if result.get("status") == "ok":
            mark_as_posted(deal["id"])
            from src.tweet_learner import record_tweet
            postiz_id = postiz_client.extract_postiz_id(result)
            record_tweet(deal["id"], content["tweet_1"], postiz_id, scheduled_time)
            posted += 1
            cat_counts[deal_cat] = cat_counts.get(deal_cat, 0) + 1
            print(f"  [pipeline] Posted: {deal['title'][:50]} ({discount:.0f}% off, score={deal_score:.0f}, conf={content_conf:.2f})")
            # Stop if daily cap reached mid-run
            if PIPELINE_MAX_DAILY_POSTS > 0 and (posts_today + posted) >= PIPELINE_MAX_DAILY_POSTS:
                print(f"  [pipeline] Daily cap reached ({posts_today + posted}/{PIPELINE_MAX_DAILY_POSTS}) — stopping run")
                break
            # Discord auto-post card
            loop = _get_bot_loop()
            if loop:
                def _on_card_done(fut, title=deal["title"][:40]):
                    exc = fut.exception()
                    if exc:
                        print(f"  [pipeline] Discord card failed for '{title}': {exc}", flush=True)
                    else:
                        print(f"  [pipeline] Discord card sent for '{title}'", flush=True)
                fut = asyncio.run_coroutine_threadsafe(
                    send_auto_approved_notification(deal, content, scheduled_label, platforms),
                    loop,
                )
                fut.add_done_callback(_on_card_done)
            else:
                print(f"  [pipeline] Discord card skipped (bot loop not ready)", flush=True)
        else:
            skipped += 1
            print(f"  [pipeline] Postiz schedule failed for: {deal['title'][:50]}")

    if posted == 0:
        return ""  # Nothing posted — stay silent
    return f"Pipeline: {posted} deal(s) scheduled to Postiz. {skipped} skipped."


def _schedule_to_postiz(deal_id: int, platforms: str = "", ab_test: bool = False) -> str:
    """Schedule an approved deal to social platforms via Postiz.
    If ab_test=True, generates two variants and posts both at different times.
    """
    from src.database import get_deal_by_id, mark_as_posted
    from src.notifier import generate_deal_content, generate_ab_variants
    from src.platform_router import select_platforms
    from src.tweet_learner import record_tweet
    from src import postiz_client

    deal = get_deal_by_id(deal_id)
    if not deal:
        return f"Deal {deal_id} not found"

    if platforms:
        platform_list = [p.strip() for p in platforms.split(",") if p.strip()]
    else:
        platform_list = select_platforms(deal)

    if ab_test:
        # A/B test: generate two variants, post at different times
        variant_a, variant_b = generate_ab_variants(deal)
        from src.postiz_client import get_smart_time
        from src.ab_testing import save_ab_test

        time_a, label_a = get_smart_time()
        result_a = postiz_client.schedule_post(deal, variant_a, platform_list, scheduled_at=time_a)
        postiz_id_a = postiz_client.extract_postiz_id(result_a)

        time_b, label_b = get_smart_time()
        result_b = postiz_client.schedule_post(deal, variant_b, platform_list, scheduled_at=time_b)
        postiz_id_b = postiz_client.extract_postiz_id(result_b)

        save_ab_test(deal_id, variant_a, variant_b, time_a, time_b, postiz_id_a, postiz_id_b)

        # Record both for self-learning
        record_tweet(deal_id, variant_a["tweet_1"], postiz_id_a, time_a)
        record_tweet(deal_id, variant_b["tweet_1"], postiz_id_b, time_b)

        mark_as_posted(deal_id)
        return f"A/B test scheduled for deal {deal_id}: variant A at {label_a}, variant B at {label_b}"

    # Standard single-variant post
    content = generate_deal_content(deal)
    result = postiz_client.schedule_post(deal, content, platform_list)
    postiz_id = postiz_client.extract_postiz_id(result)

    # Record for self-learning
    record_tweet(deal_id, content["tweet_1"], postiz_id)

    mark_as_posted(deal_id)
    return f"Scheduled deal {deal_id} to {','.join(platform_list)}: {result.get('status', 'ok')}"


def _manage_watchlist(action: str, asin: str = "", title: str = "") -> str:
    """Add, remove, or list manually-pinned ASINs for price monitoring.

    action: 'add' | 'remove' | 'list'
    asin:   Amazon ASIN (e.g. B08XYZ1234). Required for add/remove.
    title:  Human-readable product name for add (optional, improves readability).
    """
    from src.price_monitor import add_to_manual_watchlist, remove_from_manual_watchlist, list_manual_watchlist
    action = action.strip().lower()
    if action == "list":
        return list_manual_watchlist()
    if action == "add":
        if not asin:
            return "Provide an ASIN to add. Example: action='add', asin='B08XYZ1234', title='Sony WH-1000XM5'"
        return add_to_manual_watchlist(asin.strip(), title.strip())
    if action == "remove":
        if not asin:
            return "Provide an ASIN to remove."
        return remove_from_manual_watchlist(asin.strip())
    return f"Unknown action '{action}'. Use 'add', 'remove', or 'list'."


def _cancel_price_drop(deal_id: int = 0) -> str:
    """Cancel a pending price drop repost by deal_id before the 15-min timer fires."""
    from src.database import get_pending_reposts, remove_pending_repost
    pending_list = get_pending_reposts()
    if not pending_list:
        return "No pending price drop reposts at the moment."
    match = next((p for p in pending_list if p.get("deal_id") == deal_id), None)
    if not match:
        # Friendly list of what IS pending so the user can try the right id
        titles = [
            f"deal {p.get('deal_id')} — {p.get('deal', {}).get('title', p.get('asin', '?'))[:50]}"
            for p in pending_list
        ]
        return f"No pending repost found for deal {deal_id}. Pending: {'; '.join(titles)}"
    remove_pending_repost(match["asin"])
    title = match.get("deal", {}).get("title", match.get("asin", "?"))[:60]
    return f"Cancelled price drop repost for: {title}"


def _browse_with_openclaw(url: str, instruction: str = "") -> str:
    """Ask OpenClaw to browse a URL as a real user and return scraped content.

    Use for sites that block Scrapling/Playwright: Woot, BestBuy, Reddit r/deals,
    eBay Deals, Newegg, Slickdeals direct. OpenClaw navigates as a real user so
    bot-detection doesn't kick in.
    """
    from src.openclaw_client import browse, is_configured
    if not is_configured():
        return "OpenClaw not configured — set OPENCLAW_WEBHOOK_URL in .env to enable."
    if not url:
        return "Provide a URL to browse, e.g. 'browse https://www.woot.com/deals'."
    content = browse(url, instruction)
    if not content:
        return f"OpenClaw returned no content for {url}. Verify OpenClaw is running."
    return f"OpenClaw scraped {len(content)} chars from {url}:\n{content[:3000]}"


def _read_feedback() -> str:
    """Read Discord reactions/comments and update deal preferences."""
    from src.feedback import collect_feedback
    from src.database import _load_deals
    deals = _load_deals()
    posted = [d for d in deals if d.get("is_posted") and d.get("discord_message_id")]
    count = collect_feedback(posted)
    return f"Processed {count} feedback signals"


def _get_status() -> str:
    """Get pipeline stats: active deals, posted, scheduled, categories."""
    from src.database import cleanup_deals
    from config.categories import list_categories
    stats = cleanup_deals()
    stats["categories"] = list_categories()
    return json.dumps(stats)


def _analyze_tweet_performance() -> str:
    """Collect engagement data and return tweet performance report."""
    from src.tweet_learner import collect_engagement, get_performance_report
    updated = collect_engagement()
    report = get_performance_report()
    if updated:
        return f"Updated {updated} engagement records.\n\n{report}"
    return report


def _check_ab_results() -> str:
    """Check engagement on A/B test variants and return summary."""
    from src.ab_testing import check_engagement, get_ab_summary
    updated = check_engagement()
    summary = get_ab_summary()
    if updated:
        return f"Updated {updated} engagement records.\n\n{summary}"
    return summary


def _check_price_drops() -> str:
    """Check watchlist ASINs for price drops, trigger auto-reposts if eligible."""
    from src.price_monitor import detect_drops
    from config.settings import MIN_REPOST_DROP_PCT
    drops = detect_drops()
    if not drops:
        return "No price drops detected across monitored ASINs"
    lines = [f"Found {len(drops)} price drop(s):"]
    for d in drops:
        repost_note = ""
        if d["drop_pct"] >= MIN_REPOST_DROP_PCT:
            from src.database import can_repost
            eligible, reason = can_repost(d["asin"], d["new_price"])
            repost_note = " [REPOST QUEUED]" if eligible else f" [skip: {reason}]"
        lines.append(f"  {d['title'][:50]}: ${d['old_price']} -> ${d['new_price']} (-{d['drop_pct']}%){repost_note}")
    return "\n".join(lines)


def _add_category(name: str, keywords: str) -> str:
    """Add a new product category. keywords = comma-separated list."""
    import json, os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "custom_categories.json")
    cats = {}
    if os.path.exists(path):
        with open(path) as f:
            cats = json.load(f)
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    cats[name.lower()] = {
        "keywords": kw_list,
        "amazon_urls": [],
        "min_price": 25,
        "max_discount": 90,
    }
    with open(path, "w") as f:
        json.dump(cats, f, indent=2)
    return f"Added category '{name}' with {len(kw_list)} keywords"


def _get_posting_insights() -> str:
    """Posting-time insights for smart scheduling (JSON).

    Gives the agent what it needs to choose a post time that maximizes reach
    without collisions:
      - engagement_by_pst_hour: avg engagement per PST hour from YOUR past posts
        (post when your audience actually engaged). Empty until enough data.
      - default_peak_pst_hours: fallback best hours when there's little data.
      - already_booked_utc_slots: times already scheduled in Postiz — DO NOT reuse.
      - active_hours_pst: only post inside this window.
    """
    from src.database import get_engagement_by_hour
    try:
        from src.postiz_client import _get_scheduled_times
        booked = sorted(_get_scheduled_times())
    except Exception:
        booked = []
    return json.dumps({
        "engagement_by_pst_hour": get_engagement_by_hour(),
        "default_peak_pst_hours": [5, 8, 12, 17],
        "already_booked_utc_slots": booked,
        "active_hours_pst": "4:30am-7pm",
        "note": "Choose a PST time in active hours, favor a high-engagement hour, avoid already_booked. Return schedule_at as ISO 8601 UTC (e.g. 2026-06-01T23:30:00Z) on each deal you ingest, plus a one-line schedule_reason.",
    })


def run_agent(command: str, thread_id: str = "default") -> str:
    """Execute a natural-language command via the deterministic tool router.

    All natural-language commands route through tool_router.dispatch():
    one LLM call (DeepSeek, with keyword fallback) classifies intent, then a
    direct call to the matching _tool function. No ReAct loop — the planning /
    multi-step reasoning lives in the Hermes agent, which calls this backend.

    thread_id is accepted for call-site compatibility but unused (dispatch is
    stateless; conversational memory lives in Hermes).

    Result is pushed to OpenClaw/Hermes as a notification (fire-and-forget).
    """
    try:
        from src.tool_router import dispatch
        result = dispatch(command)
    except Exception as exc:
        result = f"Dispatch error: {exc}"
        print(f"  [agent] tool_router error: {exc}")

    # Push to OpenClaw/Hermes (fire-and-forget — never blocks response)
    try:
        from src.openclaw_client import notify, is_configured
        if is_configured() and result:
            notify(result[:1500], title="QuadStar")
    except Exception:
        pass

    return result
