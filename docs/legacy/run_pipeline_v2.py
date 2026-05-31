#!/usr/bin/env python3
"""
QuadStar Deals Pipeline v3 — Research + Scrape + Score + Post + Notify

Stages:
  1. Research — x_search via Hermes gateway for trending deals, competitor intel, market gaps
  2. Scrape — Slickdeals, TechBargains, Amazon (Browserbase), CamelCamelCamel
  3. Merge & boost — research insights boost deal scores
  4. Post via Postiz → Twitter
  5. Discord notification — research summary + posted deals

Usage:
  python3 run_pipeline_v2.py              # full pipeline
  python3 run_pipeline_v2.py --research-only  # just research, save to file
  python3 run_pipeline_v2.py --scrape-only    # skip research, use cached or no boost
"""

import json, os, re, time, random, sys, argparse
from datetime import datetime, timezone
from urllib.parse import quote
import urllib.request, urllib.error

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_DIR = "/root/Projects/quadstar-deals"
DATA_DIR = os.path.join(PROJECT_DIR, "data")
RESEARCH_DIR = os.path.join(DATA_DIR, "research")
DEALS_FILE = os.path.join(DATA_DIR, "deals.json")
BUDGET_FILE = "/root/.hermes/scripts/quadstar_budget.json"
ENV_FILES = ["/root/.hermes/.env", os.path.join(PROJECT_DIR, ".env")]
LEGACY_PIPELINE = os.path.join(PROJECT_DIR, "run_pipeline.py")

# ── Research scoring boosts ──────────────────────────────────────────────────
TRENDING_BOOST = 15
COMPETITOR_GAP_BOOST = 10
SEASONAL_BOOST = 8

# ── Pipeline constants (match run_pipeline.py) ───────────────────────────────
AFFILIATE_TAG = "quadstar0e-20"
MIN_DISCOUNT = 25.0
MIN_SCORE = 58
MIN_PRICE = 50.0
MAX_DISCOUNT = 80.0
MAX_POSTS = 4
MAX_BUDGET = 4.00

BRAND_TIER_1 = ["apple","sony","bose","samsung","lg","dell","hp","lenovo","asus","microsoft","google","nvidia","amd","intel","logitech","razer","corsair","steelseries","jbl","sennheiser"]
BRAND_TIER_2 = ["anker","tp-link","netgear","western digital","seagate","crucial","hyperx","elgato","shokz","jabra","philips","epson","brother","roku","amazon","fire","echo","ring","eufy","roborock"]
TECH_KEYWORDS = ["laptop","macbook","chromebook","notebook","desktop","pc","imac","monitor","display","gpu","graphics","processor","cpu","ram","ssd","hard drive","nvme","motherboard","keyboard","mouse","webcam","router","modem","wifi","mesh","iphone","ipad","tablet","smartphone","airpods","earbuds","headphones","speaker","soundbar","playstation","ps5","xbox","nintendo","switch","gaming","controller","console","apple watch","smartwatch","fitbit","garmin","echo","alexa","smart home","thermostat","security camera","robot vacuum","roomba","tv","television","oled","qled","4k","8k","projector","fire stick","roku","chromecast","charger","power bank","usb-c","thunderbolt","docking station","printer","scanner","nas","external drive","drone","camera","gopro","wireless","bluetooth","usb","hdmi","battery","portable","headset","microphone","adapter","hub","dock"]


def load_env():
    for path in ENV_FILES:
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    s = line.strip()
                    if s and not s.startswith("#") and "=" in s:
                        k, v = s.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 1: Research via Hermes x_search
# ═══════════════════════════════════════════════════════════════════════════════

def call_hermes(messages, model="grok-4.20-reasoning", timeout=120):
    """Call Hermes gateway /v1/chat/completions."""
    hermes_url = os.environ.get("HERMES_URL", "http://localhost:3000").rstrip("/")
    try:
        data = json.dumps({"model": model, "messages": messages, "max_tokens": 2000}).encode()
        req = urllib.request.Request(
            f"{hermes_url}/v1/chat/completions", data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read().decode("utf-8", errors="replace"))
            return result.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        print(f"  [HERMES ERROR] {e}")
        return None


def run_research():
    """
    Use Hermes x_search to gather market intelligence from X.
    Returns research dict with trending topics, competitor intel, gaps, seasonal signals.
    """
    print("\n" + "═" * 60)
    print("STAGE 1: Research — x_search market intelligence")
    print("═" * 60)

    os.makedirs(RESEARCH_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    research_file = os.path.join(RESEARCH_DIR, f"research-{today}.json")

    # Use cached research if already done today
    if os.path.exists(research_file):
        print(f"  Using cached research from {today}")
        with open(research_file) as f:
            return json.load(f)

    research = {
        "date": today,
        "hermes_available": False,
        "trending_topics": [],
        "competitor_posts": [],
        "market_gaps": [],
        "seasonal_signals": [],
        "top_products": [],
        "boost_keywords": [],
        "raw": {}
    }

    # Check Hermes gateway
    hermes_url = os.environ.get("HERMES_URL", "http://localhost:3000").rstrip("/")
    try:
        urllib.request.urlopen(f"{hermes_url}/health", timeout=5)
        research["hermes_available"] = True
    except Exception:
        print(f"  [SKIP] Hermes gateway not reachable at {hermes_url}")
        with open(research_file, "w") as f:
            json.dump(research, f, indent=2)
        return research

    # ── Query 1: Trending Amazon tech deals ──────────────────────────────
    print("  Q1: Trending Amazon tech deals on X...")
    r1 = call_hermes([{
        "role": "user",
        "content": "Search X for trending Amazon tech deals today. Focus on: laptops, gaming gear, headphones, SSDs, smart home, accessories. List the top 8-10 products/deals with brand names and approximate discount %. Return as a structured list."
    }])
    if r1:
        research["raw"]["trending"] = r1[:3000]
        products = re.findall(r'(?:MacBook|AirPods|Sony|Samsung|ASUS|ROG|Acer|Nvidia|iPhone|iPad|Fire\s?Stick|Ring|Echo|PS5|Xbox|Switch|Beats|Bose|JBL|Anker|Soundcore|Crucial|WD|Seagate|Sennheiser|SteelSeries|Logitech|Razer)[\w\s]{0,40}', r1, re.IGNORECASE)
        research["trending_topics"] = list(dict.fromkeys(p.strip() for p in products if len(p.strip()) > 2))[:12]
        print(f"    Trending: {research['trending_topics'][:5]}")
    time.sleep(3)

    # ── Query 2: Competitor intel ────────────────────────────────────────
    print("  Q2: Competitor deal posts...")
    r2 = call_hermes([{
        "role": "user",
        "content": "Search X for Amazon tech deal posts from deal accounts like @ScottyDeals, @TechDropsDeals, @BigDealsHunter, @FariaAragonez in the last 24 hours. What products/categories are they posting about? List specific products and deals."
    }])
    if r2:
        research["raw"]["competitor"] = r2[:3000]
        deals = re.findall(r'([A-Z][\w\s\-\(\)]{8,80}(?:laptop|headphone|earbud|SSD|monitor|keyboard|mouse|speaker|TV|tablet|camera|drone|router|charger|hub|dock|gaming|console)[\w\s]{0,20})', r2, re.IGNORECASE)
        research["competitor_posts"] = list(dict.fromkeys(d.strip() for d in deals if len(d.strip()) > 8))[:10]
        print(f"    Competitor deals: {len(research['competitor_posts'])}")
    time.sleep(3)

    # ── Query 3: Market trends / new launches ────────────────────────────
    print("  Q3: Market trends and new launches...")
    r3 = call_hermes([{
        "role": "user",
        "content": "Search X for new tech product launches and trending gadgets in May 2026. What's getting buzz? Any new Apple, Samsung, Sony, or gaming product announcements? Focus on products that would be good Amazon deals."
    }])
    if r3:
        research["raw"]["trends"] = r3[:2500]
        trends = re.findall(r'(?:launch|new|announce|trending|buzz)\s*:?\s*([A-Z][\w\s\-\(\)]{8,60})', r3, re.IGNORECASE)
        research["market_gaps"] = list(dict.fromkeys(t.strip() for t in trends if len(t.strip()) > 5))[:8]
        print(f"    Market gaps: {len(research['market_gaps'])}")
    time.sleep(3)

    # ── Query 4: Seasonal / event signals ────────────────────────────────
    print("  Q4: Seasonal demand and sale events...")
    r4 = call_hermes([{
        "role": "user",
        "content": "Search X for upcoming Amazon sale events and seasonal tech demand. Look for: Memorial Day deals, Prime Day rumors, Father's Day tech gifts, graduation gifts, back-to-school tech. What categories are expected to have the best deals in the next 2 weeks?"
    }])
    if r4:
        research["raw"]["seasonal"] = r4[:2000]
        events = re.findall(r'(Memorial Day|Prime Day|Father\'?s Day|Graduation|Back to School|Black Friday|Cyber Monday|Summer)[^\.\n]{0,120}', r4, re.IGNORECASE)
        research["seasonal_signals"] = list(dict.fromkeys(e.strip() for e in events if len(e.strip()) > 3))[:6]
        print(f"    Seasonal: {research['seasonal_signals']}")
    time.sleep(3)

    # ── Query 5: Hidden gems ─────────────────────────────────────────────
    print("  Q5: Hidden gem / high-value deals...")
    r5 = call_hermes([{
        "role": "user",
        "content": "Search X for hidden gem Amazon tech deals under $100 that offer exceptional value. Think: Anker accessories, TP-Link routers, Crucial SSDs, JBL audio, etc. List specific products with prices if available."
    }])
    if r5:
        research["raw"]["gems"] = r5[:2000]
        gems = re.findall(r'([A-Z][\w\s\-\(\)]{8,60}(?:under\s*\$|\$\d{2,3}))', r5, re.IGNORECASE)
        research["top_products"] = list(dict.fromkeys(g.strip() for g in gems if len(g.strip()) > 5))[:8]
        print(f"    Hidden gems: {len(research['top_products'])}")

    # Build boost keywords from all research
    all_products = research["trending_topics"] + research["top_products"]
    research["boost_keywords"] = list(dict.fromkeys(p.lower() for p in all_products if len(p) > 3))[:25]
    print(f"\n  Boost keywords ({len(research['boost_keywords'])}): {research['boost_keywords'][:10]}")

    # Save
    with open(research_file, "w") as f:
        json.dump(research, f, indent=2)
    print(f"  Research saved: {research_file}")

    return research


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 2: Scrape + Score + Filter + Dedup (from run_pipeline.py)
# ═══════════════════════════════════════════════════════════════════════════════

def import_legacy():
    """Import the legacy run_pipeline module."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("legacy", LEGACY_PIPELINE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {LEGACY_PIPELINE}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_scrape(legacy):
    """Scrape all sources, score, filter, dedup. Returns (new_deals, all_existing, fetch_count)."""
    print("\n" + "═" * 60)
    print("STAGE 2: Scrape + Score + Filter + Dedup")
    print("═" * 60)

    load_env()
    bb_key = os.environ.get("BROWSERBASE_API_KEY", "")
    all_deals = []
    fc = 0

    # Slickdeals
    try:
        d = legacy.fetch_slickdeals()
        all_deals.extend(d)
        print(f"  Slickdeals: {len(d)}")
    except Exception as e:
        print(f"  [SKIP] Slickdeals: {e}")

    # TechBargains
    try:
        d = legacy.fetch_techbargains()
        all_deals.extend(d)
        print(f"  TechBargains: {len(d)}")
    except Exception as e:
        print(f"  [SKIP] TechBargains: {e}")

    # Amazon + Camel (Browserbase)
    if bb_key:
        for name, fn in [("Amz BestSellers", legacy.fetch_amz_bestsellers),
                          ("Amz Deals", legacy.fetch_amz_deals),
                          ("CamelCamelCamel", legacy.fetch_camel)]:
            try:
                d = fn(bb_key)
                all_deals.extend(d)
                fc += 1
                print(f"  {name}: {len(d)}")
            except Exception as e:
                print(f"  [SKIP] {name}: {e}")
    else:
        print("  [SKIP] Browserbase: no API key")

    print(f"  Total raw: {len(all_deals)}")

    # Score
    for deal in all_deals:
        s, b, t = legacy.score_deal(deal)
        deal["score"] = s
        deal["brand"] = b
        deal["brand_tier"] = t

    # Filter
    filt = [
        d for d in all_deals
        if d.get("score", 0) >= legacy.MIN_SCORE
        and (d.get("discount_pct") or 0) >= legacy.MIN_DISCOUNT
        and (d.get("deal_price") or 0) >= legacy.MIN_PRICE
        and (d.get("discount_pct") or 0) <= legacy.MAX_DISCOUNT
        and legacy.has_tech(d.get("title", ""))
    ]
    filt.sort(key=lambda x: x["score"], reverse=True)
    print(f"  Passing filter: {len(filt)}")

    # Dedup
    existing = legacy.load_deals()
    new_deals = legacy.dedup(filt, existing)
    print(f"  New after dedup: {len(new_deals)}")

    return new_deals, existing, fc


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 3: Merge research + boost scores
# ═══════════════════════════════════════════════════════════════════════════════

def merge_and_boost(new_deals, research):
    """Apply research-based scoring boosts to scraped deals."""
    print("\n" + "═" * 60)
    print("STAGE 3: Merge research insights + boost scoring")
    print("═" * 60)

    if not research.get("hermes_available") or not research.get("boost_keywords"):
        print("  No research data — using base scores only")
        new_deals.sort(key=lambda x: x.get("score", 0), reverse=True)
        return new_deals[:MAX_POSTS]

    boosted = []
    boost_log = []

    for deal in new_deals:
        original = deal.get("score", 0)
        boost = 0
        reasons = []
        title_l = deal.get("title", "").lower()

        # Trending keyword match
        for kw in research.get("boost_keywords", []):
            if kw in title_l:
                boost += TRENDING_BOOST
                reasons.append(f"trending:{kw}")
                break

        # Competitor gap match
        for comp in research.get("competitor_posts", []):
            comp_words = set(comp.lower().split()) - {"the","a","an","and","or","is","in","to","of","for","with","on","at","by","from"}
            deal_words = set(title_l.split())
            if len(comp_words & deal_words) >= 2:
                boost += COMPETITOR_GAP_BOOST
                reasons.append("competitor_gap")
                break

        # Seasonal match
        seasonal_keywords = {
            "father": ["gaming", "tool", "watch", "speaker", "headphone", "grill", "outdoor"],
            "graduation": ["laptop", "tablet", "headphone", "speaker", "camera", "watch"],
            "back to school": ["laptop", "tablet", "headphone", "printer", "ssd", "keyboard", "mouse"],
            "memorial": ["grill", "outdoor", "furniture", "mattress", "tv", "speaker"],
            "prime": ["echo", "fire", "ring", "kindle", "alexa", "blink", "eufy"],
        }
        for signal in research.get("seasonal_signals", []):
            sig_l = signal.lower()
            for season_key, season_products in seasonal_keywords.items():
                if season_key in sig_l:
                    if any(sp in title_l for sp in season_products):
                        boost += SEASONAL_BOOST
                        reasons.append(f"seasonal:{signal}")
                        break

        deal["score"] = original + boost
        deal["boost"] = boost
        deal["boost_reasons"] = reasons
        boosted.append(deal)

        if boost > 0:
            boost_log.append(f"    +{boost} [{original}→{original+boost}] {deal['title'][:60]} | {', '.join(reasons)}")

    boosted.sort(key=lambda x: x.get("score", 0), reverse=True)

    if boost_log:
        print(f"  Boosted {len(boost_log)} deals:")
        for b in boost_log[:10]:
            print(b)
    else:
        print("  No deals matched research keywords")

    return boosted[:MAX_POSTS]


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 4: Post + Save + Discord
# ═══════════════════════════════════════════════════════════════════════════════

def post_and_notify(top_deals, all_existing, fetch_count, research, legacy):
    """Post to Twitter via Postiz, save to deals.json, notify Discord."""
    print("\n" + "═" * 60)
    print("STAGE 4: Post + Save + Discord")
    print("═" * 60)

    if not top_deals:
        print("  No deals to post.")
        if research.get("hermes_available"):
            _discord_research_only(research)
        return 0

    load_env()
    purl = os.environ.get("POSTIZ_API_URL", "http://localhost:4007/api")
    pkey = os.environ.get("POSTIZ_API_KEY", "")
    tid = os.environ.get("POSTIZ_TWITTER_ID", "")
    if not pkey or not tid:
        print("  [ERROR] Postiz not configured")
        return 0

    # Post
    posted = []
    for i, d in enumerate(top_deals):
        tweet = legacy.build_tweet(d)
        print(f"  Post {i+1}: {d['title'][:60]}")
        ok = False
        for attempt in range(2):
            ok, resp = legacy.post_tweet(purl, pkey, tid, tweet)
            if ok:
                print(f"    ✓ Posted (attempt {attempt+1})")
                break
            print(f"    ✗ Failed: {resp[:80]}")
            time.sleep(2)
        d["is_posted"] = ok
        d["posted_at"] = datetime.now(timezone.utc).isoformat() if ok else None
        d["research_boosted"] = d.get("boost", 0) > 0
        posted.append(d)

    # Save
    nid = max([e.get("id", 0) for e in all_existing], default=0)
    for d in posted:
        nid += 1
        all_existing.append({
            "title": d.get("title", ""), "asin": d.get("asin"),
            "original_price": d.get("original_price"), "deal_price": d.get("deal_price"),
            "discount_pct": d.get("discount_pct"),
            "retailer": "Amazon" if "amazon" in d.get("url", "").lower() else d.get("source", ""),
            "source_url": d.get("url", ""),
            "affiliate_url": legacy.make_aff(d["asin"]) if d.get("asin") else d.get("url", ""),
            "image_url": None, "category": "tech",
            "brand": d.get("brand"), "brand_tier": d.get("brand_tier"),
            "source": d.get("source"), "id": nid, "score": d.get("score"),
            "boost": d.get("boost", 0), "boost_reasons": d.get("boost_reasons", []),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "posted_at": d.get("posted_at"),
            "is_posted": d.get("is_posted", False),
            "research_boosted": d.get("research_boosted", False),
            "is_active": True
        })
    legacy.save_deals(all_existing)
    pc = len([d for d in posted if d.get("is_posted")])
    print(f"  Saved {len(posted)} ({pc} posted)")

    # Discord
    _discord_full(research, posted, pc)
    return pc


def _discord_full(research, posted, pc):
    """Send Discord message with research summary + posted deals."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    channel = os.environ.get("DISCORD_CHANNEL_ID", "")
    if not token or not channel:
        return

    lines = [f"**🐉 QuadStar Deals — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}**"]

    # Research section
    if research.get("hermes_available"):
        lines.append("\n📊 **Research Intel:**")
        if research.get("trending_topics"):
            lines.append(f"  🔥 Trending: {', '.join(research['trending_topics'][:6])}")
        if research.get("competitor_posts"):
            lines.append(f"  👀 {len(research['competitor_posts'])} competitor deal(s) spotted")
        if research.get("market_gaps"):
            lines.append(f"  📈 Gaps: {', '.join(research['market_gaps'][:4])}")
        if research.get("seasonal_signals"):
            lines.append(f"  🗓️ Seasonal: {', '.join(research['seasonal_signals'])}")

    # Deals section
    if pc > 0:
        lines.append(f"\n✅ **{pc} New Post(s):**")
        for d in posted:
            if d.get("is_posted"):
                bt = f"[{d['brand']}] " if d.get("brand") else ""
                rocket = " 🚀" if d.get("research_boosted") else ""
                lines.append(
                    f"• {bt}{d['title'][:80]}{rocket}\n"
                    f"  ↳ **{d.get('discount_pct', '?')}% OFF** → **${d.get('deal_price', '?')}** (Score: {d.get('score', '?')})"
                )
    else:
        lines.append("\n⚠️ No new deals posted.")

    boosted = len([d for d in posted if d.get("research_boosted")])
    if boosted:
        lines.append(f"\n📌 {boosted} deal(s) boosted by research intelligence.")

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n...(truncated)"
    _discord_send(token, channel, msg)


def _discord_research_only(research):
    """Send research-only Discord message."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    channel = os.environ.get("DISCORD_CHANNEL_ID", "")
    if not token or not channel:
        return

    lines = [f"**🐉 QuadStar Deals — Research Brief {datetime.now().strftime('%Y-%m-%d')}**"]
    lines.append("\n📊 **Market Intel (no new deals):**")
    if research.get("trending_topics"):
        lines.append(f"  🔥 Trending: {', '.join(research['trending_topics'][:8])}")
    if research.get("competitor_posts"):
        lines.append(f"  👀 {len(research['competitor_posts'])} competitor deal(s) spotted")
    if research.get("market_gaps"):
        lines.append(f"  📈 Gaps: {', '.join(research['market_gaps'][:5])}")
    lines.append("\n💡 Use this intel to guide next deal selections.")

    _discord_send(token, channel, "\n".join(lines))


def _discord_send(token, channel, message):
    data = json.dumps({"content": message}).encode()
    try:
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel}/messages",
            data=data,
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = r.status in (200, 201)
            print(f"  Discord: {'sent' if ok else 'failed'}")
            return ok
    except Exception as e:
        print(f"  [DISCORD ERROR] {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="QuadStar Deals Pipeline v3")
    parser.add_argument("--research-only", action="store_true", help="Run research stage only")
    parser.add_argument("--scrape-only", action="store_true", help="Skip research, use cached or no boost")
    args = parser.parse_args()

    print("═" * 60)
    print(f"QuadStar Deals Pipeline v3 — {datetime.now().isoformat()}")
    print("═" * 60)
    load_env()

    # Jitter
    jitter = random.randint(0, 120)
    print(f"\n[JITTER] Sleeping {jitter}s...")
    time.sleep(jitter)

    # Import legacy
    legacy = import_legacy()

    # Budget check
    ok, budget = legacy.check_budget()
    if not ok:
        print("HALT: budget exceeded")
        return
    print(f"[BUDGET] ${budget['spent']:.2f}/${legacy.MAX_BUDGET}")

    # ── Stage 1: Research ─────────────────────────────────────────────────
    research = {"hermes_available": False, "boost_keywords": [], "trending_topics": [], "competitor_posts": [], "market_gaps": [], "seasonal_signals": [], "top_products": []}
    if not args.scrape_only:
        research = run_research()
    else:
        print("\n[SKIP] Research stage (--scrape-only mode)")

    if args.research_only:
        print("\n[RESEARCH-ONLY MODE] Done.")
        return

    # ── Stage 2: Scrape ───────────────────────────────────────────────────
    new_deals, all_existing, fc = run_scrape(legacy)

    if not new_deals:
        print("\n  No new deals found.")
        if research.get("hermes_available"):
            _discord_research_only(research)
        legacy.update_budget(budget, 0, fc)
        return

    # ── Stage 3: Merge + Boost ────────────────────────────────────────────
    top_deals = merge_and_boost(new_deals, research)

    print(f"\n  Top {len(top_deals)} deals:")
    for d in top_deals:
        boost_str = f" (+{d.get('boost', 0)} research)" if d.get("boost", 0) > 0 else ""
        print(f"    [{d['score']}] {d.get('discount_pct', '?')}% ${d.get('deal_price', '?')} | {d['title'][:60]}{boost_str}")

    # ── Stage 4: Post + Save + Discord ────────────────────────────────────
    pc = post_and_notify(top_deals, all_existing, fc, research, legacy)

    legacy.update_budget(budget, pc, fc)
    print(f"\n{'═' * 60}")
    print(f"Done. {pc}/{len(top_deals)} posted. ${budget['spent']:.4f} spent.")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    main()
