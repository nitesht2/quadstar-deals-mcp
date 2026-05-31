# Quadstar Deals — Daily Deal Flow & Market Research Pipeline

## Purpose
Research deal flow, market trends, and relevant intelligence for @quadstardeals (Amazon affiliate: tech, gaming, smart home, audio, laptops, accessories). Output a structured brief that identifies what to post, what's trending, and what to watch.

## Pipeline Stages

### Stage 1: X Search — Deal Flow Pulse
Use x_search 3-5 queries to find active deals and trending products:
- Query 1: "Amazon deal tech today 2026"
- Query 2: "Amazon Prime deal laptop gaming"
- Query 3: "best Amazon deals headphones smart home"
- Query 4: "Amazon Lightning Deal SSD storage"
- Query 5: "Amazon deal under $50 gadget" (if applicable)
Extract: product names, prices, discount %, source accounts, engagement signals.

### Stage 2: X Search — Market Trends
Use x_search 2-3 queries for broader market context:
- Query 1: "best selling tech products Amazon 2026"
- Query 2: "trending gadgets launch May 2026"
- Query 3: "Amazon affiliate marketing tips deals"
Extract: trending categories, new product launches, seasonal demand patterns.

### Stage 3: X Search — Competitor Intel
Use x_search to check what competitors are posting:
- Query: "site:x.com deal Amazon tech"
- Look for: @ScottyDeals, @TechDropsDeals, @BigDealsHunter, @FariaAragonez
- Extract: what categories they're posting, engagement levels, any deals we're missing.
Note: never copy. Use for gap analysis only.

### Stage 4: Synthesis & Filtering
Apply Quadstar Deals filter criteria:
- Categories we post: laptops, gaming, headphones/audio, smart home, SSDs, accessories
- Price range: $20-$1500 (sweet spot $50-$500)
- Minimum discount: 15% off or notable value
- No: kitchen, fashion, supplements, baby products (not our niche)
- Priority: gaming laptops, Apple products, Sony/Samsung/Audio-Technica audio, SSDs, smart home (Ring, Echo, Nest)
Rank deals by: discount % × estimated demand × affiliate commission potential.

### Stage 5: Obsidian Recall (Hindsight)
Before writing the report, search your Obsidian vault for:
- Previously posted deals on the same products (avoid duplicates)
- Historical price data (is this actually a good deal or a fake discount?)
- Any notes on product quality/returns (don't promote lemons)
- Seasonal patterns (e.g., laptops spike during back-to-school, Oct-Nov)

### Stage 6: Output — Structured Research Report
Write the report to /Users/nitesh/Projects/quadstar-deals/data/pipeline/research-YYYY-MM-DD.md

Report structure:
```
# Quadstar Deals Research — YYYY-MM-DD

## Top Deals to Post Today
| Product | Category | Price | Discount | Source | Priority |
|---------|----------|-------|----------|--------|----------|
| ...     | ...      | ...   | ...      | ...    | High/Med/Low |

## Market Trends This Week
- [Trend 1]: what's happening, why it matters for deal flow
- [Trend 2]: ...

## Competitor Gap Analysis
- Deals competitors posted that we missed: [list]
- Categories they're heavy on: [list]
- Our differentiation opportunity: [note]

## Price Watch / Historical Notes
- [Product]: previously posted at $X, now at $Y — [better/worse/same]
- [Product]: fake discount alert (inflated MSRP)

## What to Watch Next 2 Weeks
- [Event/launch/sale]: expected date, what to prepare for
- [Product category]: seasonal demand coming up

## Recommended Posts (Ready to Go)
1. [Product name] — [one-line hook] — [estimated commission range]
2. ...
```

## Rules
- Never post a deal without checking Obsidian for duplicates first
- If a deal was posted within 7 days, skip it
- Flag any product with known quality issues
- Keep the report under 1000 words — concise, scannable, actionable
- Save to Obsidian vault under: Projects/quadstar-deals/research/YYYY-MM-DD.md
