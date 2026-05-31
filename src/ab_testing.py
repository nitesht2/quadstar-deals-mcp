from __future__ import annotations

"""
ab_testing.py - A/B Test Tracking for Tweet Variants

Generates two stylistically different tweet variants per deal,
schedules them at different times, and tracks engagement.

Variant A: urgency/scarcity angle (CAPS hooks, FOMO CTAs)
Variant B: value/benefit angle (lowercase hooks, feature-focused CTAs)

Engagement is fetched from Postiz API after posts go live.
"""

import json
import os
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
AB_RESULTS_FILE = os.path.join(DATA_DIR, "ab_results.json")


def _load_results() -> list:
    if os.path.exists(AB_RESULTS_FILE):
        with open(AB_RESULTS_FILE, "r") as f:
            return json.load(f)
    return []


def _save_results(results: list):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(AB_RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2, default=str)


# --- Style attribute classification ---

def classify_style(tweet_text: str) -> dict:
    """Classify a tweet's style attributes for performance tracking."""
    text = tweet_text.strip()
    first_line = text.split("\n")[0] if text else ""

    # Hook type: question check first (a CAPS question is still a question)
    if first_line.endswith("?"):
        hook_type = "question"
    elif first_line.isupper() or (len(first_line) > 5 and sum(1 for c in first_line if c.isupper()) / len(first_line) > 0.6):
        hook_type = "caps"
    else:
        hook_type = "lowercase"

    # CTA style (look at last few lines)
    # Normalize apostrophes: LLMs sometimes output "won t" instead of "won't"
    lower = text.lower().replace("'", "'").replace("\u2019", "'")
    lower_normalized = lower.replace("' ", " ").replace("'", "")  # "won't" -> "wont", "won t" stays
    if any(w in lower or w.replace("'", "") in lower_normalized for w in ["won't last", "limited", "hurry", "act fast", "before it's gone", "last long"]):
        cta_style = "urgency"
    elif any(w in lower or w.replace("'", "") in lower_normalized for w in ["don't miss", "selling fast", "running out"]):
        cta_style = "fomo"
    else:
        cta_style = "casual"

    # Feature density
    feature_count = text.count("\u2705")  # checkmark emoji count

    # Emoji density
    import re
    emoji_count = len(re.findall(r'[\U0001f300-\U0001f9ff\u2600-\u26ff\u2700-\u27bf]', text))

    return {
        "hook_type": hook_type,
        "cta_style": cta_style,
        "feature_count": feature_count,
        "emoji_count": emoji_count,
        "char_length": len(text),
    }


# --- Test management ---

def save_ab_test(deal_id: int, variant_a: dict, variant_b: dict,
                 time_a: str, time_b: str, postiz_id_a: str = "", postiz_id_b: str = "") -> dict:
    """Record a new A/B test.

    Args:
        deal_id: The deal being tested.
        variant_a/b: {tweet_1, tweet_2} content dicts.
        time_a/b: ISO scheduled time strings.
        postiz_id_a/b: Postiz post IDs for engagement tracking.

    Returns:
        The saved test record.
    """
    results = _load_results()

    test = {
        "deal_id": deal_id,
        "created_at": datetime.now().isoformat(),
        "variant_a": {
            "tweet_1": variant_a["tweet_1"],
            "style": classify_style(variant_a["tweet_1"]),
            "scheduled_at": time_a,
            "postiz_id": postiz_id_a,
            "engagement": None,
        },
        "variant_b": {
            "tweet_1": variant_b["tweet_1"],
            "style": classify_style(variant_b["tweet_1"]),
            "scheduled_at": time_b,
            "postiz_id": postiz_id_b,
            "engagement": None,
        },
        "winner": None,
    }

    results.append(test)
    # Keep last 100 tests
    results = results[-100:]
    _save_results(results)
    return test


def _fetch_post_engagement(postiz_id: str) -> dict | None:
    """Fetch engagement metrics for a Postiz post via internal analytics API.

    Uses JWT session auth (same as media uploads) to call /api/analytics/post/{id}.
    Postiz fetches the metrics from Twitter using its own stored OAuth token —
    no separate Twitter API plan needed.

    Returns None if the post hasn't been published yet (no releaseId) or on error.
    """
    if not postiz_id:
        return None

    import time
    import requests
    from config.settings import POSTIZ_API_URL
    from src.postiz_client import _get_session_jwt

    token = _get_session_jwt()
    if not token:
        return None

    try:
        date_ms = int(time.time() * 1000)
        resp = requests.get(
            f"{POSTIZ_API_URL}/analytics/post/{postiz_id}?date={date_ms}",
            headers={"auth": token},
            timeout=10,
        )
        if resp.status_code != 200:
            return None

        metrics = resp.json()
        if not isinstance(metrics, list) or not metrics:
            return None

        # Response: [{"label": "Impressions", "data": [{"total": "5", "date": "..."}]}, ...]
        # Sum totals across all days per metric label.
        def _sum_label(label: str) -> int:
            for m in metrics:
                if m.get("label", "").lower() == label.lower():
                    return sum(int(d.get("total", 0)) for d in m.get("data", []))
            return 0

        return {
            "likes": _sum_label("Likes"),
            "retweets": _sum_label("Retweets"),
            "replies": _sum_label("Replies"),
            "impressions": _sum_label("Impressions"),
            "quotes": _sum_label("Quotes"),
            "bookmarks": _sum_label("Bookmarks"),
        }
    except Exception as e:
        print(f"  [ab_testing] Failed to fetch engagement for {postiz_id}: {e}")
        return None


def check_engagement() -> int:
    """Fetch engagement for all tests that don't have it yet. Returns count updated."""
    results = _load_results()
    updated = 0

    for test in results:
        for variant_key in ("variant_a", "variant_b"):
            variant = test[variant_key]
            if variant["engagement"] is None and variant["postiz_id"]:
                engagement = _fetch_post_engagement(variant["postiz_id"])
                if engagement:
                    variant["engagement"] = engagement
                    updated += 1

        # Determine winner if both variants have engagement
        a_eng = test["variant_a"].get("engagement")
        b_eng = test["variant_b"].get("engagement")
        if a_eng and b_eng and not test.get("winner"):
            # Score = likes * 1 + retweets * 2 + replies * 3 (replies are highest signal)
            score_a = a_eng.get("likes", 0) + a_eng.get("retweets", 0) * 2 + a_eng.get("replies", 0) * 3
            score_b = b_eng.get("likes", 0) + b_eng.get("retweets", 0) * 2 + b_eng.get("replies", 0) * 3
            test["winner"] = "a" if score_a >= score_b else "b"
            updated += 1

    if updated:
        _save_results(results)
    return updated


def get_ab_summary() -> str:
    """Get a summary of A/B test results."""
    results = _load_results()
    if not results:
        return "No A/B tests recorded yet."

    total = len(results)
    completed = sum(1 for t in results if t.get("winner"))
    a_wins = sum(1 for t in results if t.get("winner") == "a")
    b_wins = sum(1 for t in results if t.get("winner") == "b")

    # Aggregate style performance
    style_scores: dict[str, list[float]] = {}
    for test in results:
        for variant_key in ("variant_a", "variant_b"):
            variant = test[variant_key]
            eng = variant.get("engagement")
            if not eng:
                continue
            style = variant["style"]
            score = eng.get("likes", 0) + eng.get("retweets", 0) * 2 + eng.get("replies", 0) * 3
            for attr in ("hook_type", "cta_style"):
                key = f"{attr}:{style[attr]}"
                style_scores.setdefault(key, []).append(score)

    lines = [
        f"A/B Test Results: {completed}/{total} completed",
        f"  Variant A wins: {a_wins}, Variant B wins: {b_wins}",
        "",
        "Style performance (avg engagement score):",
    ]
    for key, scores in sorted(style_scores.items()):
        avg = sum(scores) / len(scores) if scores else 0
        lines.append(f"  {key}: {avg:.1f} (n={len(scores)})")

    return "\n".join(lines)


def get_winning_styles() -> dict:
    """Return the best-performing style attributes based on completed tests.

    Used by tweet_learner.py and notifier.py to bias generation toward winning styles.
    """
    results = _load_results()
    style_scores: dict[str, dict[str, list[float]]] = {}

    for test in results:
        for variant_key in ("variant_a", "variant_b"):
            variant = test[variant_key]
            eng = variant.get("engagement")
            if not eng:
                continue
            style = variant["style"]
            score = eng.get("likes", 0) + eng.get("retweets", 0) * 2 + eng.get("replies", 0) * 3
            for attr in ("hook_type", "cta_style"):
                style_scores.setdefault(attr, {}).setdefault(style[attr], []).append(score)

    winners = {}
    for attr, values in style_scores.items():
        best_val = None
        best_avg = -1
        for val, scores in values.items():
            avg = sum(scores) / len(scores) if scores else 0
            if avg > best_avg:
                best_avg = avg
                best_val = val
        if best_val:
            winners[attr] = {"value": best_val, "avg_score": round(best_avg, 1)}

    return winners
