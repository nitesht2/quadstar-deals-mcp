from __future__ import annotations

"""
price_monitor.py - Background Price Watcher

Monitors ASINs from recently posted/approved deals.
If a price drops below the last recorded price, sends a Discord alert.

Uses Scrapling (StealthyFetcher) to fetch current prices from Amazon product pages.
Reuses database.py price_history for comparison.
"""

import re
import json
import os
from datetime import datetime, timedelta

from src.database import (
    _load_deals, get_price_history, record_price, is_lowest_price,
    DATA_DIR,
)
from config.settings import DISCORD_WEBHOOK_URL, BRAND_NAME


WATCHLIST_DAYS = 7  # Monitor ASINs from deals posted in the last N days
MANUAL_WATCHLIST_FILE = os.path.join(DATA_DIR, "manual_watchlist.json")


def _load_manual_watchlist() -> list[dict]:
    """Load manually-pinned ASINs from disk."""
    if not os.path.exists(MANUAL_WATCHLIST_FILE):
        return []
    try:
        with open(MANUAL_WATCHLIST_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_manual_watchlist(entries: list[dict]):
    """Persist manual watchlist to disk."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = f"{MANUAL_WATCHLIST_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, MANUAL_WATCHLIST_FILE)


def add_to_manual_watchlist(asin: str, title: str = "") -> str:
    """Pin an ASIN for permanent price monitoring.

    Returns a status message suitable for the agent to relay to the user.
    """
    entries = _load_manual_watchlist()
    if any(e.get("asin") == asin for e in entries):
        return f"ASIN {asin} is already on the manual watchlist."
    entries.append({
        "asin": asin,
        "title": title or asin,
        "added_at": datetime.now().isoformat(),
        "last_price": None,
        "affiliate_url": f"https://www.amazon.com/dp/{asin}",
    })
    _save_manual_watchlist(entries)
    return f"Added {title or asin} ({asin}) to manual watchlist. Price monitoring active."


def remove_from_manual_watchlist(asin: str) -> str:
    """Remove a manually-pinned ASIN from monitoring."""
    entries = _load_manual_watchlist()
    before = len(entries)
    entries = [e for e in entries if e.get("asin") != asin]
    if len(entries) == before:
        return f"ASIN {asin} not found in manual watchlist."
    _save_manual_watchlist(entries)
    return f"Removed {asin} from manual watchlist."


def list_manual_watchlist() -> str:
    """Return a human-readable summary of manually-pinned ASINs."""
    entries = _load_manual_watchlist()
    if not entries:
        return "Manual watchlist is empty. Use 'add <ASIN> to watchlist' to pin an item."
    lines = [f"Manual watchlist ({len(entries)} items):"]
    for e in entries:
        price_note = f"last seen ${e['last_price']:.2f}" if e.get("last_price") else "no price recorded yet"
        lines.append(f"  {e.get('title', e['asin'])[:50]} — {e['asin']} ({price_note})")
    return "\n".join(lines)


def _url_price_key(url: str) -> str:
    """Stable key for tracking price history of non-ASIN (non-Amazon) products."""
    import hashlib
    return "url:" + hashlib.md5(url.encode()).hexdigest()[:12]


def get_watchlist() -> list[dict]:
    """Get items to monitor from recently posted deals.

    Returns list of dicts with asin (or None), url_key, title,
    last_price, affiliate_url.  Includes both Amazon (ASIN) and
    non-Amazon deals so every posted product gets price monitoring.
    """
    deals = _load_deals()
    cutoff = datetime.now() - timedelta(days=WATCHLIST_DAYS)
    watchlist = []
    seen_keys = set()

    for deal in deals:
        if not deal.get("is_posted"):
            continue

        posted_at = deal.get("posted_at", "")
        if not posted_at:
            continue
        try:
            if datetime.fromisoformat(posted_at) < cutoff:
                continue
        except (ValueError, TypeError):
            continue

        asin = deal.get("asin")
        source_url = deal.get("source_url", "")

        if asin:
            key = asin
        elif source_url:
            key = _url_price_key(source_url)
        else:
            continue

        if key in seen_keys:
            continue

        history = get_price_history(key)
        last_price = history[-1]["price"] if history else deal.get("deal_price", 0)

        watchlist.append({
            "asin": asin,           # None for non-Amazon deals
            "url_key": key,         # always set (ASIN or url hash)
            "source_url": source_url,
            "title": deal.get("title", ""),
            "last_price": last_price,
            "affiliate_url": deal.get("affiliate_url", source_url),
            "deal_id": deal.get("id"),
        })
        seen_keys.add(key)

    # Merge in manually-pinned ASINs (permanent monitoring, not time-bounded)
    for entry in _load_manual_watchlist():
        asin = entry.get("asin")
        if not asin or asin in seen_keys:
            continue
        history = get_price_history(asin)
        last_price = (
            history[-1]["price"] if history
            else (entry.get("last_price") or 0)
        )
        watchlist.append({
            "asin": asin,
            "url_key": asin,
            "source_url": f"https://www.amazon.com/dp/{asin}",
            "title": entry.get("title", asin),
            "last_price": last_price,
            "affiliate_url": entry.get("affiliate_url", f"https://www.amazon.com/dp/{asin}"),
            "deal_id": None,
        })
        seen_keys.add(asin)

    return watchlist


def _fetch_price_from_url(url: str, title: str = "") -> float | None:
    """Fetch current price from a non-Amazon product page via OpenClaw browse.

    Used for BestBuy, Walmart, Target, Newegg, etc. — sites that would
    block StealthyFetcher. OpenClaw's real browser bypasses bot detection.
    Returns None when OpenClaw is not configured or parsing fails.
    """
    try:
        from src.openclaw_client import browse, is_configured
        if not is_configured():
            return None

        instruction = (
            f"Find the current sale price of '{title[:80]}'. "
            "Return only the numeric price in USD (e.g. 299.99). "
            "If the product is out of stock or price is unavailable, return nothing."
        )
        content = browse(url, instruction)
        if not content:
            return None

        # Extract the first dollar amount from the response
        match = re.search(r'\$?([\d,]+\.?\d{0,2})', content.replace(",", ""))
        if match:
            price = float(match.group(1))
            if 1 < price < 50000:
                return price
    except Exception as exc:
        print(f"  [price_monitor] OpenClaw price fetch failed for {url[:60]}: {exc}")
    return None


def _fetch_price(asin: str) -> float | None:
    """Fetch current price for an ASIN from Amazon product page."""
    url = f"https://www.amazon.com/dp/{asin}"

    try:
        from scrapling import StealthyFetcher
        fetcher = StealthyFetcher()
        page = fetcher.fetch(url)

        # Try multiple price selectors (Amazon changes these)
        price_selectors = [
            'span.a-price span.a-offscreen',
            '#priceblock_dealprice',
            '#priceblock_ourprice',
            'span[data-a-color="price"] span.a-offscreen',
            '.a-price .a-offscreen',
        ]
        for selector in price_selectors:
            elements = page.css(selector)
            if elements:
                price_text = elements[0].text.strip()
                match = re.search(r'[\d,]+\.?\d*', price_text.replace(',', ''))
                if match:
                    return float(match.group())

    except Exception as e:
        print(f"  [price_monitor] Failed to fetch price for {asin}: {e}")

    return None


def _send_drop_webhook(item: dict, old_price: float, new_price: float):
    """Send a Discord webhook embed for a significant price drop (>= 20%)."""
    if not DISCORD_WEBHOOK_URL:
        return

    import requests

    drop_pct = round((1 - new_price / old_price) * 100, 1) if old_price > 0 else 0
    is_lowest = is_lowest_price(item["asin"], new_price)
    lowest_badge = " **[LOWEST EVER]**" if is_lowest else ""

    payload = {
        "username": f"{BRAND_NAME} Price Alert",
        "embeds": [{
            "title": f"Price Drop: {item['title'][:80]}",
            "color": 0xFF4500,
            "description": (
                f"**Was:** ${old_price:.2f}\n"
                f"**Now:** ${new_price:.2f} (-{drop_pct}%){lowest_badge}\n\n"
                f"[View on Amazon]({item['affiliate_url']})"
            ),
            "footer": {"text": "Price monitoring - auto-alert"},
        }],
    }

    try:
        resp = requests.post(f"{DISCORD_WEBHOOK_URL}?wait=true", json=payload, timeout=10)
        resp.raise_for_status()
        print(f"  [price_monitor] Alert sent: {item['title'][:40]} ${old_price} -> ${new_price}")
    except Exception as e:
        print(f"  [price_monitor] Alert failed: {e}")

    # Also push to OpenClaw so the alert reaches any configured channel
    # (WhatsApp, iMessage, voice, etc.). Fire-and-forget — never raises.
    try:
        from src.openclaw_client import notify_price_drop
        notify_price_drop(
            item["title"], old_price, new_price,
            round((1 - new_price / old_price) * 100, 1) if old_price > 0 else 0,
        )
    except Exception:
        pass


def _send_price_alert(drop_info: dict):
    """Send a yellow Discord embed for moderate price drops (FYI only)."""
    import requests
    from config.settings import DISCORD_WEBHOOK_URL
    if not DISCORD_WEBHOOK_URL:
        return

    title = drop_info.get("title", "")[:80]
    new_price = drop_info.get("new_price", 0)
    old_price = drop_info.get("old_price", 0)
    drop_pct = drop_info.get("drop_pct", 0)
    savings = old_price - new_price
    url = drop_info.get("affiliate_url", "")
    image_url = drop_info.get("image_url", "")

    embed = {
        "title": f"Price Alert: {title}",
        "description": (
            f"**Was:** ${old_price:.2f}\n"
            f"**Now:** ${new_price:.2f} (-{drop_pct:.1f}%, save ${savings:.2f})\n"
        ),
        "color": 0xFFA500,  # orange/yellow
        "footer": {"text": "Price monitoring - alert only (below repost threshold)"},
    }
    if url:
        embed["url"] = url
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        print(f"  [price_monitor] Alert webhook failed: {e}")


def detect_drops() -> list[dict]:
    """Check watchlist items for price drops. Returns list of drops found.

    Handles both Amazon (ASIN-based, StealthyFetcher) and non-Amazon
    products (URL-based, OpenClaw real browser).

    Records new prices and sends Discord alerts using tiered thresholds:
    - < 10% drop: ignored completely
    - 10-19% drop: yellow FYI alert only (no repost)
    - >= 20% drop: full repost pipeline
    """
    from config.settings import MIN_ALERT_DROP_PCT, MIN_REPOST_DROP_PCT

    watchlist = get_watchlist()
    if not watchlist:
        print("  [price_monitor] No items to monitor")
        return []

    amazon_count = sum(1 for i in watchlist if i.get("asin"))
    nonamazon_count = len(watchlist) - amazon_count
    print(f"  [price_monitor] Checking {len(watchlist)} items "
          f"({amazon_count} Amazon, {nonamazon_count} non-Amazon)...")
    drops = []
    import time

    for item in watchlist:
        asin = item.get("asin")
        url_key = item.get("url_key", asin)

        # Fetch current price: ASIN → StealthyFetcher, non-ASIN → OpenClaw
        if asin:
            current_price = _fetch_price(asin)
        else:
            current_price = _fetch_price_from_url(
                item.get("source_url", ""), item.get("title", "")
            )

        if current_price is None:
            continue

        # Record the new price observation using url_key (works for both)
        record_price(url_key, current_price)

        last_price = item["last_price"]
        if current_price < last_price and last_price > 0:
            drop_pct = round((1 - current_price / last_price) * 100, 1)

            # Tiered threshold: ignore drops < 10%
            if drop_pct < MIN_ALERT_DROP_PCT:
                print(f"  [price_monitor] Ignoring small drop ({drop_pct}%): {item['title'][:40]}")
                time.sleep(2)
                continue

            drop_info = {
                "asin": asin,
                "url_key": url_key,
                "title": item["title"],
                "old_price": last_price,
                "new_price": current_price,
                "drop_pct": drop_pct,
                "deal_id": item.get("deal_id"),
                "affiliate_url": item.get("affiliate_url", ""),
            }
            drops.append(drop_info)

            if drop_pct >= MIN_REPOST_DROP_PCT:
                # >= 20%: send full alert webhook, eligible for repost pipeline
                _send_drop_webhook(item, last_price, current_price)
            else:
                # 10-19%: yellow FYI alert only, no repost
                _send_price_alert(drop_info)

        time.sleep(2)  # Rate limit between requests

    print(f"  [price_monitor] Found {len(drops)} price drops out of {len(watchlist)} checked")

    # Trigger auto-repost pipeline only for drops >= repost threshold
    repost_drops = [d for d in drops if d["drop_pct"] >= MIN_REPOST_DROP_PCT]
    if repost_drops:
        candidates = _evaluate_repost_candidates(repost_drops)
        if candidates:
            _trigger_repost_pipeline(candidates)

    return drops


def _find_posted_deal(asin: str) -> dict | None:
    """Find the original posted deal for an ASIN."""
    from src.database import _load_deals
    deals = _load_deals()
    for d in deals:
        if d.get("asin") == asin and d.get("is_posted"):
            return d
    return None


def _evaluate_repost_candidates(drops: list[dict]) -> list[dict]:
    """Filter drops to those eligible for auto-repost.

    Qualification rules:
    - >= 20% drop AND >= $15 savings: full repost
    - Lowest ever price AND >= 10% drop: immediate repost (exception)
    """
    from config.settings import (
        MIN_REPOST_DROP_PCT, MIN_REPOST_DROP_DOLLARS,
        MIN_ALERT_DROP_PCT, LOWEST_IN_DAYS,
    )
    from src.database import (
        can_repost, get_original_posted_price,
        is_lowest_in_n_days, is_lowest_price,
    )

    from config.settings import MAX_PRICE_DROP_REPOSTS_PER_CYCLE
    # Process highest-drop ASINs first so the cap keeps the best ones.
    drops = sorted(drops, key=lambda d: d.get("drop_pct", 0), reverse=True)

    candidates = []
    for drop in drops:
        if len(candidates) >= MAX_PRICE_DROP_REPOSTS_PER_CYCLE:
            print(f"  [price_monitor] Repost cap ({MAX_PRICE_DROP_REPOSTS_PER_CYCLE}) reached — "
                  f"{drop['title'][:40]} queued as FYI alert only")
            # Still send a FYI-level alert so the user sees it
            _send_price_alert({**drop, "affiliate_url": drop.get("affiliate_url", "")})
            continue
        if drop["drop_pct"] < MIN_REPOST_DROP_PCT:
            continue

        eligible, reason = can_repost(drop["asin"], drop["new_price"])
        if not eligible:
            print(f"  [price_monitor] Skip repost for {drop['asin']}: {reason}")
            continue

        deal = _find_posted_deal(drop["asin"])
        original_price = get_original_posted_price(drop["asin"])

        is_lowest_ever = is_lowest_price(drop["asin"], drop["new_price"])
        dollar_savings = drop["old_price"] - drop["new_price"]

        drop_info = {
            **drop,
            "original_posted_price": original_price or drop["old_price"],
            "is_lowest_90d": is_lowest_in_n_days(drop["asin"], drop["new_price"], LOWEST_IN_DAYS),
            "is_lowest_ever": is_lowest_ever,
            "image_url": deal.get("image_url", "") if deal else "",
            "deal": deal or {},
            "deal_id": deal.get("id") if deal else drop.get("deal_id"),
        }
        if not drop_info.get("affiliate_url") and deal:
            drop_info["affiliate_url"] = deal.get("affiliate_url", "")

        # Lowest ever at any qualifying drop (>= 10%): immediate repost
        if is_lowest_ever and drop["drop_pct"] >= MIN_ALERT_DROP_PCT:
            candidates.append(drop_info)
            print(f"  [price_monitor] Repost candidate (LOWEST EVER): {drop['title'][:40]} (-{drop['drop_pct']}%)")
            continue

        # Standard path: must be >= 20% AND save >= $15
        if dollar_savings < MIN_REPOST_DROP_DOLLARS:
            print(f"  [price_monitor] Skip repost for {drop['asin']}: saves ${dollar_savings:.2f} (need ${MIN_REPOST_DROP_DOLLARS})")
            continue

        candidates.append(drop_info)
        print(f"  [price_monitor] Repost candidate: {drop['title'][:40]} (-{drop['drop_pct']}%, save ${dollar_savings:.2f})")

    return candidates


def _trigger_repost_pipeline(candidates: list[dict]):
    """Generate content and send price drop cards to Discord."""
    import asyncio
    from src.notifier import generate_price_drop_content

    for drop_info in candidates:
        try:
            content = generate_price_drop_content(drop_info)
            drop_info["content"] = content

            from src.discord_bot import send_price_drop_card, bot_loop
            if bot_loop:
                asyncio.run_coroutine_threadsafe(
                    send_price_drop_card(drop_info, content),
                    bot_loop,
                )
                print(f"  [price_monitor] Repost card dispatched: {drop_info['title'][:40]}")
            else:
                print(f"  [price_monitor] No bot loop, can't send price drop card")
        except Exception as e:
            print(f"  [price_monitor] Repost pipeline error for {drop_info.get('asin', '?')}: {e}")
