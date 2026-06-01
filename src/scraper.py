"""
scraper.py - Aggregator scraper (free / no API key).

Replaces the previous Firecrawl-backed scraper. Uses Playwright to render
DealNews and other JS-heavy aggregator pages, follows their click-redirect URLs
to recover the real Amazon link, then routes each deal through save_deal().

Public surface kept compatible with the prior Firecrawl version so call sites
(agent._scrape_deals, /tools/scrape, tests) don't change:
    run_scraper()                              # orchestrate all DEAL_SOURCES
    scrape_source(client, source)              # one source -> list[deal]
    get_scraper_client()                       # Playwright browser-context client

Helpers reused from the prior implementation:
    resolve_amazon_url, extract_asin, build_amazon_url_from_asin,
    extract_price, calculate_discount, build_affiliate_url,
    _is_image_url, _is_skip_title, _is_tech_deal
"""
from __future__ import annotations

import re
import time

import requests
from config.settings import (
    DEAL_SOURCES, AMAZON_AFFILIATE_TAG,
    MIN_DEAL_PRICE, MIN_DISCOUNT_PCT, MAX_DISCOUNT_PCT, TECH_KEYWORDS, AMAZON_ONLY,
)
from src.database import save_deal


# ── Reusable helpers (verbatim from the prior implementation) ──────────────────

def resolve_amazon_url(url: str) -> str | None:
    """Follow redirects on aggregator URLs to find the real Amazon URL."""
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


def extract_asin(url: str) -> str | None:
    for pat in (r'/dp/([A-Z0-9]{10})', r'/gp/product/([A-Z0-9]{10})',
                r'/product/([A-Z0-9]{10})', r'/ASIN/([A-Z0-9]{10})'):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def build_amazon_url_from_asin(asin: str) -> str:
    return f"https://www.amazon.com/dp/{asin}?tag={AMAZON_AFFILIATE_TAG}"


def extract_price(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r'\$?([\d,]+\.?\d*)', str(text))
    return float(m.group(1).replace(",", "")) if m else None


def calculate_discount(original: float, deal: float) -> float:
    if original and original > 0 and deal < original:
        return round(((original - deal) / original) * 100, 2)
    return 0.0


def build_affiliate_url(url: str) -> str:
    if "amazon.com" in url:
        url = re.sub(r'[?&]tag=[^&]*', '', url)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}tag={AMAZON_AFFILIATE_TAG}"
    return url


def _is_image_url(url: str) -> bool:
    indicators = ('datocms-assets.com', 'imgur.com', 'cloudfront.net',
                  'slickdealscdn.com', 'gravatar.com',
                  '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg')
    return any(i in url.lower() for i in indicators)


def _is_skip_title(title: str) -> bool:
    if not title:
        return True
    lower = title.lower()
    skip_kw = ('sign in', 'sign up', 'login', 'register', 'menu', 'cart',
               'skip to', 'cookie', 'privacy', 'terms', 'about us',
               'load more', 'show more', 'next page', 'subscribe', 'newsletter')
    if any(kw in lower for kw in skip_kw):
        return True
    # Skip vague store-wide promotions, not specific products.
    promo_patterns = (r'up to \d+% off', r'savings at\b', r'clearance',
                      r'shop .+ at\b', r'top \d+ deals', r'best .+ deals',
                      r'save on\b', r'gift guide', r'price drop')
    if any(re.search(p, lower) for p in promo_patterns):
        return True
    return len(re.sub(r'\W+', ' ', lower).split()) < 3


def _is_tech_deal(title: str) -> bool:
    lower = title.lower()
    for kw in TECH_KEYWORDS:
        if len(kw) <= 3:
            if re.search(rf'(?<!\w){re.escape(kw)}(?!\w)', lower):
                return True
        elif kw in lower:
            return True
    return False


# ── Playwright client (replaces Firecrawl) ─────────────────────────────────────

class PlaywrightClient:
    """Thin wrapper around a Playwright Chromium context, started lazily and
    closed at end-of-run. Kept as a class so scrape_source(client, source) keeps
    the same shape the tests expect."""
    def __init__(self):
        self._pw = None
        self._browser = None
        self._ctx = None

    def page(self):
        if self._ctx is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._ctx = self._browser.new_context(
                user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
                viewport={"width": 1920, "height": 1080},
            )
        return self._ctx.new_page()

    def close(self):
        try:
            if self._ctx:
                self._ctx.close()
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = None


def get_scraper_client() -> PlaywrightClient:
    """Public client factory — patchable in tests (replaces get_firecrawl_client)."""
    return PlaywrightClient()


# ── Per-source scrapers ────────────────────────────────────────────────────────

def _render_dealnews(client: PlaywrightClient, url: str) -> list[dict]:
    """Render a DealNews category page with Playwright and return raw card data.

    Returns a list of {text, redirect, img}. Title/price/store/store-name are
    parsed by _parse_dealnews_card downstream.
    """
    page = client.page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        # DealNews cards use Chromium's `content-visibility: auto` so the browser
        # SKIPS rendering offscreen card contents — innerText is empty even after
        # scrolling because cards revert to skeleton state when out of view.
        # Override the CSS so every card renders fully, regardless of viewport.
        page.add_style_tag(content="""
            .content-card, .content-visibility-auto, [class*="content-visibility"] {
                content-visibility: visible !important;
                contain-intrinsic-size: auto !important;
            }
        """)
        # Full scroll-through to trigger any JS-driven populators that ALSO gate
        # on viewport (independent of the CSS fix).
        last_h = 0
        for _ in range(20):
            page.evaluate("window.scrollBy(0, 1200)")
            page.wait_for_timeout(300)
            h = page.evaluate("document.body.scrollHeight")
            if h == last_h:
                break
            last_h = h
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(800)
        raw = page.evaluate("""() => {
            const cards = document.querySelectorAll('.content-card');
            const out = [];
            for (const c of cards) {
                const text = (c.innerText || '').trim();
                if (text.length < 20) continue;
                const click = c.querySelector('a[href*="/lw/click.html"]');
                const img = c.querySelector('img');
                out.push({
                    text,
                    redirect: click ? click.href : null,
                    img: img ? (img.src || img.getAttribute('data-src') || '') : ''
                });
            }
            return out;
        }""")
        return raw or []
    finally:
        try:
            page.close()
        except Exception:
            pass


def _parse_dealnews_card(raw: dict) -> dict | None:
    """Extract title / deal_price / original_price / store from card innerText.

    DealNews card text layout (observed):
        [STAFF PICK\\n][New\\n]<Store> · <when>\\n<Title>\\n$NEW [$OLD]\\n<shipping>\\n...

    Returns a partial deal dict or None if we can't extract the essentials.
    """
    text = (raw.get("text") or "").strip()
    if not text:
        return None
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    # Skip badge lines that some cards carry at the top.
    while lines and lines[0].upper() in ("STAFF PICK", "NEW", "POPULAR"):
        lines.pop(0)
    if len(lines) < 3:
        return None

    # Line 0: "Store · timestamp" — store is the substring before ' · '.
    head = lines[0]
    store = head.split("·")[0].strip() if "·" in head else head
    # We only want Amazon (revenue) — filter aggressively (see AMAZON_ONLY).
    if AMAZON_ONLY and "amazon" not in store.lower():
        return None

    title = lines[1]
    if _is_skip_title(title) or not _is_tech_deal(title):
        return None

    # Find prices on the next ~2 lines (e.g. "$8 $19" or "$60 $150").
    deal_price = original_price = None
    for ln in lines[2:5]:
        nums = [float(p.replace(",", "")) for p in re.findall(r'\$([\d,]+\.?\d*)', ln)]
        nums = [n for n in nums if 0 < n < 50000]
        if not nums:
            continue
        deal_price = min(nums)
        if len(nums) >= 2 and max(nums) > deal_price:
            original_price = max(nums)
        break

    if not deal_price or deal_price < MIN_DEAL_PRICE:
        return None

    discount = calculate_discount(original_price, deal_price) if original_price else 0.0
    if discount and discount > MAX_DISCOUNT_PCT:
        return None  # fake/inflated list price — filter at parse time
    if discount and discount < MIN_DISCOUNT_PCT:
        # Note: keep zero-discount items too — let downstream gates decide.
        # Only drop genuinely-bad discounts (between 0 and MIN, fishy).
        if original_price:
            return None

    return {
        "title": title[:500],
        "deal_price": deal_price,
        "original_price": original_price,
        "discount_pct": discount,
        "retailer": "Amazon",
        "image_url": raw.get("img") or None,
        "source": "DealNews",
    }


def scrape_source(client: PlaywrightClient, source: dict) -> list[dict]:
    """Scrape one aggregator source. Returns deals enriched with affiliate URLs."""
    name = source["name"]
    print(f"  Scraping {name}...")
    try:
        if "dealnews.com" in source["url"]:
            raw_cards = _render_dealnews(client, source["url"])
        else:
            # Other sources can plug in here when added. None today.
            print(f"    Unknown source type for {name}, skipping")
            return []
    except Exception as exc:
        print(f"    Error rendering {name}: {exc}")
        return []

    deals = []
    for raw in raw_cards:
        parsed = _parse_dealnews_card(raw)
        if not parsed:
            continue
        # Resolve the DealNews click-redirect to the real Amazon URL.
        redirect = raw.get("redirect")
        if not redirect:
            continue
        amz = resolve_amazon_url(redirect)
        if not amz:
            continue
        asin = extract_asin(amz)
        if not asin:
            continue

        parsed["asin"] = asin
        parsed["source_url"] = f"https://www.amazon.com/dp/{asin}"
        parsed["affiliate_url"] = build_amazon_url_from_asin(asin)
        deals.append(parsed)
        # Be polite to amazon's redirect resolver.
        time.sleep(0.25)

    with_img = sum(1 for d in deals if d.get("image_url"))
    print(f"    Found {len(deals)} amazon deals ({with_img} with images)")
    return deals


# ── Orchestration (kept compatible with the previous Firecrawl version) ────────

def run_scraper() -> int:
    """Scrape all DEAL_SOURCES via Playwright. Returns count of new deals saved.

    Source-weight orchestration is identical to the prior Firecrawl version so
    tests/test_scraper_source_weights.py keeps passing (it patches scrape_source
    + get_scraper_client). The only behavioral change is per-source SCRAPING —
    Playwright replaces Firecrawl. Free, no API key.
    """
    print("Starting Playwright aggregator scraper...")
    client = get_scraper_client()
    total_new = 0

    try:
        from src.source_tracker import get_source_weights
        weights = get_source_weights()
    except Exception:
        weights = {}

    _BASE_MAX = 10
    sources = sorted(DEAL_SOURCES, key=lambda s: weights.get(s["name"], 1.0), reverse=True)

    try:
        for source in sources:
            weight = weights.get(source["name"], 1.0)
            max_deals = max(3, int(_BASE_MAX * weight))

            deals = scrape_source(client, source)
            deals = deals[:max_deals]

            for deal in deals:
                if save_deal(deal):
                    total_new += 1
            if deals:
                try:
                    from src.source_tracker import record_scraped
                    record_scraped(source["name"], len(deals))
                except Exception:
                    pass
    finally:
        try:
            client.close()
        except Exception:
            pass

    print(f"Scraping complete. {total_new} new deals saved.")
    return total_new
