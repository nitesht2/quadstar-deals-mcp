# Agent Profiles

Each YAML file here defines a self-contained niche agent. The same codebase runs
as "QuadStar Tech Deals", "QuadStar Sneakers Deals", "QuadStar Home Deals", etc. —
just by flipping the `AGENT_PROFILE` env var and running a second instance on a
different port.

## How It Works

`config/settings.py` reads `AGENT_PROFILE` (defaults to `tech`). If
`profiles/<AGENT_PROFILE>.yaml` exists, its values override the defaults for:

- `BRAND_NAME` / `BRAND_HASHTAG`
- `AMAZON_AFFILIATE_TAG`
- `TECH_KEYWORDS` (the per-niche content keyword filter)
- `DEAL_SOURCES` (aggregator URLs scraped via Firecrawl)
- `BRAND_TIER_1` / `BRAND_TIER_2` (for deal scoring)

Data is also isolated — each profile writes to `data/<profile>/` so nothing clobbers
anything else.

## Running a Second Agent (e.g. Sneakers)

```bash
# 1. Copy a template
cp profiles/sneakers.yaml.example profiles/sneakers.yaml

# 2. Create a dedicated .env.sneakers with its own Discord/Postiz creds
# (don't reuse tech's Discord bot — each niche deserves its own audience)

# 3. Start the second server on a different port
AGENT_PROFILE=sneakers \
DATA_DIR=./data/sneakers \
DISCORD_BOT_TOKEN=... \
DISCORD_CHANNEL_ID=... \
uvicorn src.api:app --port 8002
```

Point a second OpenClaw webhook route at `localhost:8002` and you're live.

## Replication Checklist for a New Niche

1. **Keywords** — what words must appear in a deal title to qualify?
2. **Aggregators** — which deal sites cover this niche? (Slickdeals has niche
   categories; sneakers needs StockX/SNKRS; home needs HomeDepot/Lowes)
3. **Brands** — tier your top brands for scoring
4. **Amazon URLs** — best-seller and deal pages specific to the category
5. **Social accounts** — new Discord, Twitter, etc. for the niche
6. **Affiliate tag** — separate Amazon Associates tag per vertical
