"""
reply_finder.py — QuadStar Reply Guy Engine

Finds recent tweets from target tech/deals accounts, generates 3 reply
options via Kimi, and sends cards to the Discord reply channel.
User reacts A/B/C in Discord to post the selected reply.

Runs every 30 min via APScheduler (6 AM – 6 PM PST).
Completely separate from the deal pipeline — no shared state.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests
import yaml

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")

REPLIED_FILE = os.path.join(DATA_DIR, "replied_tweets.json")
FEEDBACK_FILE = os.path.join(DATA_DIR, "reply_feedback.json")

X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")


# ── State helpers ──────────────────────────────────────────────────────────────

def _load_replied() -> set:
    """Load set of tweet IDs already replied to."""
    if os.path.exists(REPLIED_FILE):
        with open(REPLIED_FILE) as f:
            return set(json.load(f))
    return set()


def _save_replied(replied: set) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    # Keep last 2000 IDs to avoid unbounded growth
    trimmed = list(replied)[-2000:]
    tmp = REPLIED_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(trimmed, f)
    os.replace(tmp, REPLIED_FILE)


def _load_feedback() -> list:
    if os.path.exists(FEEDBACK_FILE):
        with open(FEEDBACK_FILE) as f:
            return json.load(f)
    return []


def _save_feedback(feedback: list) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    trimmed = feedback[-200:]
    tmp = FEEDBACK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(trimmed, f, indent=2)
    os.replace(tmp, FEEDBACK_FILE)


def record_reply_feedback(tweet_id: str, chosen_option: int, reply_text: str) -> None:
    """Record which reply option the user picked. Called from Discord bot."""
    feedback = _load_feedback()
    feedback.append({
        "tweet_id": tweet_id,
        "chosen_option": chosen_option,  # 1, 2, or 3
        "reply_text": reply_text,
        "posted_at": datetime.now().isoformat(),
    })
    _save_feedback(feedback)


# ── Config loaders ─────────────────────────────────────────────────────────────

def _load_targets() -> dict:
    path = os.path.join(CONFIG_DIR, "reply_targets.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def _load_voice() -> dict:
    path = os.path.join(CONFIG_DIR, "quadstar_voice.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


# ── X API helpers ──────────────────────────────────────────────────────────────

def _bearer_headers() -> dict:
    return {"Authorization": f"Bearer {X_BEARER_TOKEN}"}


def _fetch_recent_tweets(username: str, lookback_hours: float = 0.5) -> list[dict]:
    """Fetch recent tweets from a user via X API v2."""
    if not X_BEARER_TOKEN:
        return []

    # Get user ID first
    url = f"https://api.twitter.com/2/users/by/username/{username}"
    try:
        r = requests.get(url, headers=_bearer_headers(), timeout=10)
        if r.status_code != 200:
            return []
        user_id = r.json().get("data", {}).get("id")
        if not user_id:
            return []
    except Exception:
        return []

    # Fetch timeline
    start_time = (
        datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    params = {
        "max_results": 10,
        "start_time": start_time,
        "tweet.fields": "created_at,public_metrics,text,conversation_id",
        "exclude": "retweets,replies",  # original tweets only
    }
    try:
        r = requests.get(
            f"https://api.twitter.com/2/users/{user_id}/tweets",
            headers=_bearer_headers(),
            params=params,
            timeout=10,
        )
        if r.status_code == 429:
            print(f"  [reply_finder] Rate limited fetching @{username}", flush=True)
            return []
        if r.status_code != 200:
            return []
        tweets = r.json().get("data") or []
        # Skip threads with too many replies (likely controversial)
        return [
            t for t in tweets
            if t.get("public_metrics", {}).get("reply_count", 0) < 50
        ]
    except Exception:
        return []


# ── Topic filter ───────────────────────────────────────────────────────────────

def _passes_topic_filter(tweet_text: str, topic_filters: dict) -> bool:
    text_lower = tweet_text.lower()
    allowed = topic_filters.get("allowed_topics", [])
    blocked = topic_filters.get("blocked_topics", [])

    if blocked:
        for word in blocked:
            # YAML parses bare 'off'/'on'/'yes'/'no' as bool — coerce to str
            if str(word).lower() in text_lower:
                return False

    if allowed:
        for word in allowed:
            if str(word).lower() in text_lower:
                return True
        return False  # allowed list set but no match

    return True  # no filters — accept all


# ── Reply generation ───────────────────────────────────────────────────────────

def _build_system_prompt(voice: dict) -> str:
    banned = "\n".join(f"  - {w}" for w in voice.get("banned_words", []))
    anti = "\n".join(f"  - {ex}" for ex in voice.get("anti_examples", []))
    patterns = "\n".join(
        f"  - {p['name']}: {p['example']}"
        for p in voice.get("reply_patterns", [])
    )
    rules = "\n".join(f"  - {r}" for r in voice.get("core_rules", []))

    return f"""You write replies for @QuadStarDeals, a deals/savings account on X/Twitter.

Identity: {voice.get('identity', '')}

Core rules:
{rules}

Reply patterns to choose from:
{patterns}

BANNED words/phrases (never use these):
{banned}

Examples of what NOT to write:
{anti}

Output exactly 3 reply options numbered 1., 2., 3.
Each option uses a different reply pattern.
Each option is max 2 sentences.
Each option must reference a specific detail from the tweet (product name, price, spec).
No hashtags. No affiliate links. No emojis unless the original tweet uses them."""


def _generate_reply_options(tweet_text: str, username: str, voice: dict) -> list[str]:
    """Generate 3 reply options via Kimi. Returns list of 3 strings."""
    from src.llm import generate

    system = _build_system_prompt(voice)
    prompt = f"""Tweet from @{username}:
{tweet_text}

Write 3 reply options following the system rules."""

    raw = generate(prompt, max_tokens=600, system=system)
    if not raw:
        return []

    # Parse numbered options: "1. text\n2. text\n3. text"
    options = []
    import re
    parts = re.split(r'\n(?=[123]\.)', raw.strip())
    for part in parts:
        text = re.sub(r'^[123]\.\s*', '', part.strip())
        if text and len(text) > 10:
            options.append(text)

    return options[:3]


def _validate_reply(text: str, voice: dict) -> bool:
    """Basic validation — check banned words and length."""
    if len(text) > 280:
        return False
    text_lower = text.lower()
    for word in voice.get("banned_words", []):
        if str(word).lower() in text_lower:
            return False
    return True


# ── Discord card sender ────────────────────────────────────────────────────────

def _send_reply_card(
    tweet_id: str,
    tweet_url: str,
    tweet_text: str,
    username: str,
    options: list[str],
) -> None:
    """Send reply options to Discord reply channel. Imported by discord_bot."""
    import asyncio
    try:
        from src.discord_bot import send_reply_card as _discord_send_reply_card
        from src.agent import _get_bot_loop  # deferred import — avoids circular at module level
        loop = _get_bot_loop()
        if loop:
            asyncio.run_coroutine_threadsafe(
                _discord_send_reply_card(tweet_id, tweet_url, tweet_text, username, options),
                loop,
            )
    except Exception as e:
        print(f"  [reply_finder] Discord send failed: {e}", flush=True)


# ── Main entry point ───────────────────────────────────────────────────────────

def run_reply_finder() -> int:
    """Fetch recent tweets, generate replies, send to Discord. Returns card count sent."""
    if not X_BEARER_TOKEN:
        print("  [reply_finder] X_BEARER_TOKEN not set — skipping", flush=True)
        return 0

    try:
        targets_cfg = _load_targets()
        voice = _load_voice()
    except Exception as e:
        print(f"  [reply_finder] Config load failed: {e}", flush=True)
        return 0

    replied = _load_replied()
    lookback = targets_cfg.get("lookback_hours", 0.5)
    max_cards = targets_cfg.get("max_cards_per_run", 5)

    tiers_cfg = targets_cfg.get("tiers", {})
    # Process Tier 2 first (deal-focused), then Tier 1, then Tier 3
    tier_order = ["Tier 2", "Tier 1", "Tier 3"]

    cards_sent = 0

    for tier_name in tier_order:
        if cards_sent >= max_cards:
            break

        accounts = tiers_cfg.get(tier_name, [])
        for account in accounts:
            if cards_sent >= max_cards:
                break

            username = account.get("username", "").lstrip("@")
            if not username:
                continue

            topic_filters = account.get("topic_filters", {})

            tweets = _fetch_recent_tweets(username, lookback_hours=lookback)
            if not tweets:
                continue

            for tweet in tweets:
                tweet_id = tweet.get("id", "")
                tweet_text = tweet.get("text", "")

                if not tweet_id or not tweet_text:
                    continue
                if tweet_id in replied:
                    continue
                if not _passes_topic_filter(tweet_text, topic_filters):
                    continue

                options = _generate_reply_options(tweet_text, username, voice)
                if not options:
                    continue

                # Validate options
                valid_options = [o for o in options if _validate_reply(o, voice)]
                if not valid_options:
                    continue

                tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"
                _send_reply_card(tweet_id, tweet_url, tweet_text, username, valid_options)

                # Save immediately after send — prevents ghost buttons on crash-restart
                replied.add(tweet_id)
                _save_replied(replied)
                cards_sent += 1
                print(f"  [reply_finder] Card sent for @{username}: {tweet_text[:60]}", flush=True)
                time.sleep(1)  # brief pause between cards
                break  # one card per account per run

    _save_replied(replied)
    return cards_sent
