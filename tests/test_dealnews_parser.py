"""Unit tests for src.scraper._parse_dealnews_card — the parser that turns
DealNews card innerText into a structured deal. The Playwright render path is
covered by live e2e; this locks down the parsing logic in isolation.
"""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import scraper  # noqa: E402


def _card(text, redirect="https://www.dealnews.com/lw/click.html?20,2,1",
          img="https://d.dlnws.com/x.jpg"):
    return {"text": text, "redirect": redirect, "img": img}


def test_parses_amazon_deal_with_discount():
    """Standard DealNews card → discount computed, store=Amazon kept."""
    text = ("Amazon · 19 hrs ago\nSony WH-1000XM5 Wireless Headphones\n"
            "$199 $399\nfree shipping w/ Prime")
    with patch.object(scraper, "_is_tech_deal", return_value=True):
        out = scraper._parse_dealnews_card(_card(text))
    assert out is not None
    assert out["title"].startswith("Sony WH-1000XM5")
    assert out["deal_price"] == 199.0 and out["original_price"] == 399.0
    assert out["discount_pct"] == 50.13  # rounded


def test_drops_non_amazon_store():
    """AMAZON_ONLY filter: a Best Buy / Walmart card is rejected."""
    text = "Best Buy · 23 hrs ago\nSome Camera Deal\n$300 $500\nfree shipping"
    with patch.object(scraper, "AMAZON_ONLY", True):
        out = scraper._parse_dealnews_card(_card(text))
    assert out is None


def test_strips_staff_pick_badges():
    """Leading STAFF PICK / NEW / POPULAR badges don't shift the parse."""
    text = ("STAFF PICK\nNew\nAmazon · 55 mins ago\nApple AirPods Pro 2\n"
            "$179 $249\nfree shipping")
    with patch.object(scraper, "_is_tech_deal", return_value=True):
        out = scraper._parse_dealnews_card(_card(text))
    assert out is not None and out["title"].startswith("Apple AirPods Pro 2")
    assert out["deal_price"] == 179.0


def test_handles_no_original_price():
    """If only one price is shown, discount is 0 — and a 0-discount item with
    no original price is KEPT (downstream gates judge it)."""
    text = "Amazon · 1 hr ago\nLogitech G502 Gaming Mouse\n$59\nfree shipping"
    with patch.object(scraper, "_is_tech_deal", return_value=True):
        out = scraper._parse_dealnews_card(_card(text))
    assert out is not None
    assert out["deal_price"] == 59.0
    assert out["original_price"] is None
    assert out["discount_pct"] == 0.0


def test_rejects_below_min_price():
    """Below MIN_DEAL_PRICE → reject (avoids cheap, low-commission items)."""
    text = "Amazon · 2 hrs ago\nUSB-C Cable Pack\n$5 $12\nfree shipping"
    with patch.object(scraper, "_is_tech_deal", return_value=True), \
         patch.object(scraper, "MIN_DEAL_PRICE", 50.0):
        out = scraper._parse_dealnews_card(_card(text))
    assert out is None


def test_rejects_fake_inflated_discount():
    """Discounts above MAX_DISCOUNT_PCT (often fake list prices) are dropped."""
    text = "Amazon · 1 hr ago\nGeneric Power Bank\n$9 $199\nfree shipping"
    with patch.object(scraper, "_is_tech_deal", return_value=True), \
         patch.object(scraper, "MAX_DISCOUNT_PCT", 85.0):
        out = scraper._parse_dealnews_card(_card(text))
    assert out is None  # 95.5% > 85 cap → rejected


def test_rejects_non_tech_title():
    """Non-tech titles drop even from an Amazon card (category filter)."""
    text = "Amazon · 3 hrs ago\nStainless Steel Cookware Set\n$120 $200\nshipping"
    with patch.object(scraper, "_is_tech_deal", return_value=False):
        out = scraper._parse_dealnews_card(_card(text))
    assert out is None


def test_empty_or_short_text_returns_none():
    assert scraper._parse_dealnews_card(_card("")) is None
    assert scraper._parse_dealnews_card(_card("only one line")) is None
