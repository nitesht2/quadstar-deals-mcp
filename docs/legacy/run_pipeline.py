#!/usr/bin/env python3
"""
run_pipeline.py — QuadStar Deals Scraper/Poster Wrapper

Scrapes Amazon tech deals via Firecrawl v2 HTTP API, scores them,
builds tweets, posts via Postiz. Compatible with v2/v3 pipeline.

Usage:
    python3 run_pipeline.py              # full pipeline
    python3 run_pipeline.py --dry-run    # score but don't post
"""

import json, os, re, sys, time, random, argparse
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

from config.settings import (
    FIRECRAWL_API_KEY, AMAZON_AFFILIATE_TAG,
    MIN_DEAL_PRICE, MIN_DISCOUNT_PCT, MAX_DISCOUNT_PCT,
    TECH_KEYWORDS, BRAND_TIER_1, BRAND_TIER_2,
    DATA_DIR, PIPELINE_MIN_DISCOUNT, PIPELINE_MIN_SCORE,
    PIPELINE_MAX_DAILY_POSTS, POSTIZ_API_URL, POSTIZ_API_KEY,
    PLATFORM_IDS, DEAL_SOURCES,
    SCORE_WEIGHT_DISCOUNT, SCORE_WEIGHT_BRAND,
    SCORE_WEIGHT_PRICE_RANGE, SCORE_WEIGHT_BADGE,
    SCORE_WEIGHT_FRESHNESS, SCORE_WEIGHT_TRENDING,
)

DEALS_FILE = os.path.join(DATA_DIR, "deals.json")
BUDGET_FILE = os.path.join(DATA_DIR, "budget.json")
MAX_BUDGET = 4.00


# ═══════════════════════════════════════════════════════════════════════════════
# Firecrawl v2 HTTP API
# ═══════════════════════════════════════════════════════════════════════════════

def _firecrawl_scrape(url):
    """Scrape a URL via Firecrawl v2 HTTP API. Returns markdown text or empty string."""
    if not FIRECRAWL_API_KEY:
        return ""
    payload = json.dumps({
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True
    }).encode()
    req = __import__("urllib.request", fromlist=["Request"]).Request(
        "https://api.firecrawl.dev/v2/scrape",
        data=payload,
        headers={
            "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    try:
        with __import__("urllib.request", fromlist=["urlopen"]).urlopen(req, timeout=60) as r:
            result = json.loads(r.read().decode("utf-8", errors="replace"))
        if result.get("success") and isinstance(result.get("data"), dict):
            return result["data"].get("markdown", "")
        if isinstance(result.get("data"), str):
            return result["data"]
        return result.get("markdown", "") if "markdown" in result else ""
    except Exception as e:
        print(f"  [Firecrawl ERROR] {e}")
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# Scraper functions
# ═══════════════════════════════════════════════════════════════════════════════

def _skip_title(title):
    skip = ["sign in","sign up","login","menu","cart","skip to","cookie","privacy",
            "terms","about us","subscribe","home","browse","category","see all",
            "view all","today's deals","deal of the day","lightning deal",
            "best sellers","most wished","new releases"]
    t = title.lower().strip()
    return any(s in t for s in skip) or len(t) < 15


def _extract_asin(url):
    for pat in [r'/dp/([A-Z0-9]{10})', r'/gp/product/([A-Z0-9]{10})',
                r'/product/([A-Z0-9]{10})', r'/ASIN/([A-Z0-9]{10})']:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _extract_price(text):
    if not text:
        return None
    m = re.search(r'\$?([\d,]+\.?\d*)', str(text))
    return float(m.group(1).replace(",", "")) if m else None


def _parse_deal(title, url, source):
    """Parse a deal from title and URL."""
    if not title or _skip_title(title):
        return None
    title = title.strip()
    asin = _extract_asin(url) if url else None
    prices = re.findall(r'\$([\d,]+\.?\d*)', title)
    deal_price = float(prices[0].replace(",", "")) if prices else None
    original_price = float(prices[1].replace(",", "")) if len(prices) > 1 else None
    discount_m = re.search(r'(\d{1,2})\s*%\s*(?:off|discount)', title, re.IGNORECASE)
    discount_pct = float(discount_m.group(1)) if discount_m else None

    if deal_price and deal_price < MIN_DEAL_PRICE:
        return None
    if discount_pct and (discount_pct < MIN_DISCOUNT_PCT or discount_pct > MAX_DISCOUNT_PCT):
        return None
    if _has_tech(title):
        return {
            "title": title[:200], "url": url, "asin": asin,
            "deal_price": deal_price, "original_price": original_price,
            "discount_pct": discount_pct, "source": source,
            "affiliate_url": _make_aff(asin) if asin else url,
        }
    return None


def _make_aff(asin):
    return f"https://www.amazon.com/dp/{asin}?tag={AMAZON_AFFILIATE_TAG}"


def _has_tech(title):
    t = title.lower()
    return any(kw in t for kw in TECH_KEYWORDS)


def fetch_slickdeals():
    results = []
    try:
        md = _firecrawl_scrape("https://slickdeals.net/deals/amazon/")
        if not md:
            return results
        links = re.findall(r'\[([^\]]{20,120})\]\(https?://[^\)]+amazon\.com[^\)]*\)', md)
        urls = re.findall(r'https?://[^\)\s]+amazon\.com[^\)\s]*', md)
        for i, title in enumerate(links[:15]):
            url = urls[i] if i < len(urls) else ""
            if title and not _skip_title(title):
                deal = _parse_deal(title, url, "Slickdeals")
                if deal:
                    results.append(deal)
    except Exception as e:
        print(f"  [Slickdeals ERROR] {e}")
    return results


def fetch_techbargains():
    results = []
    try:
        md = _firecrawl_scrape("https://www.techbargain.com/")
        if not md:
            return results
        links = re.findall(r'\[([^\]]{20,120})\]\(https?://[^\)]+amazon\.com[^\)]*\)', md)
        urls = re.findall(r'https?://[^\)\s]+amazon\.com[^\)\s]*', md)
        for i, title in enumerate(links[:10]):
            url = urls[i] if i < len(urls) else ""
            if title and not _skip_title(title):
                deal = _parse_deal(title, url, "TechBargains")
                if deal:
                    results.append(deal)
    except Exception as e:
        print(f"  [TechBargains ERROR] {e}")
    return results


def fetch_camel(browser_key=None):
    if not FIRECRAWL_API_KEY:
        return []
    results = []
    try:
        md = _firecrawl_scrape("https://camelcamelcamel.com/top_drops/electronics")
        if not md:
            return results
        links = re.findall(r'\[([^\]]{20,120})\]\(https?://[^\)]+amazon\.com[^\)]*\)', md)
        urls = re.findall(r'https?://[^\)\s]+amazon\.com[^\)\s]*', md)
        for i, title in enumerate(links[:10]):
            url = urls[i] if i < len(urls) else ""
            if title and not _skip_title(title):
                deal = _parse_deal(title, url, "CamelCamelCamel")
                if deal:
                    results.append(deal)
    except Exception as e:
        print(f"  [Camel ERROR] {e}")
    return results


def fetch_amz_bestsellers(browser_key=None):
    if not FIRECRAWL_API_KEY:
        return []
    results = []
    try:
        md = _firecrawl_scrape("https://www.amazon.com/best-sellers-electronics/zgbs/electronics/")
        if not md:
            return results
        links = re.findall(r'\[([^\]]{20,120})\]\(https?://[^\)]+/dp/[A-Z0-9]{10}[^\)]*\)', md)
        urls = re.findall(r'https?://[^\)\s]+/dp/[A-Z0-9]{10}[^\)\s]*', md)
        for i, title in enumerate(links[:10]):
            url = urls[i] if i < len(urls) else ""
            if title and not _skip_title(title):
                deal = _parse_deal(title, url, "Amazon")
                if deal:
                    results.append(deal)
    except Exception as e:
        print(f"  [AmzBestSellers ERROR] {e}")
    return results


def fetch_amz_deals(browser_key=None):
    if not FIRECRAWL_API_KEY:
        return []
    results = []
    try:
        md = _firecrawl_scrape("https://www.amazon.com/deals?ref_=nav_cs_gb")
        if not md:
            return results
        links = re.findall(r'\[([^\]]{20,120})\]\(https?://[^\)]+/dp/[A-Z0-9]{10}[^\)]*\)', md)
        urls = re.findall(r'https?://[^\)\s]+/dp/[A-Z0-9]{10}[^\)\s]*', md)
        for i, title in enumerate(links[:10]):
            url = urls[i] if i < len(urls) else ""
            if title and not _skip_title(title):
                deal = _parse_deal(title, url, "Amazon")
                if deal:
                    results.append(deal)
    except Exception as e:
        print(f"  [AmzDeals ERROR] {e}")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Scoring, tweet building, posting
# ═══════════════════════════════════════════════════════════════════════════════

def score_deal(deal):
    score = 0
    title = deal.get("title", "").lower()
    brand = ""
    brand_tier = 3

    discount = deal.get("discount_pct") or 0
    if discount >= 50:
        score += SCORE_WEIGHT_DISCOUNT
    elif discount >= 35:
        score += int(SCORE_WEIGHT_DISCOUNT * 0.8)
    elif discount >= 25:
        score += int(SCORE_WEIGHT_DISCOUNT * 0.5)

    for b in BRAND_TIER_1:
        if b in title:
            brand = b.title()
            brand_tier = 1
            score += SCORE_WEIGHT_BRAND
            break
    if not brand:
        for b in BRAND_TIER_2:
            if b in title:
                brand = b.title()
                brand_tier = 2
                score += int(SCORE_WEIGHT_BRAND * 0.7)
                break

    price = deal.get("deal_price") or 0
    if 100 <= price <= 500:
        score += SCORE_WEIGHT_PRICE_RANGE
    elif 50 <= price < 100 or 500 < price <= 800:
        score += int(SCORE_WEIGHT_PRICE_RANGE * 0.6)

    if any(b in title for b in ["deal of the day","lightning deal","best seller","amazon's choice","limited time"]):
        score += SCORE_WEIGHT_BADGE

    score += SCORE_WEIGHT_FRESHNESS

    if brand_tier == 1:
        score += SCORE_WEIGHT_TRENDING
    elif brand_tier == 2:
        score += int(SCORE_WEIGHT_TRENDING * 0.5)

    return min(score, 100), brand, brand_tier


def make_aff(asin):
    return _make_aff(asin)


def has_tech(title):
    return _has_tech(title)


def load_deals():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(DEALS_FILE):
        return []
    try:
        with open(DEALS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return []


def save_deals(deals):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DEALS_FILE, "w") as f:
        json.dump(deals, f, indent=2, default=str)


def dedup(new_deals, existing_deals, cooldown_days=7):
    existing_asins = {e.get("asin") for e in existing_deals if e.get("asin")}
    cutoff = datetime.now(timezone.utc).timestamp() - (cooldown_days * 86400)
    filtered = []
    for deal in new_deals:
        asin = deal.get("asin")
        if asin and asin in existing_asins:
            for e in existing_deals:
                if e.get("asin") == asin and e.get("posted_at"):
                    try:
                        if datetime.fromisoformat(e["posted_at"].replace("Z", "+00:00")).timestamp() > cutoff:
                            break
                    except (ValueError, TypeError):
                        pass
            else:
                filtered.append(deal)
            continue
        title = deal.get("title", "").lower()
        is_dup = any(
            _title_similarity(title, e.get("title", "").lower()) > 0.8
            for e in existing_deals
        )
        if not is_dup:
            filtered.append(deal)
    return filtered


def _title_similarity(a, b):
    if not a or not b:
        return 0.0
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def build_tweet(deal):
    title = deal.get("title", "")
    price = deal.get("deal_price")
    original = deal.get("original_price")
    discount = deal.get("discount_pct")
    brand = deal.get("brand", "")
    clean = re.sub(r'\([^)]*\)', '', title).strip()
    clean = re.sub(r'\$[\d,.]+\s*', '', clean).strip()
    clean = re.sub(r'\d+%\s*off', '', clean, flags=re.IGNORECASE).strip()
    clean = clean[:100]
    brand_str = f"[{brand}] " if brand else ""
    if discount and price:
        tweet = f"{brand_str}{clean}\n🔥 {discount}% OFF → ${price}"
        if original:
            tweet += f" (was ${original})"
    elif price:
        tweet = f"{brand_str}{clean}\n💰 ${price}"
    else:
        tweet = f"{brand_str}{clean}"
    tweet += f"\n\n#QuadStarDeals"
    return tweet[:280]


def post_tweet(postiz_url, api_key, integration_id, tweet_text):
    try:
        data = json.dumps({
            "integrationId": integration_id,
            "content": tweet_text,
            "mediaUrls": [],
            "type": "post"
        }).encode()
        req = __import__("urllib.request", fromlist=["Request"]).Request(
            f"{postiz_url}/public/v1/posts",
            data=data,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            method="POST"
        )
        with __import__("urllib.request", fromlist=["urlopen"]).urlopen(req, timeout=30) as r:
            resp = r.read().decode("utf-8", errors="replace")
            return True, resp
    except Exception as e:
        return False, str(e)


def check_budget():
    os.makedirs(DATA_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    if not os.path.exists(BUDGET_FILE):
        budget = {"spent": 0.0, "posts": 0, "date": today}
        with open(BUDGET_FILE, "w") as f:
            json.dump(budget, f)
    with open(BUDGET_FILE) as f:
        budget = json.load(f)
    if budget.get("date") != today:
        budget = {"spent": 0.0, "posts": 0, "date": today}
        with open(BUDGET_FILE, "w") as f:
            json.dump(budget, f)
    return budget["spent"] < MAX_BUDGET, budget


def update_budget(budget, posts_added, fetch_count):
    cost = (posts_added * 0.001) + (fetch_count * 0.002)
    budget["spent"] += cost
    budget["posts"] += posts_added
    with open(BUDGET_FILE, "w") as f:
        json.dump(budget, f)


# ═══════════════════════════════════════════════════════════════════════════════
# Aliases for v2/v3 pipeline compatibility
# ═══════════════════════════════════════════════════════════════════════════════
MIN_SCORE = PIPELINE_MIN_SCORE
MIN_DISCOUNT = PIPELINE_MIN_DISCOUNT
MAX_DISCOUNT = MAX_DISCOUNT_PCT
MIN_PRICE = MIN_DEAL_PRICE
MAX_BUDGET = MAX_BUDGET


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QuadStar Deals Pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Score but don't post")
    args = parser.parse_args()

    ok, budget = check_budget()
    if not ok:
        print("HALT: budget exceeded")
        sys.exit(1)
    print(f"[BUDGET] ${budget['spent']:.2f}/${MAX_BUDGET}")

    print("\nSCRAPING...")
    all_deals = []
    all_deals.extend(fetch_slickdeals())
    all_deals.extend(fetch_techbargains())
    if FIRECRAWL_API_KEY:
        fc = 3
        all_deals.extend(fetch_amz_bestsellers())
        all_deals.extend(fetch_amz_deals())
        all_deals.extend(fetch_camel())
    else:
        fc = 0
        print("  [SKIP] Firecrawl: no API key")

    print(f"  Raw: {len(all_deals)}")
    for deal in all_deals:
        s, b, t = score_deal(deal)
        deal["score"] = s
        deal["brand"] = b
        deal["brand_tier"] = t

    filt = [d for d in all_deals
            if d.get("score", 0) >= PIPELINE_MIN_SCORE
            and (d.get("discount_pct") or 0) >= PIPELINE_MIN_DISCOUNT
            and (d.get("deal_price") or 0) >= MIN_DEAL_PRICE
            and _has_tech(d.get("title", ""))]
    filt.sort(key=lambda x: x["score"], reverse=True)

    existing = load_deals()
    new_deals = dedup(filt, existing)
    top_deals = new_deals[:PIPELINE_MAX_DAILY_POSTS]

    print(f"\n  Passing filter: {len(filt)}")
    print(f"  New after dedup: {len(new_deals)}")
    print(f"  Top {len(top_deals)}:")
    for d in top_deals:
        print(f"    [{d['score']}] {d.get('discount_pct', '?')}% ${d.get('deal_price', '?')} | {d['title'][:60]}")

    if not top_deals:
        print("\nNo deals to post.")
        sys.exit(0)

    if args.dry_run:
        print("\n[DRY-RUN] Not posting.")
        sys.exit(0)

    tid = PLATFORM_IDS.get("twitter", "")
    if not POSTIZ_API_KEY or not tid:
        print("[ERROR] Postiz not configured")
        sys.exit(1)

    posted = 0
    for d in top_deals:
        tweet = build_tweet(d)
        ok, resp = post_tweet(POSTIZ_API_URL, POSTIZ_API_KEY, tid, tweet)
        if ok:
            print(f"  ✓ {d['title'][:50]}")
            posted += 1
        else:
            print(f"  ✗ {d['title'][:50]}: {resp[:80]}")

    nid = max([e.get("id", 0) for e in existing], default=0)
    for d in top_deals[:posted]:
        nid += 1
        existing.append({
            "title": d.get("title", ""), "asin": d.get("asin"),
            "deal_price": d.get("deal_price"), "discount_pct": d.get("discount_pct"),
            "score": d.get("score"), "brand": d.get("brand"),
            "source": d.get("source"), "id": nid,
            "is_posted": True, "scraped_at": datetime.now(timezone.utc).isoformat(),
            "posted_at": datetime.now(timezone.utc).isoformat(),
        })
    save_deals(existing)
    print(f"\n{posted}/{len(top_deals)} posted.")
