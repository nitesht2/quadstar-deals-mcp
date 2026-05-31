"""
tweet_poster.py — X/Twitter API v2 Post & Reply via OAuth 1.0a

Used by the reply finder system to post replies to target account tweets.
NOT used for deal posts — those go through Postiz.

Credentials (from .env):
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
"""

import os
import requests
from requests_oauthlib import OAuth1

_TWEETS_URL = "https://api.twitter.com/2/tweets"


def _create_auth() -> OAuth1 | None:
    """Build OAuth1 session. Returns None if credentials are missing.

    Reads env vars at call time (not module import) so dotenv changes
    take effect without a restart and startup order doesn't matter.
    """
    key = os.getenv("X_API_KEY", "")
    secret = os.getenv("X_API_SECRET", "")
    token = os.getenv("X_ACCESS_TOKEN", "")
    token_secret = os.getenv("X_ACCESS_TOKEN_SECRET", "")
    if not all([key, secret, token, token_secret]):
        return None
    return OAuth1(key, secret, token, token_secret)


def _post(payload: dict) -> dict:
    """Internal POST to /2/tweets. Returns {status, tweet_id?, error?}."""
    auth = _create_auth()
    if not auth:
        return {"status": "error", "error": "X API credentials not configured"}

    try:
        resp = requests.post(_TWEETS_URL, json=payload, auth=auth, timeout=15)
        if resp.status_code == 429:
            return {"status": "rate_limited", "error": "X API rate limit hit"}
        if resp.status_code == 401:
            return {"status": "error", "error": "X API auth failed — check credentials"}
        if resp.status_code not in (200, 201):
            snippet = resp.text[:300] if resp.text else "no body"
            return {"status": "error", "error": f"HTTP {resp.status_code}: {snippet}"}

        data = resp.json()
        tweet_id = data.get("data", {}).get("id")
        return {"status": "ok", "tweet_id": tweet_id}

    except requests.exceptions.RequestException as e:
        return {"status": "error", "error": str(e)}


def post_reply(tweet_id: str, text: str) -> dict:
    """Post a reply to an existing tweet.

    Args:
        tweet_id: ID of the tweet to reply to
        text: Reply text (max 280 chars)

    Returns:
        {"status": "ok", "tweet_id": "..."} or {"status": "error", "error": "..."}
    """
    return _post({
        "text": text[:280],
        "reply": {"in_reply_to_tweet_id": tweet_id},
    })


def post_tweet(text: str) -> dict:
    """Post a standalone tweet.

    Args:
        text: Tweet text (max 280 chars)

    Returns:
        {"status": "ok", "tweet_id": "..."} or {"status": "error", "error": "..."}
    """
    return _post({"text": text[:280]})
