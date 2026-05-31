#!/usr/bin/env python3
"""
QuadStar Deals — Automated Scheduler + Human-in-the-Loop Pipeline

Runs 4x/day at peak times (8am, 12pm, 5pm, 7pm PST).
Each run:
  1. Research via DeepSeek
  2. Scrape deals via Firecrawl
  3. Score + merge research boosts
  4. Send deal candidates to Discord with reaction buttons
  5. Wait for human approval (✅) or rejection (❌)
  6. Post approved deals via Postiz
  7. Send summary to Discord

Cron schedule (UTC):
  15:00, 19:00, 00:00, 02:00  (±30min jitter)
"""

import json, os, re, sys, time, random, asyncio, signal
from datetime import datetime, timezone

PROJECT_DIR = "/root/Projects/quadstar-deals"
sys.path.insert(0, PROJECT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

# Import pipeline v3 research/boost logic
RUN_PIPELINE_V3 = os.path.join(PROJECT_DIR, "run_pipeline_v3.py")
RUN_PIPELINE = os.path.join(PROJECT_DIR, "run_pipeline.py")

# Load settings
from config.settings import (
    POSTIZ_API_URL, POSTIZ_API_KEY, PLATFORM_IDS,
    DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID,
    PIPELINE_MAX_DAILY_POSTS, DATA_DIR,
)
from src.database import load_deals, save_deal

import importlib.util
spec = importlib.util.spec_from_file_location("run_pipeline", RUN_PIPELINE)
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)

# Reload to get all functions
import run_pipeline as lp


# ═══════════════════════════════════════════════════════════════════════════════
# Research (from v3)
# ═══════════════════════════════════════════════════════════════════════════════

def call_deepseek(system_prompt, user_message, model="deepseek-chat", max_tokens=2000, timeout=60):
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None
    import urllib.request
    try:
        data = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            "max_tokens": max_tokens
        }).encode()
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read().decode("utf-8", errors="replace"))
            return result.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        print(f"  [DEEPSEEK ERROR] {e}")
        return None


RESEARCH_DIR = os.path.join(DATA_DIR, "research")

def run_research():
    """Quick research — single DeepSeek query."""
    import urllib.request
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    research_file = os.path.join(RESEARCH_DIR, f"research-{today}.json")

    if os.path.exists(research_file):
        with open(research_file) as f:
            return json.load(f)

    current_month = datetime.now().strftime("%B %Y")
    system_prompt = (
        f"You are a tech deal research analyst. Today is {today}. "
        "Generate market intelligence for Amazon tech deals. "
        "Return ONLY a valid JSON object with these fields:\n"
        "  trending_topics: list of 8-12 specific products trending.\n"
        "  competitor_posts: list of 5-8 products competitors are posting.\n"
        "  market_gaps: list of 4-6 underrepresented categories.\n"
        "  seasonal_signals: list of 3-5 upcoming sale events.\n"
        "  top_products: list of 6-8 hidden gems under $100.\n"
        "  boost_keywords: flat list of 20-25 lowercase brand/product keywords.\n"
    )
    response = call_deepseek(system_prompt, f"Generate tech deal market intel for {current_month}.")
    research = {
        "date": today, "researched": bool(response),
        "trending_topics": [], "competitor_posts": [],
        "market_gaps": [], "seasonal_signals": [],
        "top_products": [], "boost_keywords": [],
    }
    if response:
        import json as _json
        try:
            m = re.search(r'\{[\s\S]*\}', response)
            if m:
                parsed = _json.loads(m.group())
                research["trending_topics"] = parsed.get("trending_topics", [])[:12]
                research["competitor_posts"] = parsed.get("competitor_posts", [])[:10]
                research["market_gaps"] = parsed.get("market_gaps", [])[:8]
                research["seasonal_signals"] = parsed.get("seasonal_signals", [])[:6]
                research["top_products"] = parsed.get("top_products", [])[:8]
                research["boost_keywords"] = parsed.get("boost_keywords", [])[:25]
        except Exception:
            pass

    with open(research_file, "w") as f:
        json.dump(research, f, indent=2)
    print(f"  Research: {len(research['trending_topics'])} trending, {len(research['boost_keywords'])} keywords")
    return research


# ═══════════════════════════════════════════════════════════════════════════════
# Deal approval via Discord reactions (human-in-the-loop)
# ═══════════════════════════════════════════════════════════════════════════════

DISCORD_API = "https://discord.com/api/v10"

def discord_request(method, path, data=None, token=None):
    """Make a Discord API request. Returns (status, response)."""
    import urllib.request, urllib.error
    t = token or DISCORD_BOT_TOKEN
    url = f"{DISCORD_API}{path}"
    body = json.dumps(data).encode() if data else None
    headers = {"Authorization": f"Bot {t}", "Content-Type": "application/json"}
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as r:
            resp_body = r.read().decode("utf-8", errors="replace")
            return r.status, resp_body
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, err_body
    except Exception as e:
        return 0, str(e)


def send_deal_approval_message(deals, research):
    """Send a Discord message with deal candidates and reaction buttons."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        print("  [SKIP] Discord not configured")
        return None

    lines = [
        f"**🐉 QuadStar Deals — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}**",
        f"_{len(research.get('trending_topics', []))} trending topics · {len(research.get('boost_keywords', []))} keywords loaded_",
        "",
        f"**{len(deals)} deal(s) found. React ✅ to post, ❌ to skip:**",
    ]

    for i, d in enumerate(deals, 1):
        bt = f"[{d['brand']}] " if d.get("brand") else ""
        rocket = " 🚀" if d.get("research_boosted") else ""
        lines.append(
            f"**{i}.** {bt}{d['title'][:80]}{rocket}\n"
            f"  ↳ **{d.get('discount_pct', '?')}% OFF** → **${d.get('deal_price', '?')}** (Score: {d.get('score', '?')})"
        )

    lines.append("")
    lines.append("_React within 15 min to approve. Auto-cancels after timeout._")

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n...(truncated)"

    status, resp = discord_request(
        "POST",
        f"/channels/{DISCORD_CHANNEL_ID}/messages",
        {"content": msg}
    )
    if status in (200, 201):
        msg_data = json.loads(resp)
        msg_id = msg_data.get("id")
        print(f"  Discord: sent approval message {msg_id}")
        return msg_id
    else:
        print(f"  [DISCORD ERROR] {status}: {resp[:100]}")
        return None


def add_reactions(message_id, emojis):
    """Add reaction emojis to a Discord message."""
    for emoji in emojis:
        status, resp = discord_request(
            "PUT",
            f"/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}/reactions/{emoji}/@me"
        )
        if status == 204:
            print(f"  Added reaction: {emoji}")
        elif status == 403:
            print(f"  [SKIP] Can't add reaction {emoji} (bot needs 'Add Reactions' permission)")
        time.sleep(0.5)


def wait_for_reaction(message_id, approve_emoji, reject_emoji, timeout=900):
    """
    Poll for reactions on a Discord message.
    Returns 'approve', 'reject', or 'timeout'.
    Polls every 30 seconds.
    """
    print(f"  Waiting for reactions (timeout: {timeout}s)...")
    user_id = None  # Will be set if we can fetch bot info

    start = time.time()
    while time.time() - start < timeout:
        time.sleep(30)
        elapsed = int(time.time() - start)
        print(f"  Polling... ({elapsed}s elapsed)")

        # Fetch message to check reactions
        status, resp = discord_request(
            "GET",
            f"/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}"
        )
        if status != 200:
            continue

        try:
            msg_data = json.loads(resp)
            reactions = msg_data.get("reactions", [])
            for reaction in reactions:
                emoji_name = reaction.get("emoji", {}).get("name", "")
                count = reaction.get("count", 0)
                if emoji_name == approve_emoji and count > 0:
                    print(f"  ✅ Approved by human")
                    return "approve"
                if emoji_name == reject_emoji and count > 0:
                    print(f"  ❌ Rejected by human")
                    return "reject"
        except (json.JSONDecodeError, KeyError):
            pass

    print(f"  ⏰ Timeout ({timeout}s)")
    return "timeout"


# ═══════════════════════════════════════════════════════════════════════════════
# Main scheduler run
# ═══════════════════════════════════════════════════════════════════════════════

def run_scheduled_pipeline():
    """One full pipeline run with human-in-the-loop."""
    print(f"\n{'═'*60}")
    print(f"QuadStar Deals — Scheduled Run {datetime.now().isoformat()}")
    print(f"{'═'*60}")

    # 1. Research
    print("\n[1/5] Research...")
    research = run_research()

    # 2. Scrape
    print("\n[2/5] Scraping...")
    ok, budget = lp.check_budget()
    if not ok:
        print("HALT: budget exceeded")
        return
    print(f"  Budget: ${budget['spent']:.2f}/${lp.MAX_BUDGET}")

    all_deals = []
    for name, fn in [("Slickdeals", lp.fetch_slickdeals),
                      ("TechBargains", lp.fetch_techbargains),
                      ("CamelCamelCamel", lp.fetch_camel),
                      ("AmzBestSellers", lp.fetch_amz_bestsellers),
                      ("AmzDeals", lp.fetch_amz_deals)]:
        try:
            d = fn()
            all_deals.extend(d)
            print(f"  {name}: {len(d)}")
        except Exception as e:
            print(f"  {name}: SKIP ({e})")

    print(f"  Total raw: {len(all_deals)}")

    # 3. Score + filter + dedup
    print("\n[3/5] Scoring...")
    for deal in all_deals:
        s, b, t = lp.score_deal(deal)
        deal["score"] = s
        deal["brand"] = b
        deal["brand_tier"] = t

    filt = [d for d in all_deals
            if d.get("score", 0) >= lp.MIN_SCORE
            and (d.get("discount_pct") or 0) >= lp.MIN_DISCOUNT
            and (d.get("deal_price") or 0) >= lp.MIN_PRICE
            and lp.has_tech(d.get("title", ""))]
    filt.sort(key=lambda x: x["score"], reverse=True)

    existing = lp.load_deals()
    new_deals = lp.dedup(filt, existing)

    # Apply research boosts
    boost_kws = [k.lower() for k in research.get("boost_keywords", [])]
    for deal in new_deals:
        title_l = deal.get("title", "").lower()
        if any(kw in title_l for kw in boost_kws):
            deal["score"] = deal.get("score", 0) + 15
            deal["research_boosted"] = True
        else:
            deal["research_boosted"] = False

    new_deals.sort(key=lambda x: x.get("score", 0), reverse=True)
    top_deals = new_deals[:PIPELINE_MAX_DAILY_POSTS]

    print(f"  Passing filter: {len(filt)}")
    print(f"  New after dedup: {len(new_deals)}")
    print(f"  Top {len(top_deals)}:")
    for d in top_deals:
        boost = " (+15 research)" if d.get("research_boosted") else ""
        print(f"    [{d['score']}] {d.get('discount_pct', '?')}% ${d.get('deal_price', '?')} | {d['title'][:60]}{boost}")

    if not top_deals:
        print("\n  No deals found. Sending research-only notification.")
        _discord_research_notification(research)
        return

    # 4. Human approval via Discord
    print("\n[4/5] Awaiting human approval...")
    msg_id = send_deal_approval_message(top_deals, research)

    if msg_id:
        add_reactions(msg_id, ["✅", "❌"])
        decision = wait_for_reaction(msg_id, "✅", "❌", timeout=900)  # 15 min timeout

        if decision != "approve":
            print(f"  Decision: {decision}. Not posting.")
            _discord_send(f"⏰ **QuadStar Deals** — Run cancelled ({decision}). {len(top_deals)} deals skipped.")
            return
    else:
        print("  [WARN] Could not send approval message. Auto-approving top 2 deals.")
        top_deals = top_deals[:2]

    # 5. Post approved deals
    print("\n[5/5] Posting...")
    tid = PLATFORM_IDS.get("twitter", "")
    if not POSTIZ_API_KEY or not tid:
        print("  [ERROR] Postiz not configured")
        return

    posted = []
    for d in top_deals:
        tweet = lp.build_tweet(d)
        print(f"  Posting: {d['title'][:60]}")
        ok, resp = lp.post_tweet(POSTIZ_API_URL, POSTIZ_API_KEY, tid, tweet)
        if ok:
            print(f"    ✓ Posted")
            d["is_posted"] = True
            d["posted_at"] = datetime.now(timezone.utc).isoformat()
            posted.append(d)
        else:
            print(f"    ✗ Failed: {resp[:80]}")
            d["is_posted"] = False
        time.sleep(3)

    # Save
    nid = max([e.get("id", 0) for e in existing], default=0)
    for d in posted:
        nid += 1
        existing.append({
            "title": d.get("title", ""), "asin": d.get("asin"),
            "deal_price": d.get("deal_price"), "discount_pct": d.get("discount_pct"),
            "score": d.get("score"), "brand": d.get("brand"),
            "source": d.get("source"), "id": nid,
            "is_posted": True, "research_boosted": d.get("research_boosted", False),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "posted_at": d.get("posted_at"),
        })
    lp.save_deals(existing)
    lp.update_budget(budget, len(posted), 3)

    # Summary
    _discord_summary(research, posted, len(top_deals))
    print(f"\nDone. {len(posted)}/{len(top_deals)} posted.")


def _discord_research_notification(research):
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return
    lines = [
        f"**🐉 QuadStar Research Brief — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}**",
        f"📊 No new deals this run. Research intel loaded:",
    ]
    if research.get("trending_topics"):
        lines.append(f"  🔥 {', '.join(research['trending_topics'][:6])}")
    if research.get("seasonal_signals"):
        lines.append(f"  🗓️ {', '.join(research['seasonal_signals'][:3])}")
    _discord_send("\n".join(lines))


def _discord_summary(research, posted, total):
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return
    lines = [f"**🐉 QuadStar Deals — Posted {len(posted)}/{total}**"]
    for d in posted:
        bt = f"[{d['brand']}] " if d.get("brand") else ""
        rocket = " 🚀" if d.get("research_boosted") else ""
        lines.append(
            f"• {bt}{d['title'][:80]}{rocket}\n"
            f"  ↳ **{d.get('discount_pct', '?')}% OFF → ${d.get('deal_price', '?')}**"
        )
    if research.get("seasonal_signals"):
        lines.append(f"\n🗓️ Seasonal: {', '.join(research['seasonal_signals'][:3])}")
    _discord_send("\n".join(lines))


def _discord_send(message):
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return
    import urllib.request, urllib.error
    url = f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages"
    data = json.dumps({"content": message}).encode()
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"  Discord: sent ({r.status})")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"  [DISCORD ERROR] {e.code}: {err[:100]}")
    except Exception as e:
        print(f"  [DISCORD ERROR] {e}")


if __name__ == "__main__":
    jitter = random.randint(0, 180)
    print(f"[JITTER] Sleeping {jitter}s...")
    time.sleep(jitter)
    run_scheduled_pipeline()
