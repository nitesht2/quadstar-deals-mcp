from __future__ import annotations

"""
database.py - Quantdeal Deal Storage

Uses a local JSON file to track deals and posted status.
No external database needed — works directly in GitHub Actions.

Features:
- Auto-expire deals older than 48 hours
- Purge posted deals to prevent JSON bloat
- Fuzzy title deduplication (catches same product under different ASINs)
- Image quality filter (skip deals without proper product images)
- Price history tracking (surface "lowest ever" deals)
"""

import json
import os
import threading
from datetime import datetime, timedelta

# DATA_DIR is profile-aware: loaded from config/settings.py so each agent
# profile (tech, sneakers, home) gets its own isolated data directory.
# Falls back to ./data for backward compatibility when settings fails to import.
try:
    from config.settings import DATA_DIR as _SETTINGS_DATA_DIR
    DATA_DIR = _SETTINGS_DATA_DIR
except ImportError:
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DEALS_FILE = os.path.join(DATA_DIR, "deals.json")
PRICE_HISTORY_FILE = os.path.join(DATA_DIR, "price_history.json")
REPOST_TRACKING_FILE = os.path.join(DATA_DIR, "repost_tracking.json")
PENDING_REPOSTS_FILE = os.path.join(DATA_DIR, "pending_reposts.json")

# Stale deal threshold — deals expire after 24h (deals move fast)
DEAL_EXPIRY_HOURS = 24

# Per-file write locks to prevent concurrent writes from corrupting JSON
_file_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_lock(path: str) -> threading.Lock:
    """Return a per-path lock, creating it on first use."""
    with _locks_lock:
        if path not in _file_locks:
            _file_locks[path] = threading.Lock()
        return _file_locks[path]


def _atomic_write_json(path: str, data) -> None:
    """Write JSON atomically: write to tempfile, fsync, then rename.

    Prevents partial/corrupt JSON if process is killed mid-write.
    Safe for concurrent callers via per-path lock.
    """
    _ensure_data_dir()
    lock = _get_lock(path)
    with lock:
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # fsync may not be supported on all filesystems
        os.replace(tmp, path)  # atomic on POSIX


def _safe_load_json(path: str, default):
    """Load JSON with corruption recovery.

    If the file is truncated/corrupt, back it up to path.corrupt.<ts>
    and return the default. This prevents a single bad write from
    killing the entire pipeline.
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        ts = int(datetime.now().timestamp())
        backup = f"{path}.corrupt.{ts}"
        try:
            os.rename(path, backup)
            print(f"  [database] Corrupt JSON at {path} ({e}); moved to {backup}")
        except OSError:
            print(f"  [database] Corrupt JSON at {path} ({e}); could not back up")
        return default


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_deals() -> list:
    _ensure_data_dir()
    return _safe_load_json(DEALS_FILE, [])


def _save_deals(deals: list):
    _atomic_write_json(DEALS_FILE, deals)


# --- Price History ---

def _load_price_history() -> dict:
    """Load price history: {asin: [{price, date}, ...]}"""
    _ensure_data_dir()
    return _safe_load_json(PRICE_HISTORY_FILE, {})


def _save_price_history(history: dict):
    _atomic_write_json(PRICE_HISTORY_FILE, history)


def record_price(asin: str, price: float):
    """Record a price observation for an ASIN."""
    history = _load_price_history()
    if asin not in history:
        history[asin] = []
    # Only record if price changed from last observation
    entries = history[asin]
    if not entries or entries[-1]["price"] != price:
        entries.append({
            "price": price,
            "date": datetime.now().isoformat(),
        })
        # Keep last 30 observations per ASIN to prevent bloat
        history[asin] = entries[-30:]
        _save_price_history(history)


def get_price_history(asin: str) -> list:
    """Get price history for an ASIN. Returns list of {price, date}."""
    return _load_price_history().get(asin, [])


def is_lowest_price(asin: str, current_price: float) -> bool:
    """Check if current price is the lowest we've ever tracked."""
    entries = get_price_history(asin)
    if not entries:
        return False  # No history yet — can't say "lowest ever"
    lowest = min(e["price"] for e in entries)
    return current_price <= lowest


# --- Repost Tracking ---

def _load_repost_tracking() -> dict:
    """Load repost tracking: {asin: {repost_count, last_reposted_at, original_posted_price}}"""
    _ensure_data_dir()
    return _safe_load_json(REPOST_TRACKING_FILE, {})


def _save_repost_tracking(tracking: dict):
    _atomic_write_json(REPOST_TRACKING_FILE, tracking)


def can_repost(asin: str, current_price: float) -> tuple[bool, str]:
    """Check if an ASIN is eligible for price drop repost.

    Rules:
    - repost_count == 0: eligible (first repost)
    - repost_count >= 1 AND is_lowest_price: eligible (LOWEST EVER override)
    - repost_count >= 1 AND not lowest: not eligible
    """
    tracking = _load_repost_tracking()
    entry = tracking.get(asin, {})
    count = entry.get("repost_count", 0)

    if count == 0:
        return True, "first repost eligible"

    if count >= 1 and is_lowest_price(asin, current_price):
        return True, "LOWEST EVER override"

    return False, f"already reposted {count} time(s), not lowest ever"


def record_repost(asin: str, price: float):
    """Record that a repost happened for this ASIN."""
    tracking = _load_repost_tracking()
    entry = tracking.get(asin, {"repost_count": 0})
    entry["repost_count"] = entry.get("repost_count", 0) + 1
    entry["last_reposted_at"] = datetime.now().isoformat()
    entry["repost_prices"] = entry.get("repost_prices", []) + [price]
    tracking[asin] = entry
    _save_repost_tracking(tracking)


def is_lowest_in_n_days(asin: str, current_price: float, days: int = 90) -> bool:
    """Check if current_price is the lowest in the last N days of price history."""
    entries = get_price_history(asin)
    if len(entries) < 2:
        return False  # Not enough data to make the claim
    cutoff = datetime.now() - timedelta(days=days)
    recent = []
    for e in entries:
        try:
            entry_date = datetime.fromisoformat(e["date"])
            if entry_date >= cutoff:
                recent.append(e["price"])
        except (ValueError, TypeError, KeyError):
            continue
    if not recent:
        return False
    return current_price <= min(recent)


def get_original_posted_price(asin: str) -> float | None:
    """Get the price at which a deal was originally posted. Used for 'was $X when we posted it'."""
    deals = _load_deals()
    for d in deals:
        if d.get("asin") == asin and d.get("is_posted"):
            return d.get("deal_price")
    return None


# --- Pending Reposts (15-min fast-track timer persistence) ---

def save_pending_repost(pending: dict):
    """Add a pending repost to persistence file. Survives bot restarts.

    Read-modify-write is serialized via PENDING_REPOSTS_FILE lock to
    prevent two threads from clobbering each other's updates.
    """
    lock = _get_lock(PENDING_REPOSTS_FILE)
    with lock:
        all_pending = _safe_load_json(PENDING_REPOSTS_FILE, [])
        # Remove any existing entry for this ASIN (replace, don't duplicate)
        all_pending = [p for p in all_pending if p.get("asin") != pending.get("asin")]
        all_pending.append(pending)
        # Inline the write (we already hold the lock)
        _ensure_data_dir()
        tmp = f"{PENDING_REPOSTS_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(all_pending, f, indent=2, default=str)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, PENDING_REPOSTS_FILE)


def get_pending_reposts() -> list[dict]:
    """Load all pending reposts from disk."""
    _ensure_data_dir()
    return _safe_load_json(PENDING_REPOSTS_FILE, [])


def remove_pending_repost(asin: str):
    """Remove a pending repost (after approval or rejection)."""
    lock = _get_lock(PENDING_REPOSTS_FILE)
    with lock:
        all_pending = _safe_load_json(PENDING_REPOSTS_FILE, [])
        filtered = [p for p in all_pending if p.get("asin") != asin]
        _ensure_data_dir()
        tmp = f"{PENDING_REPOSTS_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(filtered, f, indent=2, default=str)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, PENDING_REPOSTS_FILE)


def get_expired_pending_reposts() -> list[dict]:
    """Return pending reposts whose auto_approve_at has passed."""
    now = datetime.now()
    expired = []
    for p in get_pending_reposts():
        try:
            approve_at = datetime.fromisoformat(p["auto_approve_at"])
            if now >= approve_at:
                expired.append(p)
        except (ValueError, TypeError, KeyError):
            continue
    return expired


# --- Fuzzy Title Deduplication ---

def _normalize_title(title: str) -> str:
    """Normalize title for fuzzy matching."""
    import re
    t = title.lower().strip()
    # Remove common noise words and punctuation
    t = re.sub(r'[^\w\s]', ' ', t)
    noise = {'with', 'for', 'and', 'the', 'new', 'latest', 'edition', 'version',
             'model', 'generation', 'gen', 'inch', 'in', 'gb', 'tb', 'mm'}
    words = [w for w in t.split() if w not in noise and len(w) > 1]
    return ' '.join(sorted(words))


def _titles_similar(title1: str, title2: str, threshold: float = 0.75) -> bool:
    """Check if two titles are similar using word overlap ratio."""
    words1 = set(_normalize_title(title1).split())
    words2 = set(_normalize_title(title2).split())
    if not words1 or not words2:
        return False
    overlap = len(words1 & words2)
    smaller = min(len(words1), len(words2))
    return (overlap / smaller) >= threshold if smaller > 0 else False


# --- Image Quality Filter ---

def _has_valid_image(deal: dict) -> bool:
    """Check if deal has a proper product image (not placeholder/tiny)."""
    img = deal.get("image_url", "")
    if not img:
        return False
    # Skip data URIs (base64 placeholders)
    if img.startswith("data:"):
        return False
    # Skip Amazon's 1x1 tracking pixels and tiny placeholders
    if "1x1" in img or "pixel" in img or "spacer" in img:
        return False
    # Skip very small thumbnails (less than 75px usually means icon/placeholder)
    if "_SL50_" in img or "_SL25_" in img or "_SS40_" in img:
        return False
    return True


# --- Category Refinement ---

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "gaming": [
        "ps5", "playstation", "xbox", "nintendo", "switch", "gaming", "game",
        "controller", "headset gamer", "gpu", "graphics card", "rtx", "gtx",
        "steam deck", "razer", "corsair", "logitech g", "mechanical keyboard",
        "gaming mouse", "gaming chair", "gaming monitor",
    ],
    "audio": [
        "headphones", "earbuds", "airpods", "earphones", "speaker", "soundbar",
        "subwoofer", "bose", "sony wh", "sennheiser", "jabra", "anker soundcore",
        "beats", "jbl", "audio", "noise cancelling", "noise-cancelling",
    ],
    "home": [
        "smart home", "echo dot", "echo show", "alexa", "ring doorbell",
        "smart bulb", "smart plug", "robot vacuum", "roomba", "air purifier",
        "humidifier", "coffee maker", "instant pot", "air fryer", "blender",
        "vacuum cleaner", "dyson", "irobot", "nest thermostat", "philips hue",
    ],
    "phones": [
        "iphone", "samsung galaxy", "pixel phone", "android phone", "smartphone",
        "magsafe", "phone case", "screen protector", "wireless charger",
        "usb-c charger", "power bank", "portable charger",
    ],
    "computers": [
        "laptop", "macbook", "chromebook", "desktop pc", "all-in-one",
        "monitor", "keyboard", "mouse", "webcam", "usb hub", "ssd", "hard drive",
        "ram", "memory", "motherboard", "processor", "cpu", "nvme",
    ],
    "tv": [
        "4k tv", "oled", "qled", "smart tv", "roku", "fire tv", "apple tv",
        "streaming", "projector", "hdmi", "television",
    ],
    "fitness": [
        "treadmill", "bike", "dumbbells", "weights", "fitness tracker",
        "fitbit", "apple watch", "garmin", "smartwatch", "yoga mat",
        "resistance bands", "pull-up bar", "jump rope", "protein powder",
    ],
    "camera": [
        "camera", "dslr", "mirrorless", "lens", "tripod", "gopro", "drone",
        "action camera", "ring light", "memory card", "sd card",
    ],
}


def _refine_category(title: str) -> str:
    """Map a deal title to a more specific category using keyword matching.

    Falls back to "tech" if no keywords match — preserving the existing
    default without introducing gaps in platform routing.
    """
    title_lower = title.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in title_lower:
                return category
    return "tech"


# --- Core Functions ---

def save_deal(deal: dict) -> bool:
    """Save a deal. Skips duplicates by URL, ASIN, and fuzzy title match.
    Also records price history and validates image quality.
    Returns True if saved, False if duplicate/filtered.
    """
    deals = _load_deals()

    # Check for URL duplicate
    existing_urls = {d["source_url"] for d in deals}
    if deal["source_url"] in existing_urls:
        return False

    # Check for ASIN duplicate
    existing_asins = {d.get("asin") for d in deals if d.get("asin")}
    if deal.get("asin") and deal["asin"] in existing_asins:
        return False

    # Check for fuzzy title duplicate (same product, different ASIN/seller)
    for existing in deals:
        if existing.get("is_active") and _titles_similar(deal.get("title", ""), existing.get("title", "")):
            # Keep the one with the lower price
            if deal.get("deal_price", 0) < existing.get("deal_price", 0):
                existing["is_active"] = False  # Deactivate the more expensive one
                break
            else:
                return False  # This one is more expensive, skip it

    # Discount sanity gate: reject deals where original > 2x deal price.
    # Amazon list prices are often inflated; scraper mixing up prices from
    # bundles/used listings causes fake high discounts.
    orig = deal.get("original_price") or 0
    current = deal.get("deal_price") or 0
    if orig > 0 and current > 0:
        from config.settings import MAX_DISCOUNT_PCT
        computed_disc = ((orig - current) / orig) * 100
        if computed_disc > MAX_DISCOUNT_PCT:
            print(f"    Skipped (discount {computed_disc:.0f}% > cap {MAX_DISCOUNT_PCT:.0f}%): {deal.get('title', '')[:50]}")
            return False

    # Image quality filter
    if not _has_valid_image(deal):
        print(f"    Skipped (no valid image): {deal.get('title', '')[:50]}")
        return False

    # Tech keyword safety net — second-pass filter at save time
    from src.amazon_scraper import _matches_category
    from config.settings import TECH_KEYWORDS
    if not _matches_category(deal.get("title", ""), TECH_KEYWORDS):
        print(f"    Skipped (not tech): {deal.get('title', '')[:50]}")
        return False

    # Category refinement: if category is the generic fallback ("tech"),
    # try to assign a more specific category via title keyword matching.
    # This improves platform routing without any LLM calls.
    if deal.get("category", "tech") in ("tech", "", None):
        deal["category"] = _refine_category(deal.get("title", ""))

    # Record price history
    if deal.get("asin") and deal.get("deal_price"):
        record_price(deal["asin"], deal["deal_price"])

    # Check if this is the lowest price we've tracked
    if deal.get("asin") and is_lowest_price(deal["asin"], deal.get("deal_price", 0)):
        deal["is_lowest_ever"] = True

    deal["id"] = max((d.get("id", 0) for d in deals), default=0) + 1
    deal["scraped_at"] = datetime.now().isoformat()
    deal["posted_at"] = None
    deal["is_posted"] = False
    deal["is_active"] = True
    deals.append(deal)

    _save_deals(deals)
    try:
        from src.archive import archive_deal
        archive_deal(deal)  # permanent SQLite history (fire-and-forget)
    except Exception:
        pass
    return True


def score_deal(deal: dict, _perf_records: list | None = None) -> float:
    """Score a deal 0-100 using weighted multi-factor model.

    _perf_records is an optional preloaded tweet_performance.json list,
    passed by get_top_unposted_deals() to avoid N file reads when
    scoring a batch.
    """
    from config.settings import (
        SCORE_WEIGHT_DISCOUNT, SCORE_WEIGHT_BRAND, SCORE_WEIGHT_PRICE_RANGE,
        SCORE_WEIGHT_ENGAGEMENT, SCORE_WEIGHT_BADGE, SCORE_WEIGHT_FRESHNESS,
        SCORE_WEIGHT_TRENDING, BRAND_TIER_1, BRAND_TIER_2,
    )

    score = 0.0
    title_lower = deal.get("title", "").lower()

    # 1. Discount (0-25): linear scale 15%=0, 50%+=max
    discount = deal.get("discount_pct", 0)
    discount_score = min(max((discount - 15) / 35, 0), 1) * SCORE_WEIGHT_DISCOUNT
    score += discount_score

    # 2. Brand tier (0-20): tier 1=full, tier 2=60%, unknown=25%
    brand_score = SCORE_WEIGHT_BRAND * 0.25  # default unknown
    for brand in BRAND_TIER_1:
        if brand in title_lower:
            brand_score = SCORE_WEIGHT_BRAND
            break
    else:
        for brand in BRAND_TIER_2:
            if brand in title_lower:
                brand_score = SCORE_WEIGHT_BRAND * 0.6
                break
    score += brand_score

    # 3. Price sweet spot (0-15): $100-500 is best
    price = deal.get("deal_price", 0)
    if 100 <= price <= 500:
        price_score = SCORE_WEIGHT_PRICE_RANGE
    elif 50 <= price < 100 or 500 < price <= 1000:
        price_score = SCORE_WEIGHT_PRICE_RANGE * 0.5
    else:
        price_score = SCORE_WEIGHT_PRICE_RANGE * 0.2
    score += price_score

    # 4. Historical engagement (0-15)
    engagement_score = _get_engagement_score(deal, SCORE_WEIGHT_ENGAGEMENT, _perf_records)
    score += engagement_score

    # 5. Lowest-ever badge (0-10)
    asin = deal.get("asin", "")
    deal_price = deal.get("deal_price", 0)
    if asin and deal_price:
        if is_lowest_price(asin, deal_price):
            score += SCORE_WEIGHT_BADGE
        elif is_lowest_in_n_days(asin, deal_price, 90):
            score += SCORE_WEIGHT_BADGE * 0.6

    # 6. Source freshness (0-10)
    scraped_at = deal.get("scraped_at", "")
    if scraped_at:
        try:
            scraped_dt = datetime.fromisoformat(scraped_at)
            hours_old = (datetime.now() - scraped_dt).total_seconds() / 3600
            if hours_old < 1:
                score += SCORE_WEIGHT_FRESHNESS
            elif hours_old < 4:
                score += SCORE_WEIGHT_FRESHNESS * 0.6
            else:
                score += SCORE_WEIGHT_FRESHNESS * 0.2
        except (ValueError, TypeError):
            score += SCORE_WEIGHT_FRESHNESS * 0.2

    # 7. Trending (0-5): seen in multiple scrape runs
    score += _get_trending_score(asin, SCORE_WEIGHT_TRENDING)

    return round(score, 1)


def _get_engagement_score(deal: dict, max_weight: float, records: list | None = None) -> float:
    """Check tweet_performance.json for similar past deals.

    Accepts a preloaded records list to avoid re-reading the file once
    per deal when scoring a batch (see get_top_unposted_deals).
    """
    if records is None:
        perf_path = os.path.join(DATA_DIR, "tweet_performance.json")
        records = _safe_load_json(perf_path, [])

    if not records:
        return max_weight * 0.5

    title_lower = deal.get("title", "").lower()
    relevant = []
    for r in records:
        r_title = r.get("tweet_text", "").lower()
        if any(word in r_title for word in title_lower.split()[:3] if len(word) > 4):
            eng = r.get("engagement_score", 0)
            if eng > 0:
                relevant.append(eng)

    if not relevant:
        return max_weight * 0.5

    avg_engagement = sum(relevant) / len(relevant)
    normalized = min(avg_engagement / 5.0, 1.0)
    return normalized * max_weight


def _get_trending_score(asin: str, max_weight: float) -> float:
    """Check if ASIN appeared in previous scrape runs."""
    deals = _load_deals()
    appearances = sum(1 for d in deals if d.get("asin") == asin)
    if appearances >= 3:
        return max_weight
    elif appearances >= 2:
        return max_weight * 0.6
    return 0


def get_top_unposted_deals(limit: int = 5, min_discount: float = 15.0, max_age_hours: int = 24) -> list:
    """Fetch top unposted deals ranked by preference score.

    Prioritizes fresh deals (within max_age_hours), but falls back to older
    unposted deals if no fresh ones qualify. This ensures we always have
    content to post rather than staying silent for days.

    Deals below min_discount (default 15%) are filtered out.
    """
    deals = _load_deals()
    now = datetime.now()

    fresh_unposted = []
    older_unposted = []
    for d in deals:
        if d.get("is_posted") or not d.get("is_active"):
            continue
        if not _has_valid_image(d):
            continue
        # Hard filter: skip deals below minimum discount threshold
        if (d.get("discount_pct", 0) or 0) < min_discount:
            continue

        scraped_at = d.get("scraped_at", "")
        if not scraped_at:
            continue
        try:
            scraped_time = datetime.fromisoformat(scraped_at)
            age_hours = (now - scraped_time).total_seconds() / 3600
        except (ValueError, TypeError):
            continue

        bucket = fresh_unposted if age_hours <= max_age_hours else older_unposted
        bucket.append(d)

    # Load tweet_performance.json once and pass to every score_deal call
    # to avoid N+1 file reads when ranking a batch of deals.
    perf_path = os.path.join(DATA_DIR, "tweet_performance.json")
    perf_records = _safe_load_json(perf_path, [])

    # Attach composite deal score
    for d in fresh_unposted:
        d["deal_score"] = score_deal(d, _perf_records=perf_records)
    for d in older_unposted:
        d["deal_score"] = score_deal(d, _perf_records=perf_records)

    # Sort by composite deal score (primary), recency as tiebreaker
    fresh_unposted.sort(key=lambda d: d["deal_score"], reverse=True)
    older_unposted.sort(key=lambda d: d["deal_score"], reverse=True)

    # Prefer fresh deals, fall back to older unposted ones
    result = fresh_unposted[:limit]
    if len(result) < limit:
        result.extend(older_unposted[:limit - len(result)])

    return result[:limit]


def get_deal_by_id(deal_id: int) -> dict | None:
    """Fetch a single deal by its id field. Returns None if not found."""
    deals = _load_deals()
    for deal in deals:
        if deal.get("id") == deal_id:
            return deal
    return None


def get_posted_deals() -> list:
    """Return all deals that have been posted."""
    deals = _load_deals()
    return [d for d in deals if d.get("is_posted")]


def get_posts_today_count() -> int:
    """Count deals auto-posted today (PST calendar day)."""
    from datetime import date
    today = date.today().isoformat()
    deals = _load_deals()
    return sum(
        1 for d in deals
        if d.get("is_posted") and (d.get("posted_at") or "").startswith(today)
    )


def get_watchlist_asins(days: int = 7) -> list[str]:
    """Get unique ASINs from deals posted in the last N days."""
    deals = _load_deals()
    cutoff = datetime.now() - timedelta(days=days)
    asins = set()
    for d in deals:
        asin = d.get("asin")
        if not asin or not d.get("is_posted"):
            continue
        posted_at = d.get("posted_at", "")
        if not posted_at:
            continue
        try:
            if datetime.fromisoformat(posted_at) >= cutoff:
                asins.add(asin)
        except (ValueError, TypeError):
            continue
    return list(asins)


def mark_as_posted(deal_id: int):
    """Mark a deal as posted."""
    deals = _load_deals()
    posted_source = None
    posted_deal = None
    for deal in deals:
        if deal.get("id") == deal_id:
            deal["is_posted"] = True
            deal["posted_at"] = datetime.now().isoformat()
            posted_source = deal.get("source")
            posted_deal = deal
            break
    _save_deals(deals)
    # Permanent SQLite record of the post (fire-and-forget)
    if posted_deal:
        try:
            from src.archive import archive_post
            archive_post(posted_deal)
        except Exception:
            pass
    # Track which source produced a posted deal (fire-and-forget)
    if posted_source:
        try:
            from src.source_tracker import record_posted
            record_posted(posted_source)
        except Exception:
            pass


def update_deal(deal_id: int, fields: dict):
    """Update specific fields on a deal."""
    deals = _load_deals()
    for deal in deals:
        if deal.get("id") == deal_id:
            deal.update(fields)
            break
    _save_deals(deals)


# --- Cleanup Functions ---

def expire_stale_deals() -> int:
    """Expire deals older than DEAL_EXPIRY_HOURS. Returns count expired."""
    deals = _load_deals()
    cutoff = datetime.now() - timedelta(hours=DEAL_EXPIRY_HOURS)
    expired = 0

    for deal in deals:
        if not deal.get("is_active"):
            continue
        scraped_at = deal.get("scraped_at", "")
        if not scraped_at:
            continue
        try:
            scraped_time = datetime.fromisoformat(scraped_at)
            if scraped_time < cutoff:
                deal["is_active"] = False
                expired += 1
        except (ValueError, TypeError):
            continue

    if expired > 0:
        _save_deals(deals)
    return expired


def purge_old_posted_deals(keep_days: int = 7) -> int:
    """Remove posted deals older than keep_days to prevent JSON bloat.
    Keeps unposted active deals and recently posted deals.
    Returns count purged.
    """
    deals = _load_deals()
    cutoff = datetime.now() - timedelta(days=keep_days)
    original_count = len(deals)

    kept = []
    for deal in deals:
        # Always keep unposted active deals
        if not deal.get("is_posted") and deal.get("is_active"):
            kept.append(deal)
            continue

        # Keep recently posted/scraped deals
        timestamp = deal.get("posted_at") or deal.get("scraped_at", "")
        if not timestamp:
            continue  # No timestamp = drop it
        try:
            deal_time = datetime.fromisoformat(timestamp)
            if deal_time >= cutoff:
                kept.append(deal)
        except (ValueError, TypeError):
            continue  # Bad timestamp = drop it

    purged = original_count - len(kept)
    if purged > 0:
        _save_deals(kept)
    return purged


def get_engagement_by_hour() -> dict:
    """Average engagement per PST hour-of-day from past posts.

    Data-driven posting-time signal: post when YOUR audience actually engaged.
    Returns {pst_hour:int -> avg_engagement_score} for hours that have data.
    Empty until enough posts have engagement recorded (then it self-improves).
    """
    from collections import defaultdict
    recs = _safe_load_json(os.path.join(DATA_DIR, "tweet_performance.json"), [])
    buckets: dict[int, list] = defaultdict(list)
    for r in recs:
        ts = r.get("posted_at", "")
        eng = r.get("engagement_score")
        if not ts or eng is None:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", ""))
            pst_hour = (dt.hour - 7) % 24  # server is UTC -> PDT
            buckets[pst_hour].append(float(eng))
        except (ValueError, TypeError):
            continue
    return {h: round(sum(v) / len(v), 2) for h, v in sorted(buckets.items())}


def cleanup_deals() -> dict:
    """Run all cleanup tasks. Called at start of each pipeline run.
    Returns summary dict.
    """
    expired = expire_stale_deals()
    purged = purge_old_posted_deals()
    deals = _load_deals()
    return {
        "expired": expired,
        "purged": purged,
        "remaining": len(deals),
        "active": sum(1 for d in deals if d.get("is_active")),
        "unposted": sum(1 for d in deals if not d.get("is_posted") and d.get("is_active")),
    }
