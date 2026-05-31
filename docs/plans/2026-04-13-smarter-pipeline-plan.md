# Smarter Pipeline Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the deals bot intelligent with multi-factor deal scoring, Discord action notifications, LLM-first price drop content, fast-track hot deals, and a closed feedback loop.

**Architecture:** Five independent features layered onto the existing pipeline. Scoring model is the foundation (Task 1-2), then notifications (Task 3), price drop revamp (Task 4-5), fast-track trigger (Task 6), and feedback loop (Task 7). Each task is a commit.

**Tech Stack:** Python, FastAPI, discord.py, APScheduler, LLM (Anthropic Claude Haiku via src/llm.py)

**Design Doc:** `docs/plans/2026-04-13-smarter-pipeline-design.md`

---

### Task 1: Deal Scoring Model - Core Function

**Files:**
- Modify: `config/settings.py` (add scoring config)
- Modify: `src/database.py` (add `score_deal()` function)

**Step 1: Add scoring config to settings.py**

Add after the `AUTO_APPROVE_SCORE` line (~line 70):

```python
# Deal scoring weights (0-100 composite score)
SCORE_WEIGHT_DISCOUNT = 25
SCORE_WEIGHT_BRAND = 20
SCORE_WEIGHT_PRICE_RANGE = 15
SCORE_WEIGHT_ENGAGEMENT = 15
SCORE_WEIGHT_BADGE = 10
SCORE_WEIGHT_FRESHNESS = 10
SCORE_WEIGHT_TRENDING = 5

# Fast-track: score >= this triggers immediate Discord card
FAST_TRACK_SCORE = float(os.getenv("FAST_TRACK_SCORE", "85"))

# Known brand tiers for scoring
BRAND_TIER_1 = [
    "apple", "sony", "bose", "samsung", "lg", "dell", "hp", "lenovo",
    "asus", "microsoft", "google", "nvidia", "amd", "intel", "logitech",
    "razer", "corsair", "steelseries", "jbl", "sennheiser",
]
BRAND_TIER_2 = [
    "anker", "tp-link", "netgear", "western digital", "seagate", "crucial",
    "hyperx", "elgato", "shokz", "jabra", "philips", "epson", "brother",
    "roku", "amazon", "fire", "echo", "ring", "eufy", "roborock",
]
```

**Step 2: Add score_deal() to database.py**

Add new function in `src/database.py`. This is the core scoring engine.

```python
def score_deal(deal: dict) -> float:
    """Score a deal 0-100 using weighted multi-factor model.
    
    Factors: discount, brand tier, price sweet spot, historical engagement,
    lowest-ever badge, source freshness, trending signal.
    """
    from config.settings import (
        SCORE_WEIGHT_DISCOUNT, SCORE_WEIGHT_BRAND, SCORE_WEIGHT_PRICE_RANGE,
        SCORE_WEIGHT_ENGAGEMENT, SCORE_WEIGHT_BADGE, SCORE_WEIGHT_FRESHNESS,
        SCORE_WEIGHT_TRENDING, BRAND_TIER_1, BRAND_TIER_2,
    )
    from datetime import datetime

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

    # 4. Historical engagement (0-15): check tweet_performance for similar products
    engagement_score = _get_engagement_score(deal, SCORE_WEIGHT_ENGAGEMENT)
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


def _get_engagement_score(deal: dict, max_weight: float) -> float:
    """Check tweet_performance.json for similar past deals (same category/brand)."""
    import json
    perf_path = os.path.join(DATA_DIR, "tweet_performance.json")
    if not os.path.exists(perf_path):
        return max_weight * 0.5  # neutral if no data yet

    try:
        with open(perf_path) as f:
            records = json.load(f)
    except (json.JSONDecodeError, IOError):
        return max_weight * 0.5

    if not records:
        return max_weight * 0.5

    # Find records with matching category or brand keywords
    title_lower = deal.get("title", "").lower()
    category = deal.get("category", "tech")
    relevant = []
    for r in records:
        r_title = r.get("tweet_text", "").lower()
        if category in r.get("category", "") or any(
            word in r_title for word in title_lower.split()[:3]
            if len(word) > 4
        ):
            eng = r.get("engagement_score", 0)
            if eng > 0:
                relevant.append(eng)

    if not relevant:
        return max_weight * 0.5  # neutral

    avg_engagement = sum(relevant) / len(relevant)
    # Normalize: avg engagement of 5+ is excellent
    normalized = min(avg_engagement / 5.0, 1.0)
    return normalized * max_weight


def _get_trending_score(asin: str, max_weight: float) -> float:
    """Check if ASIN appeared in previous scrape runs (popular product)."""
    deals = _load_deals()
    appearances = sum(1 for d in deals if d.get("asin") == asin)
    if appearances >= 3:
        return max_weight
    elif appearances >= 2:
        return max_weight * 0.6
    return 0
```

**Step 3: Wire scoring into get_unposted_deals**

In `src/database.py`, find `get_unposted_deals()` and replace the sorting logic. Currently it sorts by `discount_pct`. Change to sort by `score_deal()` and attach the score to each deal dict.

Look for the return statement and add:
```python
for d in unposted:
    d["deal_score"] = score_deal(d)
unposted.sort(key=lambda d: d["deal_score"], reverse=True)
```

**Step 4: Verify scoring works**

Run manually:
```bash
cd /Users/nitesh/Projects/quadstar-deals
source venv/bin/activate
python3 -c "
from src.database import get_unposted_deals, score_deal, _load_deals
deals = _load_deals()[-10:]
for d in deals:
    s = score_deal(d)
    print(f'{s:5.1f} | {d.get(\"discount_pct\",0):4.0f}% | {d[\"title\"][:55]}')
"
```

Expected: Scores vary based on brand/price/discount, not just discount alone. A MacBook at 36% should score higher than a no-name USB hub at 50%.

**Step 5: Commit**

```bash
git add config/settings.py src/database.py
git commit -m "feat: add multi-factor deal scoring model (0-100)"
```

---

### Task 2: Price Drop Threshold Updates

**Files:**
- Modify: `config/settings.py` (update thresholds)
- Modify: `src/price_monitor.py` (add alert-only tier, min dollar drop)

**Step 1: Update thresholds in settings.py**

Change existing:
```python
MIN_REPOST_DROP_PCT = float(os.getenv("MIN_REPOST_DROP_PCT", "20"))  # was 15
```

Add new settings after it:
```python
# Price drop alert threshold (Discord FYI, no repost card)
MIN_ALERT_DROP_PCT = float(os.getenv("MIN_ALERT_DROP_PCT", "10"))
# Minimum dollar savings to qualify for repost
MIN_REPOST_DROP_DOLLARS = float(os.getenv("MIN_REPOST_DROP_DOLLARS", "15"))
```

**Step 2: Update price_monitor.py**

In `_evaluate_repost_candidates()`, add the dollar threshold check alongside the percentage check. Also add the alert-only tier for 10-19% drops.

In `detect_drops()`, add logic for the alert-only Discord webhook message for drops between 10-19%.

Key changes:
- Drops < 10%: ignore completely (no alert, no noise)
- Drops 10-19%: send Discord webhook alert only (FYI embed, yellow color)
- Drops >= 20% AND >= $15 savings: full repost pipeline with approval card
- Lowest ever at any qualifying drop: immediate card

**Step 3: Verify**

Restart server and check logs. The $0.01 HP laptop drop should now be silently ignored.

**Step 4: Commit**

```bash
git add config/settings.py src/price_monitor.py
git commit -m "fix: price drop thresholds - 20% repost, 10% alert, $15 min savings"
```

---

### Task 3: Discord Action Notifications

**Files:**
- Modify: `src/discord_bot.py` (add confirmation embeds after every button action)

**Step 1: Add notification helper function**

Add a helper at the top of discord_bot.py that sends color-coded embed replies:

```python
async def _send_action_notification(interaction, title: str, description: str, color_type: str = "success"):
    """Send a color-coded embed notification as a follow-up message."""
    import discord
    colors = {
        "success": discord.Color.green(),     # approved, published
        "action": discord.Color.gold(),       # rescheduled, auto-approved
        "error": discord.Color.red(),         # failed, rejected
        "info": discord.Color.blue(),         # fast-track, alerts
    }
    embed = discord.Embed(
        title=title,
        description=description,
        color=colors.get(color_type, discord.Color.greyple()),
    )
    try:
        await interaction.followup.send(embed=embed)
    except Exception:
        pass  # non-critical, don't break the flow
```

**Step 2: Add notifications to each button callback**

For each existing button handler:

- **DealApproveButton.callback**: After successful schedule, send green embed: "Deal scheduled for [time] on [platform]"
- **DealPostNowButton.callback**: After schedule, send green embed: "Deal posting in 5 minutes on [platform]"  
- **TimeSelect.callback**: After reschedule, send yellow embed: "Rescheduled to [time] on [platform]"
- **DealRejectButton.callback**: Send red ephemeral embed: "Deal rejected and marked inactive"
- **Auto-approve path** in agent.py: Send yellow embed via webhook: "Auto-approved: [title] ([score], [discount]%)"

Each notification should reference the deal title (truncated) and the action taken.

**Step 3: Add notification to price drop card buttons**

Same pattern for `PriceDropApproveButton` and `PriceDropRejectButton`.

**Step 4: Test manually**

Trigger a deal card in Discord, approve it, verify the green confirmation embed appears as a follow-up message.

**Step 5: Commit**

```bash
git add src/discord_bot.py src/agent.py
git commit -m "feat: Discord action notifications with color-coded embeds"
```

---

### Task 4: Price Drop Content - LLM-First

**Files:**
- Modify: `src/notifier.py` (rewrite price drop content generation)

**Step 1: Rewrite _build_price_drop_tweet_1 template**

Replace the "UPDATE: This just dropped another X%" format with "PRICE DROP ALERT" style. This is the fallback template only (LLM writes primary content).

```python
def _build_price_drop_tweet_1(drop_info: dict, badge: str) -> str:
    """Fallback template for price drop tweet 1 (only used if LLM fails)."""
    title = drop_info.get("title", "")[:60]
    new_price = drop_info.get("new_price", 0)
    original = drop_info.get("original_posted_price", drop_info.get("old_price", 0))
    drop_pct = drop_info.get("drop_pct", 0)
    savings = original - new_price

    badge_line = f"\n{badge}" if badge else ""
    tweet = (
        f"PRICE DROP ALERT\n\n"
        f"{title}\n"
        f"Was ${original:.2f} > Now ${new_price:.2f}\n"
        f"You save ${savings:.0f} ({drop_pct:.0f}% off)"
        f"{badge_line}\n\n"
        f"Link below\n#ad #PriceDrop"
    )
    if len(tweet) > 280:
        tweet = tweet[:277] + "..."
    return tweet
```

**Step 2: Rewrite _build_price_drop_tweet_2 template**

Remove all price mentions. Short CTA only.

```python
def _build_price_drop_tweet_2(drop_info: dict) -> str:
    """Fallback template for price drop tweet 2 (only used if LLM fails)."""
    url = drop_info.get("affiliate_url", "")
    return f"Check the link before it's gone.\n\n{url}"
```

**Step 3: Update LLM prompts in generate_price_drop_content()**

Update the LLM prompt to:
- Always use "PRICE DROP ALERT" style opener for tweet_1
- Show was/now/savings in tweet_1
- Write creative CTA for tweet_2 with NO prices (Amazon prices are dynamic)
- Include affiliate URL in tweet_2
- LinkedIn: professional, specs, no urgency hype

The LLM path should be the PRIMARY path, not the fallback. Restructure the function so LLM is called first and template is the except/fallback.

**Step 4: Verify**

```bash
python3 -c "
from src.notifier import generate_price_drop_content
test_drop = {
    'title': 'Sony WH-1000XM5 Noise Canceling Headphones',
    'new_price': 248.0, 'old_price': 398.0, 'drop_pct': 37.7,
    'original_posted_price': 348.0,
    'is_lowest_ever': True, 'is_lowest_90d': True,
    'affiliate_url': 'https://amazon.com/dp/B0BX2L8PW2?tag=quadstar0e-20',
    'image_url': 'https://m.media-amazon.com/images/I/51BKhBYkxML.jpg',
}
content = generate_price_drop_content(test_drop)
print('=== TWEET 1 ===')
print(content['tweet_1'])
print('=== TWEET 2 ===')
print(content['tweet_2'])
"
```

Expected: LLM-generated creative content with PRICE DROP ALERT style, no "UPDATE:" prefix. Tweet 2 has no prices.

**Step 5: Commit**

```bash
git add src/notifier.py
git commit -m "feat: LLM-first price drop content, PRICE DROP ALERT format"
```

---

### Task 5: Price Drop Alert-Only Discord Embed

**Files:**
- Modify: `src/price_monitor.py` (add alert-only embed for 10-19% drops)

**Step 1: Add alert embed function**

In price_monitor.py, add a function that sends a yellow Discord webhook embed for moderate drops (10-19%):

```python
def _send_price_alert(drop_info: dict):
    """Send a yellow Discord embed for moderate price drops (FYI, no repost)."""
    import requests
    from config.settings import DISCORD_WEBHOOK_URL
    if not DISCORD_WEBHOOK_URL:
        return

    title = drop_info.get("title", "")[:80]
    new_price = drop_info.get("new_price", 0)
    old_price = drop_info.get("old_price", 0)
    drop_pct = drop_info.get("drop_pct", 0)
    url = drop_info.get("affiliate_url", "")
    image_url = drop_info.get("image_url", "")

    embed = {
        "title": f"Price Drop: {title}",
        "description": (
            f"**Was:** ${old_price:.2f}\n"
            f"**Now:** ${new_price:.2f} (-{drop_pct:.1f}%)\n"
        ),
        "color": 0xFFA500,  # orange/yellow
        "url": url if url else None,
        "footer": {"text": "Price monitoring - alert only (below repost threshold)"},
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
```

**Step 2: Wire into detect_drops()**

In the drop processing loop, add the alert-only tier:
- drop_pct >= 20 AND savings >= $15: existing repost pipeline
- drop_pct >= 10 AND drop_pct < 20: call `_send_price_alert()`
- drop_pct < 10: ignore completely

**Step 3: Verify**

Check that the 1-cent HP laptop drop no longer triggers any notification.

**Step 4: Commit**

```bash
git add src/price_monitor.py
git commit -m "feat: tiered price drop alerts - ignore <10%, alert 10-19%, repost 20%+"
```

---

### Task 6: Fast-Track Hot Deal Trigger

**Files:**
- Modify: `src/api.py` (add 30-min fast-track scheduler job)
- Modify: `src/agent.py` (add fast-track check function)
- Modify: `config/categories.py` (tag high-yield URLs)

**Step 1: Tag high-yield URLs in categories.py**

Add a `fast_track_urls` list to the tech category config. These are the 2-3 pages that rotate most frequently:

```python
"fast_track_urls": [
    ("https://www.amazon.com/gp/goldbox", "Gold Box"),
    ("https://www.amazon.com/gp/movers-and-shakers/electronics", "Electronics Movers"),
    ("https://www.amazon.com/gp/movers-and-shakers/pc", "PC Movers"),
],
```

**Step 2: Add fast-track scrape function in agent.py**

```python
def _fast_track_check() -> str:
    """Quick scrape of high-yield pages. If any deal scores >= FAST_TRACK_SCORE, send card immediately."""
    from config.settings import FAST_TRACK_SCORE
    from src.amazon_scraper import scrape_amazon_deals
    from src.database import score_deal, save_deal, get_unposted_deals

    # scrape_amazon_deals with fast_track=True uses only fast_track_urls
    deals = scrape_amazon_deals(category_name="tech", fast_track=True)
    
    hot_deals = []
    for deal in deals:
        if save_deal(deal):  # only new deals
            deal["deal_score"] = score_deal(deal)
            if deal["deal_score"] >= FAST_TRACK_SCORE:
                hot_deals.append(deal)

    if hot_deals:
        # Send immediate Discord cards for hot deals
        _generate_and_send_cards(limit=len(hot_deals))
        return f"Fast-track: {len(hot_deals)} hot deals sent to Discord"
    
    return "Fast-track: no hot deals found"
```

**Step 3: Add fast_track parameter to scrape_amazon_deals**

In `src/amazon_scraper.py`, modify `scrape_amazon_deals()` to accept `fast_track=False` parameter. When True, use `fast_track_urls` instead of `amazon_urls`.

```python
def scrape_amazon_deals(category_name: str = "tech", fast_track: bool = False) -> list:
    cat = _get_category_config(category_name)
    amazon_pages = cat.get("fast_track_urls", []) if fast_track else cat.get("amazon_urls", [])
    # ... rest unchanged
```

**Step 4: Add 30-min scheduler job in api.py**

Add after the existing scheduler jobs:

```python
scheduler.add_job(_fast_track_run, "cron", minute="*/30", id="fast_track")
print(f"  [scheduler] Fast-track check every 30 min")
```

With the async wrapper:
```python
async def _fast_track_run():
    """Fast-track check: quick scrape of high-yield pages for hot deals."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _run_fast_track)

def _run_fast_track():
    from src.agent import _fast_track_check
    result = _fast_track_check()
    if "hot deals" in result:
        print(f"  [scheduler] {result}")
```

**Step 5: Verify**

Restart server, check logs at the next :00 or :30 mark for fast-track run.

**Step 6: Commit**

```bash
git add config/categories.py src/amazon_scraper.py src/agent.py src/api.py
git commit -m "feat: fast-track hot deal detector - 30min check, score >= 85 triggers immediately"
```

---

### Task 7: Feedback Loop Closure

**Files:**
- Modify: `src/tweet_learner.py` (add `get_style_guidance()` function)
- Modify: `src/notifier.py` (inject style guidance into LLM prompts)

**Step 1: Add style guidance extractor to tweet_learner.py**

```python
def get_style_guidance() -> str:
    """Return LLM-readable style guidance based on tweet performance data.
    
    Returns a string like:
    "Based on past performance:
    - Best hook style: caps (avg 8.2 engagement)
    - Best CTA style: urgency (avg 6.1 engagement)
    - Optimal tweet length: 180-220 chars
    - Emoji density: 2-4 per tweet performs best"
    """
```

This function reads `tweet_performance.json`, aggregates by style attributes, and returns a formatted string. If not enough data (< 10 records with engagement), return empty string (don't give bad advice from tiny sample).

**Step 2: Inject guidance into notifier.py LLM prompts**

In `generate_deal_content()` and `generate_price_drop_content()`, before the LLM call:

```python
from src.tweet_learner import get_style_guidance
style_hint = get_style_guidance()
if style_hint:
    prompt += f"\n\nStyle guidance from past performance:\n{style_hint}"
```

This lets the LLM naturally incorporate what's working without hard-coding rules.

**Step 3: Verify**

```bash
python3 -c "
from src.tweet_learner import get_style_guidance
print(get_style_guidance() or 'Not enough data yet (need 10+ records)')
"
```

**Step 4: Commit**

```bash
git add src/tweet_learner.py src/notifier.py
git commit -m "feat: close feedback loop - tweet performance insights auto-feed into LLM prompts"
```

---

### Task 8: Final Integration Test + Server Restart

**Files:**
- No new files

**Step 1: Run full pipeline manually**

```bash
python3 -c "
from src.agent import run_agent
result = run_agent('Scrape deals for tech, then send top 5 unposted deals to Discord for approval.')
print(result)
"
```

Verify:
- Deals are scored (not just sorted by discount)
- Discord cards appear
- Approving a card sends a green confirmation embed
- Rejecting sends a red ephemeral embed

**Step 2: Restart server**

```bash
launchctl stop com.quadstar.deals && sleep 2 && launchctl start com.quadstar.deals
curl -s http://localhost:8002/health
```

**Step 3: Push to GitHub**

```bash
git push origin master
```
