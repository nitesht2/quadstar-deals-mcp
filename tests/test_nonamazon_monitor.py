"""Unit tests for non-Amazon price monitoring in src/price_monitor.py"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --- _url_price_key ---

def test_url_price_key_is_stable():
    """Same URL always produces the same key."""
    from src.price_monitor import _url_price_key
    key1 = _url_price_key("https://www.bestbuy.com/site/sony-wh1000xm5/6505727.p")
    key2 = _url_price_key("https://www.bestbuy.com/site/sony-wh1000xm5/6505727.p")
    assert key1 == key2


def test_url_price_key_starts_with_url_prefix():
    from src.price_monitor import _url_price_key
    key = _url_price_key("https://www.bestbuy.com/product/123")
    assert key.startswith("url:")


def test_url_price_key_different_urls_produce_different_keys():
    from src.price_monitor import _url_price_key
    key1 = _url_price_key("https://www.bestbuy.com/product/123")
    key2 = _url_price_key("https://www.walmart.com/product/456")
    assert key1 != key2


# --- _fetch_price_from_url ---

def test_fetch_price_from_url_returns_none_when_openclaw_not_configured():
    from src.price_monitor import _fetch_price_from_url
    with patch("src.openclaw_client.is_configured", return_value=False):
        result = _fetch_price_from_url("https://www.bestbuy.com/product/123")
    assert result is None


def test_fetch_price_from_url_extracts_price_from_openclaw_response():
    from src.price_monitor import _fetch_price_from_url
    with patch("src.openclaw_client.is_configured", return_value=True), \
         patch("src.openclaw_client.browse", return_value="The current price is $249.99 on sale"):
        result = _fetch_price_from_url("https://www.bestbuy.com/product/123", "Sony Headphones")
    assert result == 249.99


def test_fetch_price_from_url_returns_none_when_openclaw_returns_empty():
    from src.price_monitor import _fetch_price_from_url
    with patch("src.openclaw_client.is_configured", return_value=True), \
         patch("src.openclaw_client.browse", return_value=""):
        result = _fetch_price_from_url("https://www.bestbuy.com/product/123")
    assert result is None


def test_fetch_price_from_url_returns_none_on_exception():
    from src.price_monitor import _fetch_price_from_url
    with patch("src.openclaw_client.is_configured", side_effect=Exception("boom")):
        result = _fetch_price_from_url("https://www.bestbuy.com/product/123")
    assert result is None


def test_fetch_price_from_url_rejects_unreasonable_prices():
    """Prices outside 1–50000 range are noise — return None."""
    from src.price_monitor import _fetch_price_from_url
    with patch("src.openclaw_client.is_configured", return_value=True), \
         patch("src.openclaw_client.browse", return_value="Price: $0.00"):
        result = _fetch_price_from_url("https://www.bestbuy.com/product/123")
    assert result is None


# --- get_watchlist includes non-ASIN deals ---

def test_get_watchlist_includes_posted_deal_without_asin():
    """Non-Amazon posted deals (no ASIN) should appear in the watchlist with url_key."""
    from src.price_monitor import get_watchlist
    from datetime import datetime, timedelta

    nonamazon_deal = {
        "id": 42,
        "title": "Sony WH-1000XM5 at BestBuy",
        "asin": None,
        "source_url": "https://www.bestbuy.com/site/sony/6505727.p",
        "affiliate_url": "https://www.bestbuy.com/site/sony/6505727.p",
        "deal_price": 249.99,
        "is_posted": True,
        "posted_at": datetime.now().isoformat(),
    }

    with patch("src.price_monitor._load_deals", return_value=[nonamazon_deal]), \
         patch("src.price_monitor._load_manual_watchlist", return_value=[]), \
         patch("src.price_monitor.get_price_history", return_value=[]):
        watchlist = get_watchlist()

    assert len(watchlist) == 1
    item = watchlist[0]
    assert item["asin"] is None
    assert item["url_key"].startswith("url:")
    assert item["title"] == "Sony WH-1000XM5 at BestBuy"


def test_get_watchlist_skips_unposted_nonamazon_deals():
    from src.price_monitor import get_watchlist
    from datetime import datetime

    deal = {
        "id": 43,
        "title": "Some BestBuy deal",
        "asin": None,
        "source_url": "https://www.bestbuy.com/product/999",
        "deal_price": 100.0,
        "is_posted": False,
        "posted_at": datetime.now().isoformat(),
    }

    with patch("src.price_monitor._load_deals", return_value=[deal]), \
         patch("src.price_monitor._load_manual_watchlist", return_value=[]), \
         patch("src.price_monitor.get_price_history", return_value=[]):
        watchlist = get_watchlist()

    assert len(watchlist) == 0
