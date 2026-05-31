"""
rss_scraper.py - RSS feed scraper for deal aggregators

Scrapes Slickdeals and DealNews RSS feeds for Amazon deals.
No API key required. Resolves redirect URLs to get real Amazon links.
Reuses save_deal, resolve_amazon_url, and _matches_category from existing modules.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime
import requests

from src.database import save_deal


def resolve_amazon_url(url: str) -> str | None:
    """Follow redirects to find the real Amazon URL. Returns None if not Amazon."""
    if "amazon.com" in url:
        return url
    try:
        resp = requests.head(url, allow_redirects=True, timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"})
        if "amazon.com" in resp.url:
            return resp.url
        resp = requests.get(url, allow_redirects=True, timeout=10,
                            headers={"User-Agent": "Mozilla/5.0"}, stream=True)
        if "amazon.com" in resp.url:
            return resp.url
    except Exception:
        pass
    return None

RSS_SOURCES = [
    {
        "name": "slickdeals",
        "url": "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&q=amazon&rss=1",
        "label": "Slickdeals",
    },
    {
        "name": "dealnews",
        "url": "https://dealnews.com/c142/Electronics/?rss=1",
        "label": "DealNews",
    },
]

# Match: $99, $1,299.99, $99.99
_PRICE_RE = re.compile(r"\$([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?)")
# Match: 30% off, 40% Off, (50% off)
_DISC_RE = re.compile(r"(\d{1,2})%\s*off", re.IGNORECASE)
# Amazon ASIN from URL
_ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")


def _parse_price(text: str) -> float | None:
    """Extract first dollar amount from text."""
    m = _PRICE_RE.search(text)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def _parse_all_prices(text: str) -> list[float]:
    """Extract all dollar amounts from text."""
    return [float(p.replace(",", "")) for p in _PRICE_RE.findall(text)]


def _parse_discount(text: str) -> float | None:
    """Extract discount percentage from text."""
    m = _DISC_RE.search(text)
    return float(m.group(1)) if m else None


def _extract_asin(url: str) -> str | None:
    m = _ASIN_RE.search(url)
    return m.group(1) if m else None


def _build_affiliate_url(asin: str) -> str:
    from config.settings import AMAZON_AFFILIATE_TAG
    return f"https://www.amazon.com/dp/{asin}?tag={AMAZON_AFFILIATE_TAG}"


def _fetch_rss(url: str, timeout: int = 15) -> list[dict]:
    """Fetch and parse RSS feed. Returns list of {title, link, description} dicts."""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        print(f"  [rss] Fetch failed {url}: {e}")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"  [rss] XML parse error {url}: {e}")
        return []

    items = []
    ns = {"media": "http://search.yahoo.com/mrss/"}
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        # Some feeds use media:content for image
        img = None
        media = item.find("media:content", ns)
        if media is not None:
            img = media.get("url")
        if not img:
            # Try <enclosure>
            enc = item.find("enclosure")
            if enc is not None:
                img = enc.get("url")
        items.append({"title": title, "link": link, "description": desc, "image_url": img})
    return items


def _process_item(item: dict, source_name: str) -> bool:
    """Process one RSS item. Returns True if saved as a new deal."""
    from config.settings import TECH_KEYWORDS, MIN_DEAL_PRICE
    from src.amazon_scraper import _matches_category

    title = item["title"]
    raw_link = item["link"]
    desc = item.get("description", "")
    combined_text = f"{title} {desc}"

    # Must contain tech keywords
    if not _matches_category(title, TECH_KEYWORDS):
        return False

    # Resolve to Amazon URL
    amazon_url = resolve_amazon_url(raw_link)
    if not amazon_url:
        return False

    asin = _extract_asin(amazon_url)
    if not asin:
        return False

    # Extract prices
    prices = _parse_all_prices(combined_text)
    discount_pct = _parse_discount(combined_text)

    # Need at least a deal price
    if not prices:
        return False

    deal_price = min(prices)  # Lowest price mentioned is the deal price
    if deal_price < MIN_DEAL_PRICE:
        return False

    # Try to get original price: if 2 prices, higher = original
    original_price = max(prices) if len(prices) >= 2 else None

    # Compute discount if not explicit
    if not discount_pct and original_price and original_price > deal_price:
        discount_pct = round((original_price - deal_price) / original_price * 100, 1)

    # Can't score without a discount — skip
    if not discount_pct or discount_pct < 10:
        return False

    affiliate_url = _build_affiliate_url(asin)

    deal = {
        "title": title[:200],
        "source_url": amazon_url,
        "affiliate_url": affiliate_url,
        "asin": asin,
        "deal_price": deal_price,
        "original_price": original_price or round(deal_price / (1 - discount_pct / 100), 2),
        "discount_pct": discount_pct,
        "image_url": item.get("image_url") or f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01.LZZZZZZZ.jpg",
        "category": "tech",
        "source": source_name,
        "scraped_at": datetime.now().isoformat(),
        "is_posted": False,
        "is_active": True,
    }

    return save_deal(deal)


def run_rss_scraper() -> int:
    """Scrape all RSS sources. Returns count of new deals saved."""
    total = 0
    for source in RSS_SOURCES:
        try:
            items = _fetch_rss(source["url"])
            saved = 0
            for item in items:
                try:
                    if _process_item(item, source["name"]):
                        saved += 1
                except Exception as e:
                    print(f"  [rss:{source['name']}] Item error: {e}")
            if saved:
                print(f"  [rss:{source['name']}] Saved {saved} new deals")
            total += saved
        except Exception as e:
            print(f"  [rss:{source['name']}] Source error: {e}")
    return total
