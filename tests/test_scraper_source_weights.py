"""Unit tests for source-weight-based scraping in src/scraper.py"""
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Stub out firecrawl before any scraper import
_firecrawl_stub = types.ModuleType("firecrawl")
_firecrawl_stub.FirecrawlApp = MagicMock
sys.modules.setdefault("firecrawl", _firecrawl_stub)


def _make_deal(title: str) -> dict:
    return {
        "title": title, "source_url": f"https://example.com/{title}",
        "deal_price": 100.0, "discount_pct": 30, "asin": None,
        "affiliate_url": "", "image_url": "https://example.com/img.jpg",
        "source": "Slickdeals",
    }


def test_high_weight_source_gets_more_deals():
    """Source with weight 2.0 → max 20 deals; weight 0.5 → max 5 deals."""
    from src.scraper import run_scraper

    source_deals = {
        "Slickdeals":      [_make_deal(f"slick-{i}") for i in range(25)],
        "DealNews":        [_make_deal(f"deal-{i}") for i in range(25)],
        "Camelcamelcamel": [],
    }
    saved = []

    def fake_scrape(client, source):
        return source_deals[source["name"]]

    with patch("src.scraper.get_firecrawl_client", return_value=MagicMock()), \
         patch("src.scraper.scrape_source", side_effect=fake_scrape), \
         patch("src.scraper.save_deal", side_effect=lambda d: saved.append(d["title"]) or True), \
         patch("src.source_tracker.get_source_weights",
               return_value={"Slickdeals": 2.0, "DealNews": 0.5, "Camelcamelcamel": 1.0}):
        run_scraper()

    slick_saved = sum(1 for t in saved if t.startswith("slick-"))
    deal_saved = sum(1 for t in saved if t.startswith("deal-"))
    assert slick_saved == 20, f"Expected 20, got {slick_saved}"
    assert deal_saved == 5, f"Expected 5, got {deal_saved}"


def test_neutral_weight_caps_at_base_max():
    """Source with weight 1.0 (no data yet) should process up to 10 deals."""
    from src.scraper import run_scraper

    deals = [_make_deal(f"item-{i}") for i in range(20)]
    saved = []

    with patch("src.scraper.get_firecrawl_client", return_value=MagicMock()), \
         patch("src.scraper.scrape_source", return_value=deals), \
         patch("src.scraper.save_deal", side_effect=lambda d: saved.append(d) or True), \
         patch("src.source_tracker.get_source_weights", return_value={}):
        run_scraper()

    assert len(saved) == 30  # 3 sources × 10 each (neutral weight)


def test_floor_of_3_prevents_source_starvation():
    """Even a very low weight source should get at least 3 deals."""
    from src.scraper import run_scraper

    deals = [_make_deal(f"item-{i}") for i in range(10)]
    saved = []

    def fake_scrape(client, source):
        return deals if source["name"] == "Slickdeals" else []

    with patch("src.scraper.get_firecrawl_client", return_value=MagicMock()), \
         patch("src.scraper.scrape_source", side_effect=fake_scrape), \
         patch("src.scraper.save_deal", side_effect=lambda d: saved.append(d) or True), \
         patch("src.source_tracker.get_source_weights",
               return_value={"Slickdeals": 0.1}):  # weight so low floor kicks in
        run_scraper()

    assert len(saved) == 3  # floor of 3 applied


def test_sources_sorted_by_weight_highest_first():
    """Higher-weight sources should be scraped before lower-weight ones."""
    from src.scraper import run_scraper

    order = []

    def fake_scrape(client, source):
        order.append(source["name"])
        return []

    with patch("src.scraper.get_firecrawl_client", return_value=MagicMock()), \
         patch("src.scraper.scrape_source", side_effect=fake_scrape), \
         patch("src.source_tracker.get_source_weights",
               return_value={"Slickdeals": 0.5, "DealNews": 2.0, "Camelcamelcamel": 1.0}):
        run_scraper()

    assert order[0] == "DealNews"
    assert order[-1] == "Slickdeals"


def test_weight_fetch_failure_falls_back_to_neutral():
    """If get_source_weights() raises, scraper runs normally with no crash."""
    from src.scraper import run_scraper

    with patch("src.scraper.get_firecrawl_client", return_value=MagicMock()), \
         patch("src.scraper.scrape_source", return_value=[]), \
         patch("src.source_tracker.get_source_weights", side_effect=Exception("db error")):
        result = run_scraper()  # must not raise

    assert result == 0
