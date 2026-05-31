"""Telegram channel publisher — direct Bot API, no Postiz.

Follows the platform isolation rule: this module is fully self-contained.
Failures never propagate to callers — send_deal() always returns bool.
Feature is inert until TELEGRAM_BOT_TOKEN + TELEGRAM_CHANNEL_ID are set.

Active-hours queue: posts outside TELEGRAM_MIN_HOUR..TELEGRAM_MAX_HOUR (UTC)
are written to data/telegram_queue.json and sent when process_queue() runs.
"""
import json
import os
import requests
from datetime import datetime

_API = "https://api.telegram.org/bot{token}/{method}"
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_QUEUE_FILE = os.path.join(_DATA_DIR, "telegram_queue.json")


def _is_active_hour() -> bool:
    """Return True if current UTC hour is within the configured posting window."""
    from config.settings import TELEGRAM_MIN_HOUR, TELEGRAM_MAX_HOUR
    return TELEGRAM_MIN_HOUR <= datetime.utcnow().hour < TELEGRAM_MAX_HOUR


def _enqueue(deal: dict, content: dict) -> None:
    """Append a deal to the Telegram queue for later processing."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    queue = []
    if os.path.exists(_QUEUE_FILE):
        try:
            with open(_QUEUE_FILE) as f:
                queue = json.load(f)
        except Exception:
            queue = []
    queue.append({"deal": deal, "content": content, "queued_at": datetime.utcnow().isoformat()})
    tmp = f"{_QUEUE_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(queue, f, indent=2, default=str)
    os.replace(tmp, _QUEUE_FILE)
    print(f"  [telegram] Outside active hours — queued: {deal.get('title', '')[:50]}")


def process_queue() -> int:
    """Send all queued Telegram posts during active hours. Returns count sent."""
    if not _is_active_hour():
        return 0
    if not os.path.exists(_QUEUE_FILE):
        return 0
    try:
        with open(_QUEUE_FILE) as f:
            queue = json.load(f)
    except Exception:
        return 0
    if not queue:
        return 0

    sent = 0
    failed = []
    for item in queue:
        ok = send_deal(item["deal"], item["content"], skip_hour_check=True)
        if ok:
            sent += 1
        else:
            failed.append(item)

    # Write back only the ones that failed (retry next run)
    tmp = f"{_QUEUE_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(failed, f, indent=2, default=str)
    os.replace(tmp, _QUEUE_FILE)

    if sent:
        print(f"  [telegram] Queue flushed: {sent} sent, {len(failed)} failed/retrying")
    return sent


def is_configured() -> bool:
    """Return True only if both env vars are set."""
    from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID)


def send_deal(deal: dict, content: dict, channel_id: str = "", skip_hour_check: bool = False) -> bool:
    """Post an approved deal to a Telegram channel immediately.

    Combines tweet_1 (hook + features) and tweet_2 (affiliate link + CTA)
    into a single Telegram message. Tries sendPhoto first; falls through
    to sendMessage if the image URL is missing or Telegram can't fetch it.

    Args:
        deal: Deal dict. Uses image_url for the photo.
        content: Content dict. Uses tweet_1 and tweet_2.
        channel_id: Override channel. Falls back to TELEGRAM_CHANNEL_ID.

    Returns:
        True on success, False on any error. Never raises.
    """
    from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID

    token = TELEGRAM_BOT_TOKEN
    target = channel_id or TELEGRAM_CHANNEL_ID
    if not token or not target:
        print("  [telegram] Not configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHANNEL_ID")
        return False

    # Queue if outside active hours (unless we're already processing the queue)
    if not skip_hour_check and not _is_active_hour():
        _enqueue(deal, content)
        return True  # Caller treats queued-for-later as success

    # Combine tweet_1 (hook+features) + tweet_2 (link+CTA) into one message.
    # Telegram caption limit for sendPhoto is 1024 chars.
    # tweet_1 (<280) + "\n\n" + tweet_2 (<280) ≈ 562 chars max — well within limit.
    caption = content.get("tweet_1", "").strip() + "\n\n" + content.get("tweet_2", "").strip()
    if len(caption) > 1024:
        caption = caption[:1021] + "..."

    image_url = deal.get("image_url", "")
    use_photo = bool(image_url and not image_url.startswith("data:"))

    try:
        if use_photo:
            resp = requests.post(
                _API.format(token=token, method="sendPhoto"),
                json={"chat_id": target, "photo": image_url, "caption": caption},
                timeout=10,
            )
            if resp.status_code == 200:
                print(f"  [telegram] Posted (photo): {deal.get('title', '')[:50]}")
                return True
            # Non-200 (e.g. bad image URL) — fall through to sendMessage

        # Plain text — no parse_mode avoids HTML-entity issues with & in titles.
        # disable_web_page_preview=False lets Telegram auto-preview the affiliate link.
        resp = requests.post(
            _API.format(token=token, method="sendMessage"),
            json={"chat_id": target, "text": caption, "disable_web_page_preview": False},
            timeout=10,
        )
        if resp.status_code == 200:
            print(f"  [telegram] Posted (text): {deal.get('title', '')[:50]}")
            return True

        print(f"  [telegram] API error {resp.status_code}: {resp.json().get('description', '')}")
        return False

    except Exception as exc:
        print(f"  [telegram] send_deal failed: {exc}")
        return False
