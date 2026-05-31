"""Pre-post price verification.

Fetches the actual Amazon product page for a deal and compares the displayed
price to what we stored in the DB. If mismatch exceeds tolerance, the deal is
rejected to prevent posting wrong prices (and wrong links for product variations).

Uses Playwright to render the page (Amazon product pages are JS-heavy).
"""
from __future__ import annotations

import re
import time
from typing import Optional

# Price mismatch tolerance — deals with >10% price drift get rejected
PRICE_TOLERANCE_PCT = 10.0
# Max time to spend verifying a single deal
VERIFY_TIMEOUT_S = 20


def _extract_prices_from_html(html: str) -> tuple[Optional[float], Optional[float]]:
    """Parse current and list price from Amazon product page HTML.

    Returns (current_price, list_price). Either may be None if not found.
    """
    # Current price: .a-price:not([data-a-strike]) .a-offscreen (first match)
    # Use JS-rendered HTML format Amazon uses on product detail pages
    current = None
    list_price = None

    # Primary current price pattern (product detail page)
    current_patterns = [
        r'<span class="a-offscreen">\$([\d,]+\.?\d*)</span>',
        r'"priceAmount"\s*:\s*([\d.]+)',
        r'id="priceblock_ourprice"[^>]*>\$([\d,]+\.?\d*)',
        r'id="priceblock_dealprice"[^>]*>\$([\d,]+\.?\d*)',
    ]
    for pat in current_patterns:
        m = re.search(pat, html)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if 0 < val < 100000:
                    current = val
                    break
            except (ValueError, IndexError):
                continue

    # List price (strike-through)
    list_patterns = [
        r'data-a-strike="true"[^>]*>\s*<span[^>]*class="a-offscreen"[^>]*>\$([\d,]+\.?\d*)',
        r'"listPrice"\s*:\s*\{\s*"amount"\s*:\s*([\d.]+)',
        r'List Price:</span>\s*<span[^>]*>\$([\d,]+\.?\d*)',
    ]
    for pat in list_patterns:
        m = re.search(pat, html)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if 0 < val < 100000:
                    list_price = val
                    break
            except (ValueError, IndexError):
                continue

    return current, list_price


def verify_deal_price(deal: dict) -> tuple[bool, str]:
    """Fetch the actual Amazon page and verify deal prices match DB.

    Returns (is_valid, reason). is_valid=True means the deal's prices are
    accurate and it can safely be posted. is_valid=False means reject.
    """
    url = deal.get("source_url") or deal.get("affiliate_url", "")
    if not url:
        return False, "no URL"

    # Strip affiliate tag for verification fetch (avoid affiliate cookie pollution)
    verify_url = re.sub(r"[?&]tag=[^&]+", "", url)
    verify_url = re.sub(r"[?&]psc=\d+", "", verify_url).split("#")[0]

    db_current = deal.get("deal_price") or 0
    db_original = deal.get("original_price") or 0

    if db_current <= 0:
        return False, "no DB price to verify"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        # Fail open if Playwright unavailable — don't block posts
        return True, "playwright unavailable, skipping verify"

    actual_current = None
    actual_list = None

    # StealthyFetcher bypasses Amazon's bot detection (raw Playwright gets captcha)
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError:
        return True, "scrapling unavailable, skipping verify"

    page = None
    for attempt in range(2):
        try:
            page = StealthyFetcher.fetch(
                verify_url,
                headless=True,
                network_idle=True,
                timeout=VERIFY_TIMEOUT_S * 1000,
            )
            if page and page.status == 200:
                break  # Page loaded — extract prices from HTML regardless of selector
        except Exception as e:
            if attempt == 1:
                return True, f"verify fetch failed: {e}"
        if attempt == 0:
            time.sleep(2)  # Brief backoff before retry

    if not page or page.status != 200:
        return True, f"fetch failed (status={page.status if page else 'none'}), skipping verify"

    # CAPTCHA detection — Amazon returns a tiny (~5KB) captcha page when bot-detected.
    # A real product page is always 100KB+. Fail open to avoid blocking valid deals.
    try:
        body_len = len(page.body or b"")
    except Exception:
        body_len = 0
    if body_len < 15000:
        return True, f"captcha/redirect detected (body={body_len}b), skipping verify"

    # Current price: first .a-offscreen that's NOT inside a strike-through
    current_prices = page.css('span.a-price:not([data-a-strike]) .a-offscreen')
    for el in current_prices:
        txt = el.text or ""
        m = re.search(r'\$([\d,]+\.?\d*)', txt)
        if m:
            try:
                val = float(m.group(1).replace(',', ''))
                if 0 < val < 100000:
                    actual_current = val
                    break
            except ValueError:
                continue

    # List price (strike-through)
    list_prices = page.css('span.a-price[data-a-strike] .a-offscreen')
    for el in list_prices:
        txt = el.text or ""
        m = re.search(r'\$([\d,]+\.?\d*)', txt)
        if m:
            try:
                val = float(m.group(1).replace(',', ''))
                if 0 < val < 100000:
                    actual_list = val
                    break
            except ValueError:
                continue

    if actual_current is None:
        # CSS selector missed — try HTML regex fallback (catches JSON-LD price data,
        # priceblock elements, and other non-standard page layouts)
        raw_html = ""
        if page:
            try:
                raw_html = page.body or ""
                if isinstance(raw_html, bytes):
                    raw_html = raw_html.decode("utf-8", errors="ignore")
            except Exception:
                raw_html = ""
        html_current, html_list = _extract_prices_from_html(raw_html)
        if html_current is not None:
            actual_current = html_current
            if actual_list is None and html_list is not None:
                actual_list = html_list
            print(f"    [verify] CSS selector missed, recovered via HTML fallback: ${actual_current:.2f}")
        else:
            return False, "could not extract current price from page (variation mismatch?)"

    # Direction-aware drift check:
    # - actual > DB (price went UP): stale/wrong DB price — reject if drift > tolerance
    # - actual < DB (price went DOWN): deal got better — safe to post, update implied price
    if actual_current > db_current:
        drift_pct = (actual_current - db_current) / db_current * 100
        if drift_pct > PRICE_TOLERANCE_PCT:
            return False, f"price drift +{drift_pct:.1f}%: DB=${db_current:.2f}, actual=${actual_current:.2f}"
    else:
        # Price dropped — log it but allow through
        drop_pct = (db_current - actual_current) / db_current * 100
        if drop_pct > 1:
            print(f"    [verify] price dropped {drop_pct:.1f}%: DB=${db_current:.2f}, actual=${actual_current:.2f} — posting at actual price")

    # If we also have a list price mismatch, that's often benign (list prices fluctuate).
    # Log but don't reject.
    if actual_list and db_original > 0:
        list_drift = abs(actual_list - db_original) / db_original * 100
        if list_drift > 20:
            print(f"    [verify] list price drift {list_drift:.0f}%: DB=${db_original:.0f} actual=${actual_list:.0f}")

    return True, f"verified: current=${actual_current:.2f}"
