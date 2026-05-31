"""
tweet_learner.py - Self-Learning Tweet Style

Tracks tweet performance and generates style insights for the LLM prompt.
Over time, the system learns which hook types, CTAs, and formats get
the most engagement and biases generation toward winning styles.

Data sources:
  - ab_testing.py: A/B test results with per-variant engagement
  - Postiz API: engagement metrics for all posted tweets

Storage: data/tweet_performance.json
"""

import json
import os
from datetime import datetime, timedelta, timezone

from src.ab_testing import classify_style, _fetch_post_engagement

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
PERF_FILE = os.path.join(DATA_DIR, "tweet_performance.json")


def _load_performance() -> list:
    if os.path.exists(PERF_FILE):
        with open(PERF_FILE, "r") as f:
            return json.load(f)
    return []


def _save_performance(data: list):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PERF_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def record_tweet(deal_id: int, tweet_text: str, postiz_id: str = "", posted_at: str = "", source: str = ""):
    """Record a posted tweet for performance tracking."""
    data = _load_performance()
    style = classify_style(tweet_text)
    data.append({
        "deal_id": deal_id,
        "style": style,
        "postiz_id": postiz_id,
        "posted_at": posted_at or datetime.now().isoformat(),
        "engagement": None,
        "source": source,  # which deal site this came from
    })
    # Keep last 200 records
    data = data[-200:]
    _save_performance(data)


def collect_engagement() -> int:
    """Fetch engagement for tweets that don't have it yet. Returns count updated."""
    data = _load_performance()
    updated = 0
    for record in data:
        if record.get("engagement") is None and record.get("postiz_id"):
            # postiz_id can be a string or a list of {postId, integration} dicts
            raw_id = record["postiz_id"]
            if isinstance(raw_id, list):
                postiz_id = raw_id[0].get("postId") if raw_id else None
            else:
                postiz_id = raw_id
            if not postiz_id:
                continue
            eng = _fetch_post_engagement(postiz_id)
            if eng:
                record["engagement"] = eng
                updated += 1
                # Feed engagement back to source tracker
                source = record.get("source", "")
                if source:
                    try:
                        from src.source_tracker import record_engagement
                        total = eng.get("likes", 0) + eng.get("retweets", 0) + eng.get("replies", 0)
                        record_engagement(source, total)
                    except Exception:
                        pass
    if updated:
        _save_performance(data)
    return updated


def get_style_insights() -> str:
    """Generate a style brief for the LLM prompt based on performance data.

    Returns a human-readable summary of which styles perform best,
    or empty string if not enough data.
    """
    data = _load_performance()

    # Also pull in A/B test data for richer signal
    from src.ab_testing import get_winning_styles
    ab_winners = get_winning_styles()

    # Aggregate from tweet_performance.json
    style_scores: dict[str, dict[str, list[float]]] = {}
    for record in data:
        eng = record.get("engagement")
        if not eng:
            continue
        style = record["style"]
        score = eng.get("likes", 0) + eng.get("retweets", 0) * 2 + eng.get("replies", 0) * 3
        for attr in ("hook_type", "cta_style"):
            style_scores.setdefault(attr, {}).setdefault(style[attr], []).append(score)

    # Merge with A/B results
    insights = []

    # Hook type insight
    hook_data = style_scores.get("hook_type", {})
    if hook_data:
        best_hook = max(hook_data, key=lambda k: sum(hook_data[k]) / len(hook_data[k]))
        avg = sum(hook_data[best_hook]) / len(hook_data[best_hook])
        insights.append(f"Best performing hook style: {best_hook} (avg engagement: {avg:.0f})")
    elif ab_winners.get("hook_type"):
        w = ab_winners["hook_type"]
        insights.append(f"Best performing hook style: {w['value']} (from A/B tests, avg: {w['avg_score']})")

    # CTA style insight
    cta_data = style_scores.get("cta_style", {})
    if cta_data:
        best_cta = max(cta_data, key=lambda k: sum(cta_data[k]) / len(cta_data[k]))
        avg = sum(cta_data[best_cta]) / len(cta_data[best_cta])
        insights.append(f"Best performing CTA style: {best_cta} (avg engagement: {avg:.0f})")
    elif ab_winners.get("cta_style"):
        w = ab_winners["cta_style"]
        insights.append(f"Best performing CTA style: {w['value']} (from A/B tests, avg: {w['avg_score']})")

    # Feature count insight
    feature_scores: dict[int, list[float]] = {}
    for record in data:
        eng = record.get("engagement")
        if not eng:
            continue
        fc = record["style"].get("feature_count", 0)
        score = eng.get("likes", 0) + eng.get("retweets", 0) * 2 + eng.get("replies", 0) * 3
        feature_scores.setdefault(fc, []).append(score)
    if feature_scores:
        best_fc = max(feature_scores, key=lambda k: sum(feature_scores[k]) / len(feature_scores[k]))
        insights.append(f"Optimal feature count in tweets: {best_fc}")

    if not insights:
        return ""

    return "STYLE PERFORMANCE DATA (use this to guide your writing):\n" + "\n".join(f"- {i}" for i in insights)


def _score_engagement(eng: dict) -> float:
    """Weighted engagement: replies signal most, retweets next, likes last."""
    return eng.get("likes", 0) + eng.get("retweets", 0) * 2 + eng.get("replies", 0) * 3


def get_style_guidance() -> str:
    """Return LLM-readable, directive style guidance from tweet performance + A/B tests.

    Combines both data sources so early-stage signal from A/B tests kicks in
    before enough organic tweets accumulate. Frames winners as explicit
    instructions ("USE X") rather than soft observations.

    Returns empty string if there's no meaningful signal yet.
    """
    records = _load_performance()
    scored = [r for r in records if r.get("engagement") and r["style"]]

    # Aggregate {attribute: {value: [scores]}} from tweet performance
    agg: dict[str, dict[str, list[float]]] = {}
    for r in scored:
        s = _score_engagement(r["engagement"])
        style = r["style"]
        for attr in ("hook_type", "cta_style"):
            agg.setdefault(attr, {}).setdefault(style.get(attr, "unknown"), []).append(s)

    # Merge A/B test winners as a fallback when organic data is thin
    from src.ab_testing import get_winning_styles
    ab_winners = get_winning_styles()

    def _pick_winner(attr: str) -> tuple[str, float, int] | None:
        bucket = agg.get(attr, {})
        if bucket and sum(len(v) for v in bucket.values()) >= 3:
            best = max(bucket, key=lambda k: sum(bucket[k]) / len(bucket[k]))
            avg = sum(bucket[best]) / len(bucket[best])
            return best, avg, sum(len(v) for v in bucket.values())
        if attr in ab_winners:
            w = ab_winners[attr]
            return w["value"], w["avg_score"], 0
        return None

    lines: list[str] = []

    hook = _pick_winner("hook_type")
    if hook:
        label, avg, n = hook
        source = f"n={n} organic" if n else "A/B tests"
        lines.append(f"- USE hook_type={label} (avg engagement {avg:.1f}, {source})")

    cta = _pick_winner("cta_style")
    if cta:
        label, avg, n = cta
        source = f"n={n} organic" if n else "A/B tests"
        lines.append(f"- USE cta_style={label} (avg engagement {avg:.1f}, {source})")

    # Optimal feature count and emoji density from organic data only (needs a few samples)
    if scored:
        # Feature count
        fc_bucket: dict[int, list[float]] = {}
        for r in scored:
            fc_bucket.setdefault(r["style"].get("feature_count", 0), []).append(_score_engagement(r["engagement"]))
        if fc_bucket and sum(len(v) for v in fc_bucket.values()) >= 5:
            best_fc = max(fc_bucket, key=lambda k: sum(fc_bucket[k]) / len(fc_bucket[k]))
            lines.append(f"- USE exactly {best_fc} checkmark features (performance-tested)")

        # Emoji density
        em_bucket: dict[int, list[float]] = {}
        for r in scored:
            em_bucket.setdefault(r["style"].get("emoji_count", 0), []).append(_score_engagement(r["engagement"]))
        if em_bucket and sum(len(v) for v in em_bucket.values()) >= 5:
            best_em = max(em_bucket, key=lambda k: sum(em_bucket[k]) / len(em_bucket[k]))
            lines.append(f"- Target ~{best_em} emojis total (performance-tested)")

    if not lines:
        return ""

    return "PROVEN-STYLE DIRECTIVES (follow these, they are measured winners):\n" + "\n".join(lines)


def send_weekly_digest() -> bool:
    """Build and send a Monday performance digest to Discord via webhook.

    Covers the prior 7 days. Returns True if the message was sent successfully.
    Gracefully handles the no-data case (sends a brief "not enough data yet" notice).
    """
    import requests as _req
    from config.settings import DISCORD_WEBHOOK_URL

    if not DISCORD_WEBHOOK_URL:
        print("  [digest] DISCORD_WEBHOOK_URL not set — skipping digest")
        return False

    data = _load_performance()
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    # Split into this-week and last-week records
    def _parse_posted_at(r: dict):
        raw = r.get("posted_at", "")
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    this_week = [r for r in data if (dt := _parse_posted_at(r)) and dt >= week_ago]
    with_eng = [r for r in this_week if r.get("engagement")]

    # ── Build embed fields ──────────────────────────────────────────
    fields = []

    if not this_week:
        embed = {
            "title": "Weekly Performance Digest",
            "description": "No tweets tracked in the last 7 days. Run the pipeline and check that `tweet_learner.record_tweet()` is being called.",
            "color": 0x5865F2,
            "footer": {"text": "QuadStar Deals · weekly digest"},
            "timestamp": now.isoformat(),
        }
        try:
            _req.post(DISCORD_WEBHOOK_URL, json={"username": "QuadStar Analytics", "embeds": [embed]}, timeout=8)
        except Exception as e:
            print(f"  [digest] Failed to send: {e}")
            return False
        return True

    # ── Overview stats ──────────────────────────────────────────────
    total_likes   = sum(r["engagement"].get("likes", 0)    for r in with_eng)
    total_rts     = sum(r["engagement"].get("retweets", 0) for r in with_eng)
    total_replies = sum(r["engagement"].get("replies", 0)  for r in with_eng)

    overview = (
        f"**{len(this_week)}** tweets posted  ·  **{len(with_eng)}** with engagement data\n"
        f"❤️ {total_likes} likes  ·  🔁 {total_rts} retweets  ·  💬 {total_replies} replies"
    )
    fields.append({"name": "📊 This Week", "value": overview, "inline": False})

    # ── Format breakdown ────────────────────────────────────────────
    fmt_scores: dict[str, list[float]] = {}
    for r in with_eng:
        fmt = r.get("style", {}).get("hook_type", "unknown")
        fmt_scores.setdefault(fmt, []).append(_score_engagement(r["engagement"]))

    if fmt_scores:
        ranked = sorted(fmt_scores.items(), key=lambda kv: sum(kv[1]) / len(kv[1]), reverse=True)
        fmt_lines = []
        for fmt, scores in ranked:
            avg = sum(scores) / len(scores)
            bar = "█" * min(int(avg / 5), 8) or "·"
            fmt_lines.append(f"`{fmt:<22}` {bar}  avg {avg:.0f}  (n={len(scores)})")
        fields.append({
            "name": "🏆 Format Leaderboard",
            "value": "\n".join(fmt_lines),
            "inline": False,
        })

        best_fmt, best_scores = ranked[0]
        worst_fmt, worst_scores = ranked[-1]
        if len(ranked) > 1:
            fields.append({
                "name": "✅ Best Format",
                "value": f"`{best_fmt}` — avg score {sum(best_scores)/len(best_scores):.0f}",
                "inline": True,
            })
            fields.append({
                "name": "⚠️ Weakest Format",
                "value": f"`{worst_fmt}` — avg score {sum(worst_scores)/len(worst_scores):.0f}",
                "inline": True,
            })

    # ── Top tweet ───────────────────────────────────────────────────
    if with_eng:
        top = max(with_eng, key=lambda r: _score_engagement(r["engagement"]))
        top_score = _score_engagement(top["engagement"])
        eng = top["engagement"]
        top_text = (
            f"❤️ {eng.get('likes',0)} · 🔁 {eng.get('retweets',0)} · 💬 {eng.get('replies',0)}  "
            f"(score {top_score:.0f})\n"
            f"Deal #{top['deal_id']} · style: `{top.get('style',{}).get('hook_type','?')}`"
        )
        fields.append({"name": "🌟 Top Tweet", "value": top_text, "inline": False})

    # ── Viral count (score >= 20) ───────────────────────────────────
    viral = [r for r in with_eng if _score_engagement(r["engagement"]) >= 20]
    if viral:
        fields.append({
            "name": "🚀 Viral Threshold (score ≥ 20)",
            "value": f"**{len(viral)}** tweet{'s' if len(viral) != 1 else ''} hit it this week",
            "inline": True,
        })
    else:
        fields.append({
            "name": "🚀 Viral Threshold (score ≥ 20)",
            "value": "None yet — keep posting consistently",
            "inline": True,
        })

    # ── Style recommendation ────────────────────────────────────────
    guidance = get_style_guidance()
    rec_text = guidance.replace("PROVEN-STYLE DIRECTIVES (follow these, they are measured winners):\n", "").strip() or "Not enough data for a recommendation yet — need at least 3 scored tweets."
    fields.append({"name": "💡 Recommendation for Next Week", "value": rec_text, "inline": False})

    # ── Assemble embed ──────────────────────────────────────────────
    week_start = week_ago.strftime("%b %d")
    week_end   = now.strftime("%b %d")
    embed = {
        "title": f"Weekly Performance Digest · {week_start} – {week_end}",
        "color": 0x00E676,
        "fields": fields,
        "footer": {"text": "QuadStar Deals · every Monday 9 AM"},
        "timestamp": now.isoformat(),
    }

    try:
        resp = _req.post(
            DISCORD_WEBHOOK_URL,
            json={"username": "QuadStar Analytics", "embeds": [embed]},
            timeout=8,
        )
        resp.raise_for_status()
        print(f"  [digest] Weekly digest sent ({len(this_week)} tweets, {len(with_eng)} with engagement)")
        return True
    except Exception as e:
        print(f"  [digest] Failed to send: {e}")
        return False


def get_performance_report() -> str:
    """Full performance report for the agent tool."""
    data = _load_performance()
    total = len(data)
    with_engagement = sum(1 for d in data if d.get("engagement"))

    if with_engagement == 0:
        return f"Tweet performance: {total} tweets tracked, no engagement data collected yet."

    # Overall stats
    total_likes = sum(d["engagement"].get("likes", 0) for d in data if d.get("engagement"))
    total_rts = sum(d["engagement"].get("retweets", 0) for d in data if d.get("engagement"))
    total_replies = sum(d["engagement"].get("replies", 0) for d in data if d.get("engagement"))

    lines = [
        f"Tweet Performance Report ({with_engagement}/{total} with engagement data)",
        f"  Total: {total_likes} likes, {total_rts} retweets, {total_replies} replies",
        "",
    ]

    insights = get_style_insights()
    if insights:
        lines.append(insights)

    return "\n".join(lines)
