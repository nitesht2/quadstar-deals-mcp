# Architecture

## Data Flow

```
[Deal Aggregators] (Slickdeals, DealNews, TechBargains)
      |
      | Firecrawl API — returns full page as markdown
      v
[src/scraper.py]
      |
      | Regex parsing: extract titles, prices, URLs, discounts
      | Append Amazon affiliate tag to Amazon links
      v
[PostgreSQL DB] — deduplicate by source_url, rank by discount %
      |
      | Top unposted deals (discount >= 15%)
      v
[src/poster.py]
      |
      | Format tweet (title, price, discount, affiliate link)
      | Post with 2-min spacing between tweets
      v
[Twitter/X API v2]
```

## Module Breakdown

| Module | Responsibility |
|---|---|
| `config/settings.py` | Env config, deal sources, brand settings, safeguard limits |
| `src/scraper.py` | Firecrawl scraping, markdown parsing, affiliate URL building |
| `src/database.py` | PostgreSQL CRUD, duplicate prevention, deal ranking |
| `src/poster.py` | Tweet formatting, posting with rate limiting |
| `src/main.py` | Pipeline orchestration, APScheduler |
| `sql/schema.sql` | Table and index definitions |

## Scraping Strategy

1. **Firecrawl API** fetches deal pages and returns clean markdown
2. **Regex parser** extracts deal data from markdown:
   - Finds markdown links `[title](url)` with prices in surrounding text
   - Extracts deal price (lowest) and original price (highest)
   - Calculates discount percentage
   - Filters out navigation/non-deal links
3. **Affiliate tagging** appends `?tag=<affiliate_tag>` to Amazon URLs
4. **Deduplication** by `source_url` prevents storing the same deal twice

## Posting Safeguards

| Safeguard | Setting | Purpose |
|---|---|---|
| Max posts per run | 5 | Avoid Twitter spam flags |
| Min discount | 15% | Only post quality deals |
| Post delay | 120 seconds | Space out tweets |
| Affiliate disclosure | Twitter bio | Amazon Associates TOS compliance |

## Revenue Phases

### Phase 1: Amazon Associates (Current)
- Scrape aggregator sites for deals with Amazon links
- Append affiliate tag for commission tracking
- Target: 3 sales to unlock PA-API access

### Phase 2: Multi-Network Affiliates
- Add Rakuten, CJ Affiliate, ShareASale, Impact
- Scrape brand sites directly
- Higher commission rates (3-20%)

### Phase 3: Website + SEO
- Deal aggregator website with categories
- SEO traffic + AdSense display ads
- Email newsletter

## Renaming the Project

Change two env vars — zero code changes:

```
BRAND_NAME=NewName
BRAND_HASHTAG=#NewName
```

No hardcoded project names exist in the codebase.

## Cloud Migration

No code changes needed — only config:

| Component | Local | Cloud |
|---|---|---|
| App | `python -m src.main` | Google Cloud Run |
| Database | PostgreSQL localhost | Google Cloud SQL |
| Scheduler | APScheduler | Google Cloud Scheduler |
| Config | `.env` file | Google Secret Manager |

Monthly cost: ~$20-25.
