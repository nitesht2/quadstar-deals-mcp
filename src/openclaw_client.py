"""openclaw_client.py — Push notifications and browse tasks to OpenClaw.

Notifications reach the user on whatever channel they're using (Discord,
WhatsApp, iMessage, voice). browse() delegates real-browser tasks to OpenClaw
for sites that block Scrapling/Playwright.

Feature is fully inert when OPENCLAW_WEBHOOK_URL is not set.
All functions are exception-safe — never propagate to callers.
"""
import requests
from datetime import datetime


def is_configured() -> bool:
    """Return True only when OpenClaw webhook is set in env."""
    from config.settings import OPENCLAW_WEBHOOK_URL
    return bool(OPENCLAW_WEBHOOK_URL)


def _post(payload: dict, timeout: int = 10):
    """Authenticated POST to OpenClaw webhook. Returns Response or None."""
    from config.settings import OPENCLAW_WEBHOOK_URL, OPENCLAW_SECRET
    if not OPENCLAW_WEBHOOK_URL:
        return None
    headers = {"Content-Type": "application/json"}
    if OPENCLAW_SECRET:
        headers["Authorization"] = f"Bearer {OPENCLAW_SECRET}"
    try:
        return requests.post(
            OPENCLAW_WEBHOOK_URL, json=payload, headers=headers, timeout=timeout
        )
    except Exception as exc:
        print(f"  [openclaw] POST failed: {exc}")
        return None


def notify(message: str, title: str = "") -> bool:
    """Push a notification to OpenClaw — delivered on the user's active channel."""
    from config.settings import BRAND_NAME
    resp = _post({
        "action": "notify",
        "title": title or BRAND_NAME,
        "message": message,
        "timestamp": datetime.utcnow().isoformat(),
    })
    return bool(resp and resp.status_code < 300)


def notify_price_drop(title: str, old_price: float, new_price: float, drop_pct: float) -> None:
    """Alert user of a price drop across all their connected channels."""
    notify(
        f"Price drop on {title[:60]}\n"
        f"${old_price:.2f} → ${new_price:.2f} (-{drop_pct:.1f}%)",
        title="Price Drop Alert",
    )


def notify_auto_approved(deal_title: str, platforms: list) -> None:
    """Alert user that a deal was auto-approved and where it's being posted."""
    notify(
        f"Auto-approved: {deal_title[:70]}\nPosting to: {', '.join(platforms)}",
        title="Deal Auto-Approved",
    )


def notify_pipeline_error(job: str, error: str) -> None:
    """Alert user of a pipeline failure (scheduler job, scrape, post, etc.)."""
    notify(f"Error in {job}: {error[:200]}", title="Pipeline Error")


def browse(url: str, instruction: str = "") -> str:
    """Ask OpenClaw to browse a URL as a real user and return the scraped content.

    Use for sites that block Scrapling/Playwright: Woot, BestBuy, Reddit,
    Slickdeals direct, eBay Deals, Newegg.
    Returns scraped text or "" if OpenClaw is unavailable.
    """
    resp = _post(
        {
            "action": "browse",
            "url": url,
            "instruction": instruction
                or f"Extract all product deals with title, price, and discount from {url}",
        },
        timeout=60,  # browsing takes longer than a notify
    )
    if not resp or resp.status_code >= 300:
        return ""
    try:
        data = resp.json()
        return data.get("content") or data.get("result") or data.get("text") or ""
    except Exception:
        return ""
