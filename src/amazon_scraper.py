"""
amazon_scraper.py - Amazon Deal Scraper

Uses Scrapling (StealthyFetcher) for anti-bot bypass + Playwright fallback.
Scrapes Amazon pages, grabs product links/images/prices, builds affiliate URLs.
"""

import re
import os
import time
from config.settings import AMAZON_AFFILIATE_TAG, MIN_DEAL_PRICE, MIN_DISCOUNT_PCT, MAX_DISCOUNT_PCT
from src.database import save_deal

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def _get_category_config(name: str) -> dict:
    """Load category config from built-in CATEGORIES or data/custom_categories.json."""
    from config.categories import CATEGORIES
    import json
    custom = {}
    custom_path = os.path.join(DATA_DIR, "custom_categories.json")
    if os.path.exists(custom_path):
        with open(custom_path) as f:
            custom = json.load(f)
    all_cats = {**CATEGORIES, **custom}
    return all_cats.get(name.lower(), all_cats["tech"])


def _matches_category(title: str, keywords: list) -> bool:
    import re
    lower = title.lower()
    for kw in keywords:
        # Short keywords (3 chars or less) need word boundary matching
        # to avoid false positives like "ram" matching "frame"
        if len(kw) <= 3:
            if re.search(rf'(?<!\w){re.escape(kw)}(?!\w)', lower):
                return True
        else:
            if kw in lower:
                return True
    return False


def _build_affiliate_url(href: str, asin: str) -> tuple[str, str]:
    """Build affiliate URL preserving variation parameters.

    Amazon variation URLs contain params that select the exact product
    variation on deal. We preserve variation-selection params to ensure
    users see the correct product/price.

    Strategy for th param:
    - If the URL has other variation params (psc, smid, etc.), keep th
      because it's selecting a specific variation
    - If th is alone, DROP it — it just triggers the variation matrix
      which defaults to cheapest (often Used/Renewed)

    Returns (source_url, affiliate_url).
    """
    from urllib.parse import urlparse, parse_qs, urlencode

    # Parse the original href to extract variation params
    variation_params = {"psc", "smid"}  # always keep these
    parsed = urlparse(href if href.startswith("http") else f"https://www.amazon.com{href}")
    query = parse_qs(parsed.query)

    # Smart th handling: only keep th if other variation params exist
    has_other_variation = any(k in query for k in variation_params)
    keep_params = variation_params | ({"th"} if has_other_variation else set())
    parsed = urlparse(href if href.startswith("http") else f"https://www.amazon.com{href}")
    query = parse_qs(parsed.query)

    # Build clean param dict: keep variation params + add affiliate tag
    clean_params = {}
    for key in keep_params:
        if key in query:
            clean_params[key] = query[key][0]
    clean_params["tag"] = AMAZON_AFFILIATE_TAG.strip()

    base = f"https://www.amazon.com/dp/{asin}"
    source_url = base
    if any(k in clean_params for k in keep_params):
        source_params = {k: v for k, v in clean_params.items() if k != "tag"}
        source_url = f"{base}?{urlencode(source_params)}" if source_params else base

    affiliate_url = f"{base}?{urlencode(clean_params)}"
    return source_url, affiliate_url


def _scrape_with_scrapling(amazon_pages: list, keywords: list, category_name: str, min_price: float, max_discount: float, min_discount: float = 0.0) -> list:
    """Use Scrapling StealthyFetcher for anti-bot bypass."""
    all_deals = []

    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError:
        print("    Scrapling not available, skipping stealth scraping")
        return []

    for url, name in amazon_pages:
        print(f"    [{name}] Fetching with StealthyFetcher...")
        try:
            def _scroll_page(page):
                """Scroll down to trigger lazy-loaded prices and content."""
                for _ in range(5):
                    page.evaluate("window.scrollBy(0, 800)")
                    page.wait_for_timeout(400)

            page = StealthyFetcher.fetch(
                url,
                headless=True,
                stealthy_headers=True,
                network_idle=True,
                wait=3000,
                page_action=_scroll_page,
            )

            if not page:
                print(f"    [{name}] No response")
                continue

            # Find all product links with /dp/ASIN pattern
            links = page.css('a[href*="/dp/"]')
            print(f"    [{name}] Found {len(links)} product links")

            seen_asins = set()
            for link in links:
                href = link.attrib.get("href", "")
                asin_match = re.search(r'/dp/([A-Z0-9]{10})', href)
                if not asin_match:
                    continue

                asin = asin_match.group(1)
                if asin in seen_asins:
                    continue
                seen_asins.add(asin)

                # Get title from link text or parent
                title = link.text or ""
                if len(title.strip()) < 10:
                    parent = link.parent
                    if parent:
                        title = parent.text or ""
                        # Take first meaningful line
                        lines = [l.strip() for l in title.split('\n') if len(l.strip()) > 15]
                        title = lines[0] if lines else ""

                title = title.strip()[:500]
                if len(title) < 10 or not _matches_category(title, keywords):
                    continue

                # Find prices using Amazon-specific selectors
                card = link.parent
                for _ in range(8):
                    if card and card.parent:
                        card = card.parent
                        # Stop at a reasonable card boundary
                        card_text = card.text or ""
                        if len(card_text) > 50 and '$' in card_text:
                            break

                deal_price = None
                original_price = None

                if card:
                    # 1. Try Amazon's structured price elements
                    # Current/deal price: .a-price:not([data-a-strike]) .a-offscreen
                    current_prices = card.css('.a-price:not([data-a-strike]) .a-offscreen')
                    for cp in current_prices:
                        txt = cp.text or ""
                        m = re.search(r'\$[\d,]+\.?\d*', txt)
                        if m:
                            val = float(m.group().replace('$', '').replace(',', ''))
                            if 0 < val < 50000:
                                deal_price = val
                                break

                    # Original/list price: .a-price[data-a-strike] .a-offscreen or .a-text-price .a-offscreen
                    orig_selectors = [
                        '.a-price[data-a-strike] .a-offscreen',
                        '.a-text-price .a-offscreen',
                    ]
                    for sel in orig_selectors:
                        orig_prices = card.css(sel)
                        for op in orig_prices:
                            txt = op.text or ""
                            m = re.search(r'\$[\d,]+\.?\d*', txt)
                            if m:
                                val = float(m.group().replace('$', '').replace(',', ''))
                                if 0 < val < 50000:
                                    original_price = val
                                    break
                        if original_price:
                            break

                    # 2. Fallback: regex on card text, but be smarter
                    if not deal_price:
                        context_text = card.text or ""
                        price_matches = re.findall(r'\$[\d,]+\.?\d*', context_text)
                        prices = []
                        for p in price_matches:
                            val = float(p.replace('$', '').replace(',', ''))
                            if 0 < val < 50000:
                                prices.append(val)
                        if prices:
                            deal_price = min(prices)
                            if len(prices) > 1 and max(prices) != min(prices):
                                candidate_orig = max(prices)
                                # Only accept original if it's within 3x of deal (avoid unrelated prices)
                                if candidate_orig <= deal_price * 2:
                                    original_price = candidate_orig

                if not deal_price:
                    continue

                # Sanity check: original should be higher than deal
                if original_price and original_price <= deal_price:
                    original_price = None

                if deal_price < min_price:
                    continue

                discount = 0.0
                if original_price and original_price > deal_price:
                    discount = round(((original_price - deal_price) / original_price) * 100, 2)

                # Check for discount text as fallback
                context_text = card.text or "" if card else ""
                if discount == 0:
                    disc_match = re.search(r'(\d+)%\s*off', context_text, re.IGNORECASE)
                    if disc_match:
                        discount = float(disc_match.group(1))

                if discount > max_discount:
                    continue

                if discount < min_discount:
                    continue

                # Find image
                image_url = None
                img_parent = link.parent
                for _ in range(5):
                    if img_parent:
                        imgs = img_parent.css('img[src*="images-amazon"], img[src*="m.media-amazon"]')
                        if imgs:
                            image_url = imgs[0].attrib.get("src")
                            break
                        img_parent = img_parent.parent

                # Upgrade thumbnail to larger image
                if image_url and "._" in image_url:
                    image_url = re.sub(r'\._[^.]+\.', '._AC_SL500_.', image_url)

                source_url, affiliate_url = _build_affiliate_url(href, asin)

                # Star rating from hidden a-icon-alt text
                star_rating = None
                for el in (card.css('.a-icon-alt') if card else []):
                    m = re.search(r'([\d.]+)\s+out of\s+5', el.text or "")
                    if m:
                        star_rating = float(m.group(1))
                        break

                # Review count
                review_count = None
                for sel in ['.a-size-base.s-underline-text', '.a-size-small']:
                    for el in (card.css(sel) if card else []):
                        t = re.sub(r'[^\d]', '', el.text or "")
                        if t and 9 < int(t) < 10_000_000:
                            review_count = int(t)
                            break
                    if review_count:
                        break

                all_deals.append({
                    "title": title,
                    "asin": asin,
                    "original_price": original_price,
                    "deal_price": deal_price,
                    "discount_pct": discount,
                    "retailer": "Amazon",
                    "source_url": source_url,
                    "affiliate_url": affiliate_url,
                    "image_url": image_url,
                    "coupon_code": None,
                    "extra_savings": None,
                    "category": category_name,
                    "star_rating": star_rating,
                    "review_count": review_count,
                })

            print(f"    [{name}] Extracted {len(all_deals)} {category_name} deals so far")

        except Exception as e:
            print(f"    [{name}] Error: {e}")

    return all_deals


def _scrape_with_playwright(amazon_pages: list, keywords: list, category_name: str, min_price: float, max_discount: float, min_discount: float = 0.0) -> list:
    """Fallback: use Playwright if Scrapling fails."""
    all_deals = []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("    Playwright not available")
        return []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        for url, name in amazon_pages:
            try:
                print(f"    [{name}] Playwright fallback...")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)

                for _ in range(5):
                    page.evaluate("window.scrollBy(0, 800)")
                    time.sleep(0.5)

                # Save screenshot for debugging
                os.makedirs(DATA_DIR, exist_ok=True)
                safe = name.lower().replace(" ", "_")
                page.screenshot(path=os.path.join(DATA_DIR, f"debug_{safe}.png"))

                raw = page.evaluate("""() => {
                    const results = [];
                    const links = document.querySelectorAll('a[href*="/dp/"]');
                    const seen = new Set();
                    for (const link of links) {
                        const href = link.getAttribute('href') || '';
                        const m = href.match(/\\/dp\\/([A-Z0-9]{10})/);
                        if (!m) continue;
                        const asin = m[1];
                        if (seen.has(asin)) continue;
                        seen.add(asin);
                        let card = link;
                        for (let i = 0; i < 8; i++) {
                            if (card.parentElement) card = card.parentElement;
                            if (card.offsetHeight > 200) break;
                        }
                        const text = card.innerText || '';
                        let title = link.innerText?.trim() || '';
                        if (title.length < 10) {
                            const lines = text.split('\\n').filter(l => l.trim().length > 15);
                            title = lines[0]?.trim() || '';
                        }
                        const imgs = card.querySelectorAll('img');
                        let imgUrl = null;
                        for (const img of imgs) {
                            const src = img.getAttribute('src') || '';
                            if (src.includes('images-amazon') || src.includes('m.media-amazon')) {
                                imgUrl = src; break;
                            }
                        }

                        // Smart price extraction using Amazon's CSS structure
                        let dealPrice = null;
                        let origPrice = null;

                        // 1. Current price from .a-price (not strikethrough)
                        const currentEls = card.querySelectorAll('.a-price:not([data-a-strike]) .a-offscreen');
                        for (const el of currentEls) {
                            const pm = el.textContent.match(/\\$[\\d,]+\\.?\\d*/);
                            if (pm) { dealPrice = parseFloat(pm[0].replace('$','').replace(',','')); break; }
                        }

                        // 2. Original price from strikethrough or list price
                        const origEls = card.querySelectorAll('.a-price[data-a-strike] .a-offscreen, .a-text-price .a-offscreen');
                        for (const el of origEls) {
                            const pm = el.textContent.match(/\\$[\\d,]+\\.?\\d*/);
                            if (pm) { origPrice = parseFloat(pm[0].replace('$','').replace(',','')); break; }
                        }

                        // 3. Fallback: regex on text but with sanity checks
                        if (!dealPrice) {
                            const pm = text.match(/\\$[\\d,]+\\.?\\d*/g) || [];
                            const prices = pm.map(p => parseFloat(p.replace('$','').replace(',',''))).filter(p => p > 0 && p < 50000);
                            if (prices.length > 0) {
                                dealPrice = Math.min(...prices);
                                if (prices.length > 1) {
                                    const maxP = Math.max(...prices);
                                    // Only use max as original if within 3x (avoids unrelated prices)
                                    if (maxP !== dealPrice && maxP <= dealPrice * 3) {
                                        origPrice = maxP;
                                    }
                                }
                            }
                        }

                        // Sanity: original must be higher than deal
                        if (origPrice && origPrice <= dealPrice) origPrice = null;

                        // Star rating from hidden a-icon-alt text
                        let starRating = null;
                        for (const el of card.querySelectorAll('.a-icon-alt')) {
                            const m = (el.textContent || '').match(/([\\d.]+)\\s+out of\\s+5/);
                            if (m) { starRating = parseFloat(m[1]); break; }
                        }

                        // Review count
                        let reviewCount = null;
                        for (const sel of ['.a-size-base.s-underline-text', '.a-size-small', '[aria-label*="ratings"]']) {
                            for (const el of card.querySelectorAll(sel)) {
                                const t = (el.textContent || '').replace(/[^\\d]/g, '');
                                const n = parseInt(t);
                                if (n > 9 && n < 10000000) { reviewCount = n; break; }
                            }
                            if (reviewCount) break;
                        }

                        const dm = text.match(/(\\d+)%\\s*off/i);
                        results.push({ asin, href, title, imgUrl, dealPrice, origPrice, disc: dm ? parseInt(dm[1]) : 0, starRating, reviewCount });
                    }
                    return results;
                }""")

                for r in raw:
                    title = r.get("title", "").strip()
                    if len(title) < 10 or not _matches_category(title, keywords):
                        continue
                    deal_price = r.get("dealPrice")
                    if not deal_price:
                        continue
                    if deal_price < min_price:
                        continue
                    original_price = r.get("origPrice")
                    discount = 0.0
                    if original_price and original_price > deal_price:
                        discount = round(((original_price - deal_price) / original_price) * 100, 2)
                    elif r.get("disc", 0) > 0:
                        discount = float(r["disc"])
                    if discount > max_discount:
                        continue
                    if discount < min_discount:
                        continue
                    asin = r["asin"]
                    href = r.get("href", "")
                    image_url = r.get("imgUrl")
                    if image_url and "._" in image_url:
                        image_url = re.sub(r'\._[^.]+\.', '._AC_SL500_.', image_url)
                    source_url, affiliate_url = _build_affiliate_url(href, asin)
                    all_deals.append({
                        "title": title[:500],
                        "asin": asin,
                        "original_price": original_price,
                        "deal_price": deal_price,
                        "discount_pct": discount,
                        "retailer": "Amazon",
                        "source_url": source_url,
                        "affiliate_url": affiliate_url,
                        "image_url": image_url,
                        "coupon_code": None,
                        "extra_savings": None,
                        "category": category_name,
                        "star_rating": r.get("starRating"),
                        "review_count": r.get("reviewCount"),
                    })

            except Exception as e:
                print(f"    [{name}] Playwright error: {e}")

        browser.close()

    return all_deals


def scrape_amazon_deals(category_name: str = "tech", fast_track: bool = False) -> list:
    """Playwright primary, Scrapling as supplementary. Error-resilient."""
    cat = _get_category_config(category_name)
    amazon_pages = cat.get("fast_track_urls", cat.get("amazon_urls", [])) if fast_track else cat.get("amazon_urls", [])
    keywords = cat.get("keywords", [])
    min_price = cat.get("min_price", MIN_DEAL_PRICE)
    max_discount = cat.get("max_discount", MAX_DISCOUNT_PCT)
    min_discount = cat.get("min_discount", MIN_DISCOUNT_PCT)

    if fast_track:
        print(f"  [fast-track] Quick check on {len(amazon_pages)} high-yield pages...")

    if not amazon_pages:
        print(f"  No Amazon URLs configured for category '{category_name}'")
        return []

    deals = []

    # Playwright primary — renders JS so Amazon prices/titles are visible
    print(f"  Scraping with Playwright for category '{category_name}'...")
    try:
        deals = _scrape_with_playwright(amazon_pages, keywords, category_name, min_price, max_discount, min_discount)
    except Exception as e:
        print(f"  Playwright crashed: {e}")

    # Scrapling supplementary — may find extra deals Playwright missed
    if not deals:
        print("  Playwright got 0, trying Scrapling fallback...")
    else:
        print(f"  Playwright found {len(deals)}, trying Scrapling for extras...")
    try:
        extra = _scrape_with_scrapling(amazon_pages, keywords, category_name, min_price, max_discount, min_discount)
        if extra:
            seen_asins = {d["asin"] for d in deals}
            new_extras = [d for d in extra if d["asin"] not in seen_asins]
            if new_extras:
                print(f"  Scrapling added {len(new_extras)} extra deals")
                deals.extend(new_extras)
    except Exception as e:
        print(f"  Scrapling crashed: {e}")

    if not deals:
        print("  WARNING: Both scrapers returned 0 deals. Amazon may be blocking.")

    # Deduplicate by ASIN
    seen = set()
    unique = []
    for d in deals:
        if d["asin"] not in seen:
            seen.add(d["asin"])
            unique.append(d)

    return unique


def fetch_product_rating(asin: str) -> tuple[float | None, int | None]:
    """Fetch star rating and review count from an Amazon product page.

    Used as a fallback for deals sourced from aggregators (Firecrawl) that
    don't go through the listing-page scrapers. Returns (None, None) on failure
    so callers can treat the signal as optional.
    """
    import requests as _req
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        resp = _req.get(
            f"https://www.amazon.com/dp/{asin}",
            headers=headers,
            timeout=8,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return None, None
        html = resp.text

        star_rating = None
        m = re.search(r'([\d.]+) out of 5 stars', html)
        if m:
            star_rating = float(m.group(1))

        review_count = None
        m = re.search(r'([\d,]+)\s+(?:global\s+)?(?:customer\s+)?ratings', html)
        if m:
            review_count = int(m.group(1).replace(",", ""))

        return star_rating, review_count
    except Exception:
        return None, None


def run_amazon_scraper(category_name: str = "tech", fast_track: bool = False) -> int:
    """Scrape Amazon deals for a category and save to database. Records price history."""
    from src.database import record_price

    deals = scrape_amazon_deals(category_name=category_name, fast_track=fast_track)

    saved = 0
    for deal in deals:
        # Record price even if deal is duplicate (builds history)
        if deal.get("asin") and deal.get("deal_price"):
            record_price(deal["asin"], deal["deal_price"])

        # Fill missing ratings via product page fetch (best-effort, silent on fail)
        if deal.get("star_rating") is None and deal.get("asin"):
            rating, count = fetch_product_rating(deal["asin"])
            if rating:
                deal["star_rating"] = rating
                deal["review_count"] = count

        if save_deal(deal):
            saved += 1
            lowest_tag = " [LOWEST EVER]" if deal.get("is_lowest_ever") else ""
            rating_tag = f" | {deal['star_rating']}★ ({deal.get('review_count', 0):,})" if deal.get("star_rating") else ""
            print(f"    Saved: ${deal['deal_price']:.0f} | {deal['title'][:50]}{lowest_tag}{rating_tag}")

    print(f"  {saved} new Amazon deals saved ({len(deals)} found total)")
    return saved
