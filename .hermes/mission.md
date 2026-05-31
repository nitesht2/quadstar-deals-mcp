# Quadstar-Deals Mission Brief

Your job: Post 4 Amazon tech/home deals to @quadstardeals on X today.

## 1. SCRAPING
Go to Amazon "Today's Deals" page. Extract deals with >20% discount.
If the main page fails, try the tech section, then the home section.
If scraping fails entirely, check cache at data/aap/quadstar/last_good_scrape.json
and use those deals if they're less than 24 hours old.

## 2. RANKING
For each deal, compute a value score: discount% x rating x review_count.
If a deal's price seems suspicious (90%+ off, new seller), open the Amazon
product page to verify the live price. Skip if the data doesn't match.
Discard deals under 4.0 stars or under 20% discount.
Select the top 4. If fewer than 4 good deals exist, post what you have.
2 quality posts > 4 junk posts.

## 3. DRAFTING
Load brand voice from ~/.voice/ (all 3 files). Apply ALL rules.
No banned words. Anti-slop checklist before finalizing each tweet.
Randomize format: ~25% single tweet with embedded link,
~75% thread with 2 posts (hook tweet + link tweet).
Each tweet must be under 280 characters. Verify character count.

## 4. SCHEDULING
Schedule via Postiz at data/aap/quadstar/schedule.json.
Randomize times within 8AM-8PM PDT. Minimum 2 hours between deals.
Confirm each schedule on the Postiz dashboard.
If Postiz is down, save drafts and retry next tick.

## 5. COST & LOGGING
Track every token used. If cumulative tokens exceed 50K, stop immediately
and save checkpoint. Log all decisions with reasoning.

## 6. ALERTING
Send ONE Discord alert to #quadstar-deal when done.
If zero deals get scheduled, mark as CRITICAL.
Otherwise, summary format: "Mission complete: X/4 deals. Cost: $Y.YYYY"
