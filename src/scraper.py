"""
scraper.py - Quantdeal Scraper Module

Uses Firecrawl API to scrape deal websites and extract structured deal data
from the returned markdown. Filters for Amazon product links and appends
the affiliate tag for commission tracking.
"""

import re
import requests
from firecrawl import FirecrawlApp
from config.settings import (
    FIRECRAWL_API_KEY, DEAL_SOURCES, AMAZON_AFFILIATE_TAG,
    MIN_DEAL_PRICE, MIN_DISCOUNT_PCT, MAX_DISCOUNT_PCT, TECH_KEYWORDS, AMAZON_ONLY,
)
from src.database import save_deal


def resolve_amazon_url(url: str) -> str | None:
    """
    Follow redirects on aggregator URLs to find the real Amazon URL.
    Returns the Amazon URL if found, None otherwise.
    """
    if "amazon.com" in url:
        return url
    try:
        resp = requests.head(url, allow_redirects=True, timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"})
        final_url = resp.url
        if "amazon.com" in final_url:
            return final_url
        # Some redirects need a GET
        resp = requests.get(url, allow_redirects=True, timeout=10,
                            headers={"User-Agent": "Mozilla/5.0"},
                            stream=True)
        if "amazon.com" in resp.url:
            return resp.url
    except Exception:
        pass
    return None


def extract_asin(url: str) -> str | None:
    """Extract Amazon ASIN from a URL."""
    patterns = [
        r'/dp/([A-Z0-9]{10})',
        r'/gp/product/([A-Z0-9]{10})',
        r'/product/([A-Z0-9]{10})',
        r'/ASIN/([A-Z0-9]{10})',
    ]
    for pat in patterns:
        match = re.search(pat, url)
        if match:
            return match.group(1)
    return None


def build_amazon_url_from_asin(asin: str) -> str:
    """Build a clean Amazon affiliate URL from an ASIN."""
    return f"https://www.amazon.com/dp/{asin}?tag={AMAZON_AFFILIATE_TAG}"


def get_firecrawl_client() -> FirecrawlApp:
    """Initialize Firecrawl client."""
    if not FIRECRAWL_API_KEY:
        raise ValueError("FIRECRAWL_API_KEY not set in .env")
    return FirecrawlApp(api_key=FIRECRAWL_API_KEY)


def extract_price(text: str) -> float | None:
    """Extract a numeric price from a string like '$299.99' or '299.99'."""
    if not text:
        return None
    match = re.search(r'\$?([\d,]+\.?\d*)', str(text))
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def calculate_discount(original: float, deal: float) -> float:
    """Calculate discount percentage."""
    if original and original > 0 and deal < original:
        return round(((original - deal) / original) * 100, 2)
    return 0.0


def build_affiliate_url(url: str) -> str:
    """Append Amazon Associates tag for Amazon links. Others returned as-is."""
    if "amazon.com" in url:
        url = re.sub(r'[?&]tag=[^&]*', '', url)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}tag={AMAZON_AFFILIATE_TAG}"
    return url


def _is_image_url(url: str) -> bool:
    """Check if URL points to an image/asset rather than a deal page."""
    image_indicators = [
        'datocms-assets.com', 'imgur.com', 'cloudfront.net',
        'slickdealscdn.com', 'gravatar.com',
        '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg',
    ]
    return any(ind in url.lower() for ind in image_indicators)


def _is_skip_title(title: str) -> bool:
    """Check if title is navigation/non-deal text or a store-wide sale link."""
    skip_keywords = [
        'sign in', 'sign up', 'login', 'register', 'menu', 'cart',
        'skip to', 'cookie', 'privacy', 'terms', 'about us',
        'get the extension', 'go to last post',
        'interests', 'categories', 'see all', 'view all',
        'load more', 'show more', 'next page', 'previous',
        'sort by', 'filter', 'subscribe', 'newsletter',
        'logo', 'icon', 'avatar',
    ]
    lower = title.lower()
    if any(kw in lower for kw in skip_keywords):
        return True

    # Skip store-wide / vague sale links (not specific products)
    store_wide_patterns = [
        r'up to \d+% off',
        r'savings at\b',
        r'clearance',
        r'sale:?\s',
        r'deals at\b',
        r'deals on\b',
        r'shop .+ at\b',
        r'top \d+ deals',
        r'best .+ deals',
        r'save on\b',
        r'^deals\b',
        r'gift guide',
        r'price drop',
    ]
    if any(re.search(pat, lower) for pat in store_wide_patterns):
        return True

    # Skip vague titles (less than 3 words after cleaning)
    clean = re.sub(r'(image for|offer for|deal for)\s*', '', lower, flags=re.IGNORECASE)
    if len(clean.split()) < 3:
        return True
    return False


def _is_tech_deal(title: str) -> bool:
    """Check if title matches tech/electronics keywords."""
    import re
    lower = title.lower()
    for kw in TECH_KEYWORDS:
        if len(kw) <= 3:
            if re.search(rf'(?<!\w){re.escape(kw)}(?!\w)', lower):
                return True
        else:
            if kw in lower:
                return True
    return False


def parse_deals_from_markdown(markdown: str, source_name: str) -> list:
    """
    Parse deal information from Firecrawl markdown output.

    Two-pass approach:
    1. First pass: collect image URLs and map them to nearby deal titles
    2. Second pass: extract deals from non-image links, attach images
    """
    deals = []
    if not markdown:
        return deals

    # Find all markdown links
    link_pattern = re.compile(
        r'\[([^\]]{3,300})\]\((https?://[^\)]+)\)',
        re.MULTILINE
    )

    all_links = list(link_pattern.finditer(markdown))

    # First pass: collect image URLs with their position in the markdown
    image_entries = []  # list of (position, title_key, image_url)
    for match in all_links:
        title = match.group(1).strip()
        url = match.group(2).strip()

        if _is_image_url(url):
            clean = re.sub(r'^image for\s+', '', title, flags=re.IGNORECASE)
            key = clean[:40].lower().strip()
            if key:
                image_entries.append((match.start(), key, url))

    def _find_image(deal_title: str, deal_position: int) -> str | None:
        """Find best matching image by title similarity or proximity."""
        title_key = deal_title[:40].lower().strip()

        # Try exact title match first
        for _, img_key, img_url in image_entries:
            if img_key == title_key:
                return img_url

        # Try partial match (first 20 chars)
        short_key = title_key[:20]
        for _, img_key, img_url in image_entries:
            if img_key[:20] == short_key:
                return img_url

        # Try word overlap match (at least 3 words in common)
        deal_words = set(title_key.split())
        for _, img_key, img_url in image_entries:
            img_words = set(img_key.split())
            common = deal_words & img_words
            if len(common) >= 3:
                return img_url

        # Fallback: closest image that appears BEFORE this deal (within 500 chars)
        best = None
        best_dist = 500
        for pos, _, img_url in image_entries:
            dist = deal_position - pos
            if 0 < dist < best_dist:
                best = img_url
                best_dist = dist

        return best

    # Second pass: extract actual deals (non-image links)
    for match in all_links:
        title = match.group(1).strip()
        url = match.group(2).strip()

        # Skip image URLs
        if _is_image_url(url):
            continue

        # Skip navigation
        if _is_skip_title(title):
            continue

        # Skip pagination/filter URLs
        if '?pf=' in url or '?page=' in url:
            continue

        # Clean common prefixes
        title = re.sub(r'^(Offer for|Deal for|Image for)\s+', '', title, flags=re.IGNORECASE)

        # Try to recover truncated titles from context after the link
        context_start = match.end()
        context_after = markdown[context_start:context_start + 300]

        if context_after and title and title[-1] not in '.!?)':
            continuation = re.match(r'^([^[\n\|]{1,100})', context_after)
            if continuation:
                extra = continuation.group(1).strip()
                if extra and not extra.startswith('$') and not extra.startswith('#'):
                    title = (title + extra).strip(' |')

        # Look for prices in surrounding context
        context = context_after[:200]
        title_and_context = title + " " + context

        # Find all dollar amounts
        prices = re.findall(r'\$[\d,]+\.?\d*', title_and_context)
        if not prices:
            continue

        price_values = []
        for p in prices:
            val = extract_price(p)
            if val and val > 0:
                price_values.append(val)

        if not price_values:
            continue

        deal_price = min(price_values)
        original_price = max(price_values) if len(price_values) > 1 else None

        if deal_price < MIN_DEAL_PRICE or deal_price > 50000:
            continue

        if original_price and original_price == deal_price:
            original_price = None

        discount = calculate_discount(original_price, deal_price) if original_price else 0.0

        # Discount sanity check — absurd discounts are parsing errors
        if discount > MAX_DISCOUNT_PCT:
            continue

        # Skip deals with no meaningful discount
        if discount < MIN_DISCOUNT_PCT:
            continue

        # Clean up title
        clean_title = re.sub(r'\$[\d,]+\.?\d*', '', title).strip()
        clean_title = re.sub(r'\s+', ' ', clean_title).strip(' -+|:')
        if len(clean_title) < 10:
            clean_title = title

        # Tech-only filter — skip non-tech deals
        if not _is_tech_deal(clean_title):
            continue

        # Match an image using fuzzy title matching or proximity
        image_url = _find_image(clean_title, match.start())

        # Extract coupon codes from surrounding context
        coupon_code = None
        coupon_patterns = re.findall(
            r'(?:code|coupon|promo|clip coupon|use code)[:\s]+["\']?([A-Z0-9]{3,20})["\']?',
            title_and_context, re.IGNORECASE
        )
        if coupon_patterns:
            coupon_code = coupon_patterns[0].upper()

        # Check for "clip coupon" or "extra savings" mentions
        extra_savings = None
        savings_match = re.search(
            r'(clip \w+ coupon|extra \d+% off|additional \d+% off|save extra \$?\d+|checkout code)',
            title_and_context, re.IGNORECASE
        )
        if savings_match:
            extra_savings = savings_match.group(0).strip()

        # Try to find a direct Amazon link in nearby context
        deal_url = url
        amazon_match = re.search(
            r'(https?://(?:www\.)?amazon\.com/[^\s\)"\'>]+)',
            title_and_context
        )
        if amazon_match:
            deal_url = amazon_match.group(1)

        # Check if this is an Amazon deal (URL, title, or source page)
        mentions_amazon = (
            "amazon.com" in deal_url
            or re.search(r'\bamazon\b', title_and_context, re.IGNORECASE)
            or "amazon" in source_name.lower()
        )

        # Amazon-only mode: skip deals not related to Amazon
        if AMAZON_ONLY and not mentions_amazon:
            continue

        # If URL is an aggregator redirect, follow it to get real Amazon URL
        if "amazon.com" not in deal_url and mentions_amazon:
            resolved = resolve_amazon_url(deal_url)
            if resolved:
                deal_url = resolved

        # Extract ASIN and build clean affiliate link
        asin = extract_asin(deal_url)
        if asin:
            affiliate_url = build_amazon_url_from_asin(asin)
        else:
            affiliate_url = build_affiliate_url(deal_url)

        # Final check: in Amazon-only mode, must have an Amazon URL
        if AMAZON_ONLY and "amazon.com" not in deal_url and not asin:
            continue

        deals.append({
            "title": clean_title[:500],
            "original_price": original_price,
            "deal_price": deal_price,
            "discount_pct": discount,
            "retailer": "Amazon",
            "source_url": f"https://www.amazon.com/dp/{asin}" if asin else deal_url,
            "affiliate_url": affiliate_url,
            "image_url": image_url,
            "coupon_code": coupon_code,
            "extra_savings": extra_savings,
            "category": "tech",
            "source": source_name,  # which site this deal came from
            "asin": asin,
        })

    # Deduplicate by URL and by title (first 40 chars)
    seen_urls = set()
    seen_titles = set()
    unique_deals = []
    for deal in deals:
        title_key = deal["title"][:40].lower().strip()
        if deal["source_url"] not in seen_urls and title_key not in seen_titles:
            seen_urls.add(deal["source_url"])
            seen_titles.add(title_key)
            unique_deals.append(deal)

    return unique_deals


def fetch_screenshot(client: FirecrawlApp, url: str) -> str | None:
    """Use Firecrawl to screenshot a deal page as image fallback."""
    try:
        result = client.scrape(url, formats=["screenshot"])

        screenshot = None
        if isinstance(result, dict):
            screenshot = result.get("screenshot")
        elif hasattr(result, "screenshot"):
            screenshot = result.screenshot

        if screenshot:
            print(f"      Got screenshot for {url[:60]}")
            return screenshot
    except Exception as e:
        print(f"      Screenshot failed for {url[:60]}: {e}")
    return None


def scrape_source(client: FirecrawlApp, source: dict) -> list:
    """Scrape deals from a single source using Firecrawl."""
    print(f"  Scraping {source['name']}...")
    try:
        # Request both markdown and screenshot of the main page
        result = client.scrape(source["url"], formats=["markdown", "screenshot"])

        markdown = ""
        page_screenshot = None
        if isinstance(result, dict):
            markdown = result.get("markdown", "")
            page_screenshot = result.get("screenshot")
        elif hasattr(result, "markdown"):
            markdown = result.markdown or ""
            page_screenshot = getattr(result, "screenshot", None)

        if not markdown:
            print(f"    No markdown content returned for {source['name']}")
            return []

        deals = parse_deals_from_markdown(markdown, source["name"])

        # For deals missing images, try screenshot fallbacks
        for deal in deals:
            if not deal["image_url"]:
                # First fallback: use the main page screenshot
                if page_screenshot:
                    deal["image_url"] = page_screenshot
                    print(f"    Using page screenshot for: {deal['title'][:50]}")
                else:
                    # Second fallback: screenshot the individual deal URL
                    screenshot = fetch_screenshot(client, deal["source_url"])
                    if screenshot:
                        deal["image_url"] = screenshot

        with_img = sum(1 for d in deals if d["image_url"])
        print(f"    Found {len(deals)} deals ({with_img} with images)")
        return deals

    except Exception as e:
        print(f"    Error scraping {source['name']}: {e}")
        return []


def run_scraper() -> int:
    """Scrape all sources and save to DB. Returns count of new deals saved."""
    print("Starting Firecrawl scraper...")
    client = get_firecrawl_client()
    total_new = 0

    # Weight sources by past performance — better sources get scraped first
    # and contribute more deals. New sources (no data yet) get neutral weight 1.0.
    try:
        from src.source_tracker import get_source_weights
        weights = get_source_weights()
    except Exception:
        weights = {}

    _BASE_MAX = 10  # deals processed per source at neutral weight (1.0)
    sources = sorted(DEAL_SOURCES, key=lambda s: weights.get(s["name"], 1.0), reverse=True)

    for source in sources:
        weight = weights.get(source["name"], 1.0)
        max_deals = max(3, int(_BASE_MAX * weight))  # floor of 3 — never starve a source

        deals = scrape_source(client, source)
        deals = deals[:max_deals]  # apply weight-based cap

        source_new = 0
        for deal in deals:
            if save_deal(deal):
                source_new += 1
                total_new += 1
        # Track how many deals each source produced
        if deals:
            try:
                from src.source_tracker import record_scraped
                record_scraped(source["name"], len(deals))
            except Exception:
                pass

    print(f"Scraping complete. {total_new} new deals saved.")
    return total_new
