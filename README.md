# QuadStar Deals

Agentic multi-platform Amazon tech deal pipeline. Scrapes Amazon deals, generates dynamic tweet content via **DeepSeek V4 Flash**, schedules to social platforms via Postiz, and auto-posts to [@quadstardeals](https://x.com/quadstardeals) on X/Twitter.

## Architecture

```
#quadstar-deal (Discord channel)
  |
  ├── APScheduler (4 peak times, randomized)
  │     Runs pipeline: scrape → score → generate → post
  │     Cron: 8am, 12pm, 5pm, 7pm PST (with ±30min jitter)
  |
  └── QuadstarDeals#3389 (discord.py bot)
        Auto-approve notifications + price drop cards
        |
        v
    FastAPI (localhost:8001)
      |
      ├── DeepSeek V4 Flash (api.deepseek.com) -- ~$0.10-0.30/month
      │     Context caching: $0.0028/1M input tokens
      │
      └── Postiz (localhost:4007, Docker)
            |
            v
          @quadstardeals on X/Twitter
            ~25% single tweet, ~75% two-post format (randomized)
```

## How It Works

1. **Scrape** -- Scrapling (stealth) + Playwright (fallback) scrape Amazon deal pages
2. **Filter** -- Tech-only keywords, $50+ price, 15%+ discount, 24h freshness
3. **Store** -- JSON file storage, deduplicate by ASIN, track price history
4. **Generate** -- DeepSeek V4 Flash writes unique tweet content per deal with brand voice rules
5. **Auto-post** -- Deals pass through gates (discount, score, confidence, price verify), then schedule to Postiz
6. **Format rotation** -- ~25% single tweet, ~75% two-post thread (randomized to avoid X pattern detection)
7. **Monitor** -- Postiz healthcheck watchdog auto-restarts Docker + containers every 5 min

## Posting Schedule

4 runs/day at randomized peak times (PST):
- 8:00 AM window (±30 min jitter)
- 12:00 PM window (±30 min jitter)
- 5:00 PM window (±30 min jitter)
- 7:00 PM window (±30 min jitter)

Each run scrapes + scores deals. Max 4 auto-posts per day.

## Two-Post Format (X Algorithm Optimized)

**Tweet 1** (all platforms, no link):
- Short hook
- Product name + 3 features
- No link (avoids ~50% reach penalty)
- Native image (2x boost)
- Max 2 hashtags

**Tweet 2** (X/Twitter reply, posted as thread):
- Short CTA + affiliate link
- Replies boost parent thread (~54x signal)

**~25% of posts use single-tweet format** (link embedded directly, no reply thread) — randomized to break detectable bot patterns.

## Brand Voice

All tweet content is filtered through [Nitesh's brand voice rules](https://github.com/nitesht2/quadstar-deals/tree/master/config/brand_voice.md):
- ~220 banned AI words (no "leverage", "seamless", "robust", etc.)
- Anti-slop checklist (concrete details, no hype, stairway ordering)
- No em dashes, no semicolons
- Prices from verified live data, never invented

## Quick Start

### Prerequisites

- Python 3.11+
- DeepSeek API key (get at https://platform.deepseek.com/api_keys)
- Docker (for Postiz)
- Discord server with bot permissions

### 1. Clone and install

```bash
git clone https://github.com/nitesht2/quadstar-deals.git
cd quadstar-deals
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
scrapling install
playwright install chromium --with-deps
```

### 2. Set up DeepSeek API

```bash
# Get your key at https://platform.deepseek.com/api_keys
# Add to .env:
# DEEPSEEK_API_KEY=sk-your_key_here
```

Model: `deepseek-v4-flash`. Context caching is automatic — repeated system prompts hit cache for $0.0028/1M input tokens.

### 3. Set up Postiz

```bash
# Clone and start Postiz
git clone https://github.com/gitroomhq/postiz-app.git postiz
cd postiz
docker compose up -d
# Connect your X/Twitter account in the UI at http://localhost:4007
# Get your API key from Settings
# Get integration IDs: curl http://localhost:4007/api/integrations -H "Authorization: YOUR_KEY"
```

### 4. Set up Discord

Create a Discord bot in the Developer Portal:
- New Application > Bot > Reset Token
- Enable **Message Content Intent**
- OAuth2 > URL Generator > Scope: bot > Permissions: Send Messages, Read Messages/View Channels, Embed Links, Read Message History
- Invite to your server and grant channel access

### 5. Configure environment

```bash
cp .env.example .env
# Fill in: DEEPSEEK_API_KEY, DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID,
#          POSTIZ_API_KEY, POSTIZ_TWITTER_ID
```

### 6. Start the server

```bash
uvicorn src.api:app --host 0.0.0.0 --port 8001
```

**Note:** Port 8001 (not 8000). Port 8000 is typically used by Hermes Agent.

### 7. Auto-start on macOS (launchd)

```bash
# The server is managed by launchd:
#   /Users/nitesh/Library/LaunchAgents/com.nitesh.quadstar-deals.plist
# Postiz healthcheck runs every 5 min:
#   /Users/nitesh/Library/LaunchAgents/com.nitesh.postiz.healthcheck.plist
```

## Agent Tools

The Hermes agent drives this backend via `/webhook/hermes`. The backend exposes
these tools (dispatched deterministically by `tool_router.dispatch()` — one intent
classify call, then a direct function call; no agent framework runs here):

| Tool | Description |
|---|---|
| `scrape_deals(category)` | Scrape Amazon + aggregators for deals |
| `get_unposted_deals(limit)` | Fetch top ranked unposted deals |
| `generate_and_send_cards()` | Generate tweets + send Discord approval cards |
| `schedule_to_postiz(deal_id, platforms)` | Schedule approved deal to social platforms |
| `read_feedback()` | Process Discord reactions/comments into preferences |
| `get_status()` | Pipeline stats: active deals, posted, categories |
| `add_category(name, keywords)` | Add new product category at runtime |

## Posting Gates (Auto-Approval Criteria)

Deals must pass ALL gates to auto-post:

1. **Discount**: >= 35% (`PIPELINE_MIN_DISCOUNT`)
2. **Score**: >= 58 (`PIPELINE_MIN_SCORE`, 0-100 composite)
3. **ASIN cooldown**: Same product not posted within 7 days
4. **Content confidence**: >= 0.85 (`PIPELINE_MIN_CONFIDENCE`)
5. **Price verification**: Live Amazon price check matches within tolerance
6. **Daily cap**: Max 4 deals per day (`PIPELINE_MAX_DAILY_POSTS`)

## Cost Breakdown

| Component | Monthly Cost |
|---|---|
| DeepSeek V4 Flash API | ~$0.10-0.30 (cache hit: $0.0028/1M input) |
| Postiz (self-hosted) | Free |
| Discord bots | Free |
| Amazon Associates | Free |
| Scrapling + Playwright | Free |
| **Total** | **~$0.10-0.30/month** |

## Environment Variables

| Variable | Description | Required |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API key (platform.deepseek.com) | Yes |
| `DISCORD_BOT_TOKEN` | Discord bot token for deal cards | Yes |
| `DISCORD_CHANNEL_ID` | Channel ID for deal cards | Yes |
| `DISCORD_WEBHOOK_URL` | Webhook URL for one-way notifications | No |
| `AMAZON_AFFILIATE_TAG` | Amazon Associates tag (default: `quadstar0e-20`) | Yes |
| `POSTIZ_API_URL` | Postiz API URL (default: `http://localhost:4007/api`) | Yes |
| `POSTIZ_API_KEY` | Postiz API key from Settings | Yes |
| `POSTIZ_TWITTER_ID` | Postiz integration ID for @quadstardeals | Yes |
| `FIRECRAWL_API_KEY` | Firecrawl key for aggregator scraping | No |
| `PIPELINE_MAX_DAILY_POSTS` | Max auto-posts per day (default: 4) | No |
| `LLM_MODEL` | Override DeepSeek model (default: `deepseek-v4-flash`) | No |

## Project Structure

```
quadstar-deals/
├── config/
│   ├── settings.py              # All configuration and env vars
│   ├── categories.py            # Category registry (tech + runtime-addable)
│   └── brand_voice.md           # Brand voice rules (~220 banned words)
├── src/
│   ├── agent.py                 # Backend tools + deterministic pipeline (driven by Hermes)
│   ├── api.py                   # FastAPI server (APScheduler + webhook)
│   ├── discord_bot.py           # Deal cards with persistent buttons
│   ├── llm.py                   # DeepSeek API interface (single provider)
│   ├── postiz_client.py         # Postiz scheduler (format rotation)
│   ├── notifier.py              # Tweet generation + brand voice
│   ├── amazon_scraper.py        # Amazon scraping (Scrapling + Playwright)
│   ├── scraper.py               # Firecrawl aggregator scraping
│   ├── database.py              # JSON storage, dedup, freshness
│   └── ... (price monitor, tweet learner, etc.)
├── data/                        # Runtime database (gitignored)
├── .env.example                 # Template with all env vars
└── requirements.txt
```

## Infrastructure Reliability

- **Postiz healthcheck watchdog** runs every 5 min via launchd
- Auto-starts Docker Desktop if down
- Auto-runs `docker compose up -d` if Postiz container is missing
- Logs to `/tmp/postiz-healthcheck.log`
