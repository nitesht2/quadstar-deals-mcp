"""
feedback.py - Discord Feedback System (Reactions + Comments)

Reads both emoji reactions (👍/👎) and text replies from Discord.
Learns your preferences to rank future deals better.

Comment examples:
  "too expensive" → learns your price ceiling
  "love headphones" → boosts headphone deals
  "no more speakers" → deprioritizes speakers
  "more like this" → boosts similar products
"""

import json
import os
import re
import time
import requests
from config.settings import DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
PREFS_FILE = os.path.join(DATA_DIR, "preferences.json")


def _load_prefs() -> dict:
    if os.path.exists(PREFS_FILE):
        with open(PREFS_FILE, "r") as f:
            return json.load(f)
    return {
        "liked_keywords": {},
        "disliked_keywords": {},
        "liked_retailers": {},
        "disliked_retailers": {},
        "liked_price_ranges": {},
        "total_feedback": 0,
        "comments": [],
    }


def _save_prefs(prefs: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2)


def _get_reactions(message_id: str) -> dict:
    """Get reaction counts for a Discord message."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return {}

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        msg = resp.json()

        result = {}
        for reaction in msg.get("reactions", []):
            emoji = reaction.get("emoji", {}).get("name", "")
            count = reaction.get("count", 0) - 1  # Subtract bot's own reaction
            if count > 0:
                result[emoji] = count
        return result
    except Exception as e:
        print(f"    Error reading reactions for {message_id}: {e}")
        return {}


def _get_replies(message_id: str) -> list:
    """Get text replies to a Discord message (your comments/feedback)."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return []

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages?after={message_id}&limit=10"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        messages = resp.json()

        replies = []
        for msg in messages:
            # Check if this message references (replies to) our deal message
            ref = msg.get("message_reference", {})
            if ref.get("message_id") == message_id:
                content = msg.get("content", "").strip()
                if content:
                    replies.append(content)
            # Also check if it's a nearby message that looks like feedback
            elif msg.get("content", "").strip() and not msg.get("webhook_id"):
                # Non-webhook message near the deal = likely user feedback
                content = msg.get("content", "").strip()
                if len(content) < 200:  # Short messages are feedback
                    replies.append(content)

        return replies
    except Exception as e:
        print(f"    Error reading replies for {message_id}: {e}")
        return []


def _parse_comment_sentiment(comment: str) -> tuple[str, list]:
    """Parse a comment into sentiment and keywords.
    Returns (sentiment, keywords) where sentiment is 'positive', 'negative', or 'neutral'.
    """
    lower = comment.lower()

    negative_signals = [
        "no", "don't", "stop", "skip", "hate", "boring", "expensive",
        "too much", "not interested", "no more", "enough", "trash",
        "pass", "nah", "meh", "overpriced", "bad",
    ]
    positive_signals = [
        "yes", "love", "more", "great", "amazing", "want", "need",
        "like this", "perfect", "fire", "sick", "awesome", "nice",
        "good", "keep", "more like",
    ]

    is_negative = any(sig in lower for sig in negative_signals)
    is_positive = any(sig in lower for sig in positive_signals)

    if is_negative and not is_positive:
        sentiment = "negative"
    elif is_positive and not is_negative:
        sentiment = "positive"
    else:
        sentiment = "neutral"

    # Extract product-related keywords from comment
    stop_words = {"the", "a", "an", "i", "me", "my", "this", "that", "these", "too", "more", "no", "not", "don't", "like", "want"}
    words = re.findall(r'[a-z]+', lower)
    keywords = [w for w in words if len(w) > 3 and w not in stop_words]

    return sentiment, keywords


def _price_range_key(price: float) -> str:
    if price < 100:
        return "$50-99"
    elif price < 250:
        return "$100-249"
    elif price < 500:
        return "$250-499"
    elif price < 1000:
        return "$500-999"
    else:
        return "$1000+"


def _extract_keywords(title: str) -> list:
    stop_words = {"the", "a", "an", "and", "or", "for", "with", "in", "on", "at", "to", "of", "is", "it"}
    words = title.lower().split()
    return [w.strip(".,!?()[]") for w in words if len(w) > 2 and w not in stop_words]


def collect_feedback(deals: list) -> int:
    """Check reactions AND replies on posted deals. Returns count with new feedback."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        print("  DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID not set. Skipping feedback.")
        return 0

    prefs = _load_prefs()
    feedback_count = 0

    for deal in deals:
        msg_id = deal.get("discord_message_id")
        if not msg_id or deal.get("feedback_collected"):
            continue

        # Check emoji reactions
        reactions = _get_reactions(msg_id)
        liked = reactions.get("\U0001f44d", 0) > 0
        disliked = reactions.get("\U0001f44e", 0) > 0
        time.sleep(0.5)  # respect Discord rate limits

        # Check text replies/comments
        replies = _get_replies(msg_id)
        time.sleep(0.5)

        if not liked and not disliked and not replies:
            continue

        keywords = _extract_keywords(deal.get("title", ""))
        retailer = deal.get("retailer", "Unknown")
        price_range = _price_range_key(deal.get("deal_price", 0))

        # Process emoji reactions
        if liked:
            for kw in keywords:
                prefs["liked_keywords"][kw] = prefs["liked_keywords"].get(kw, 0) + 1
            prefs["liked_retailers"][retailer] = prefs["liked_retailers"].get(retailer, 0) + 1
            prefs["liked_price_ranges"][price_range] = prefs["liked_price_ranges"].get(price_range, 0) + 1

        if disliked:
            for kw in keywords:
                prefs["disliked_keywords"][kw] = prefs["disliked_keywords"].get(kw, 0) + 1
            prefs["disliked_retailers"][retailer] = prefs["disliked_retailers"].get(retailer, 0) + 1

        # Process text comments
        for comment in replies:
            sentiment, comment_kws = _parse_comment_sentiment(comment)

            if sentiment == "positive":
                for kw in keywords + comment_kws:
                    prefs["liked_keywords"][kw] = prefs["liked_keywords"].get(kw, 0) + 2
            elif sentiment == "negative":
                for kw in keywords + comment_kws:
                    prefs["disliked_keywords"][kw] = prefs["disliked_keywords"].get(kw, 0) + 2

            # Store raw comment for reference
            prefs.setdefault("comments", []).append({
                "deal_title": deal.get("title", "")[:60],
                "comment": comment,
                "sentiment": sentiment,
            })
            # Keep only last 50 comments
            prefs["comments"] = prefs["comments"][-50:]

            print(f"    Comment ({sentiment}): \"{comment[:40]}\" on {deal['title'][:30]}")

        deal["feedback_collected"] = True
        prefs["total_feedback"] = prefs.get("total_feedback", 0) + 1
        feedback_count += 1

        feedback_type = []
        if liked:
            feedback_type.append("👍")
        if disliked:
            feedback_type.append("👎")
        if replies:
            feedback_type.append(f"💬x{len(replies)}")
        print(f"    Feedback: {' '.join(feedback_type)} {deal['title'][:50]}")

    _save_prefs(prefs)
    return feedback_count


def score_deal(deal: dict) -> float:
    """Score a deal based on learned preferences. Higher = user will like it more."""
    prefs = _load_prefs()

    if prefs.get("total_feedback", 0) == 0:
        return deal.get("discount_pct", 0)

    score = deal.get("discount_pct", 0)
    keywords = _extract_keywords(deal.get("title", ""))
    retailer = deal.get("retailer", "Unknown")
    price_range = _price_range_key(deal.get("deal_price", 0))

    for kw in keywords:
        score += prefs["liked_keywords"].get(kw, 0) * 5
        score -= prefs["disliked_keywords"].get(kw, 0) * 5

    score += prefs["liked_retailers"].get(retailer, 0) * 10
    score -= prefs.get("disliked_retailers", {}).get(retailer, 0) * 10
    score += prefs["liked_price_ranges"].get(price_range, 0) * 3

    return score
