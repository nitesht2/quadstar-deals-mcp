"""Unit tests for src.amazon_scraper.run_amazon_scraper save loop.

Guards the per-deal isolation: one malformed deal must never abort the whole
batch (previously a None review_count crashed the save print → killed the run).
All external deps (scrape, save, price history) are mocked — no network.
"""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _deal(asin, **over):
    d = {
        "asin": asin, "title": f"Sony Headphones {asin}", "deal_price": 199.0,
        "star_rating": 4.5, "review_count": None, "category": "tech",
    }
    d.update(over)
    return d


def test_none_review_count_does_not_crash_save_loop():
    """star_rating set + review_count None must format cleanly (was a TypeError)."""
    from src import amazon_scraper
    deals = [_deal("B0AAAA1111")]  # review_count None, rating set
    with patch.object(amazon_scraper, "scrape_amazon_deals", return_value=deals), \
         patch.object(amazon_scraper, "save_deal", return_value=True), \
         patch("src.database.record_price"), \
         patch.object(amazon_scraper, "fetch_product_rating", return_value=(None, None)):
        saved = amazon_scraper.run_amazon_scraper("tech")
    assert saved == 1


def test_one_bad_deal_does_not_abort_the_batch():
    """If save_deal raises on one deal, the rest must still be saved."""
    from src import amazon_scraper
    deals = [_deal("B0AAAA1111"), _deal("B0BBBB2222"), _deal("B0CCCC3333")]

    def flaky_save(deal):
        if deal["asin"] == "B0BBBB2222":
            raise ValueError("boom on the middle deal")
        return True

    with patch.object(amazon_scraper, "scrape_amazon_deals", return_value=deals), \
         patch.object(amazon_scraper, "save_deal", side_effect=flaky_save), \
         patch("src.database.record_price"), \
         patch.object(amazon_scraper, "fetch_product_rating", return_value=(None, None)):
        saved = amazon_scraper.run_amazon_scraper("tech")
    # First and third saved; the middle one is skipped, not fatal.
    assert saved == 2
