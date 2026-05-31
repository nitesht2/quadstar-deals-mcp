# Smarter Pipeline Design - Approach A

Date: 2026-04-13
Status: Approved
Goal: Make the deals bot smarter with better scoring, notifications, content, and learning.

Amazon stays primary (affiliate revenue). Human-in-loop for now, building toward full autonomy.

## Section 1: Deal Scoring Model

Weighted multi-factor score (0-100) replaces discount-only ranking.

| Factor | Weight | Logic |
|--------|--------|-------|
| Discount % | 25 | Linear: 15%=0, 50%+=25 |
| Brand tier | 20 | Known brands (Apple, Sony, Bose, Samsung)=20, mid-tier=12, unknown=5 |
| Price sweet spot | 15 | $100-500 range scores highest. Below $50 or above $1000 scores lower |
| Historical engagement | 15 | Similar products (category/brand) with high past engagement get boosted |
| Lowest-ever badge | 10 | is_lowest_ever=10, is_lowest_90d=6, neither=0 |
| Source freshness | 10 | Scraped <1h ago=10, <4h=6, older=2 |
| Trending signal | 5 | Appears on multiple scrape runs = slight boost |

New function: `score_deal()` in database.py
Called by: `_get_unposted_deals()` for ranking
Fast-track threshold: Score >= 85 triggers immediate Discord card

## Section 2: Discord Notifications

Confirmation messages on all key actions using color-coded embeds.

- Green: success (approved, published)
- Yellow: action taken (rescheduled, auto-approved)
- Red: failure or rejection

| Action | Notification |
|--------|-------------|
| Approve (any method) | "Deal scheduled for [time] on [platform]. Post ID: [id]" |
| Pick Time | "Rescheduled to [new time] on [platform]" |
| Reject | "Deal rejected and marked inactive" (ephemeral) |
| Auto-approve (score >= 85) | "Auto-approved: [title] ([score], [discount]% off). Scheduled [time]" |
| Fast-track trigger | "Hot deal detected ([score]): [title]. Sending approval card now." |
| Post published | "Published to [platform]: [title]" |
| Post failed | "Failed to publish: [title]. Error: [reason]" |

All replies attach to the original deal card message for context.

## Section 3: Price Drop Content Revamp

### Thresholds

| Setting | Current | New |
|---------|---------|-----|
| Alert threshold | ~0% | 10% |
| Repost threshold | 15% | 20% |
| Minimum dollar drop | None | $15 |

Behavior:
- <10% drop: Ignore (no alert)
- 10-19% drop: Discord alert only (FYI)
- 20%+ AND $15+ savings: Full repost pipeline with approval card
- Lowest ever at any qualifying drop: Immediate card

### Content

LLM writes ALL content. Templates are last-resort fallback only.

Post 1 (hook + image):
- "PRICE DROP ALERT" style opener
- Product name, was/now price, dollar savings, % off
- Badge if lowest ever
- Product image attached
- Under 280 chars

Post 2 (reply with link):
- Short creative CTA written by LLM
- NO prices (Amazon prices are dynamic, could be stale by click time)
- Affiliate URL
- @quadstardeals follow

LinkedIn:
- LLM-written professional format
- Specs and context, no hype

Anti-AI writing filter runs on all LLM output.

### Price drop vs new deal pipelines

These are separate quality gates:
- New deals: Scoring model (0-100)
- Price drops: Drop % + dollar thresholds. Already vetted when first posted.

## Section 4: Fast-Track Hot Deal Trigger

- Normal cycle: every 2h at :30, all 20 URLs
- Fast-track check: every 30min between cycles
- Fast-track only hits 2-3 highest-yield pages (Gold Box, Electronics Movers)
- Score >= 85: immediate Discord card
- Full 2h cycle skips deals already fast-tracked

Resource impact: Minimal. 2-3 pages vs 20.

## Section 5: Feedback Loop Closure

Before generating content, LLM prompt auto-includes latest tweet_learner insights:
- Best performing hook style (caps, question, lowercase)
- Best CTA style (urgency vs casual vs fomo)
- Optimal tweet length range
- Best feature/emoji density

LLM uses these as guidance, not hard rules. System improves as more engagement data accumulates.

## Future Work (Approach B and C)

### Approach B: Multi-Source Intelligence
- Slickdeals/DealNews as discovery (find trending deals, look up ASIN on Amazon)
- Cross-source deal validation
- Trending product detection

### Approach C: Full Intelligence Engine
- Engagement prediction model (train on tweet_performance data)
- Optimal posting time learner
- Auto-graduating to autonomy (suggest auto-approve for patterns you always approve)
