"""Quick test: scrape deals from Slickdeals and DealNews using Firecrawl."""

from firecrawl import FirecrawlApp
from src.scraper import parse_deals_from_markdown, build_affiliate_url
import os
from dotenv import load_dotenv

load_dotenv()

app = FirecrawlApp(api_key=os.getenv("FIRECRAWL_API_KEY"))

sources = [
    {"name": "Slickdeals", "url": "https://slickdeals.net/deals/"},
    {"name": "DealNews", "url": "https://www.dealnews.com/c142/Electronics/"},
]

for source in sources:
    print("=" * 60)
    print(f"Scraping {source['name']}...")
    print("=" * 60)

    result = app.scrape(source["url"], formats=["markdown"])

    markdown = ""
    if isinstance(result, dict):
        markdown = result.get("markdown", "")
    elif hasattr(result, "markdown"):
        markdown = result.markdown or ""

    if not markdown:
        print("No markdown returned.\n")
        continue

    # Parse deals
    deals = parse_deals_from_markdown(markdown, source["name"])
    print(f"Found {len(deals)} deals:\n")

    for i, deal in enumerate(deals[:10], 1):
        amazon = " [AMAZON]" if "amazon.com" in deal["source_url"] else ""
        discount = f" ({deal['discount_pct']:.0f}% off)" if deal["discount_pct"] > 0 else ""
        original = f" was ${deal['original_price']:.0f}" if deal["original_price"] else ""
        img = f"  [IMG]" if deal.get("image_url") else ""
        print(f"  {i}. {deal['title'][:80]}")
        print(f"     ${deal['deal_price']:.2f}{original}{discount}{amazon}{img}")
        print(f"     {deal['affiliate_url'][:100]}")
        if deal.get("image_url"):
            print(f"     IMG: {deal['image_url'][:100]}")
        print()

    print()
