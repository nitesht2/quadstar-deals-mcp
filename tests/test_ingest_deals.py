"""Unit tests for src/agent._ingest_deals — the Phase-3 Hermes ingest loop.

Verifies field normalization, deterministic discount math (agent arithmetic is
never trusted), affiliate-URL rebuild from ASIN, payload-shape tolerance, and
the saved/filtered/invalid accounting. save_deal is mocked so these tests cover
the ingest logic in isolation; save_deal's own dedup/gates are tested elsewhere.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _capture(monkeypatch_target="src.database.save_deal"):
    """Return (saved_list, fake_save_deal) — fake records each deal and returns True."""
    captured = []

    def _fake(deal):
        captured.append(deal)
        return True

    return captured, _fake


# --- Field normalization ---

def test_normalizes_hermes_field_names():
    captured, fake = _capture()
    payload = [{
        "title": "Sony WH-1000XM5 Wireless Headphones",
        "asin": "b0abcdefgh",  # lower-case on purpose
        "price": 298.0,
        "list_price": 399.99,
        "image_url": "https://m.media-amazon.com/images/I/abc._AC_SX300_.jpg",
        "rating": 4.7,
        "review_count": "1234",  # string on purpose
        "url": "https://www.amazon.com/dp/B0ABCDEFGH/ref=sr_1_1",
    }]
    with patch("src.database.save_deal", side_effect=fake):
        from src.agent import _ingest_deals
        out = _ingest_deals(payload)

    assert len(captured) == 1
    d = captured[0]
    assert d["deal_price"] == 298.0
    assert d["original_price"] == 399.99
    assert d["asin"] == "B0ABCDEFGH"          # upper-cased
    assert d["star_rating"] == 4.7
    assert d["review_count"] == 1234           # coerced to int
    assert d["retailer"] == "Amazon"
    assert d["source"] == "hermes"
    assert "1 saved" in out


def test_affiliate_url_rebuilt_from_asin_with_tag():
    captured, fake = _capture()
    with patch("src.database.save_deal", side_effect=fake):
        from src.agent import _ingest_deals
        _ingest_deals([{"title": "Anker Power Bank 20000mAh", "asin": "B0ABCDEFGH", "price": 59.99}])
    d = captured[0]
    assert "/dp/B0ABCDEFGH" in d["affiliate_url"]
    assert "tag=" in d["affiliate_url"]        # affiliate tag guaranteed (revenue)


# --- Discount is computed by code, never trusted from the agent ---

def test_discount_computed_from_prices_overrides_agent_value():
    captured, fake = _capture()
    payload = [{
        "title": "Logitech MX Master 3S Mouse",
        "asin": "B0ABCDEFGH",
        "price": 75.0,
        "list_price": 100.0,
        "discount": 90,  # agent claims 90% — must be ignored
    }]
    with patch("src.database.save_deal", side_effect=fake):
        from src.agent import _ingest_deals
        _ingest_deals(payload)
    assert captured[0]["discount_pct"] == 25.0  # (100-75)/100, not the agent's 90


def test_discount_falls_back_to_agent_value_when_no_list_price():
    captured, fake = _capture()
    payload = [{"title": "Samsung T7 SSD 1TB", "asin": "B0ABCDEFGH", "price": 80.0, "discount": 33}]
    with patch("src.database.save_deal", side_effect=fake):
        from src.agent import _ingest_deals
        _ingest_deals(payload)
    assert captured[0]["discount_pct"] == 33.0
    assert captured[0]["original_price"] is None


def test_bogus_list_price_below_deal_price_is_discarded():
    captured, fake = _capture()
    payload = [{"title": "Apple AirPods Pro", "asin": "B0ABCDEFGH", "price": 200.0, "list_price": 150.0}]
    with patch("src.database.save_deal", side_effect=fake):
        from src.agent import _ingest_deals
        _ingest_deals(payload)
    assert captured[0]["original_price"] is None
    assert captured[0]["discount_pct"] == 0.0


# --- Required fields ---

def test_missing_required_fields_counted_invalid_not_saved():
    captured, fake = _capture()
    payload = [
        {"asin": "B0ABCDEFGH", "price": 50},                 # no title
        {"title": "No ASIN headphones", "price": 50},         # no asin
        {"title": "No price gizmo", "asin": "B0ABCDEFGH"},    # no price
        {"title": "Zero price", "asin": "B0ABCDEFGH", "price": 0},  # price <= 0
    ]
    with patch("src.database.save_deal", side_effect=fake):
        from src.agent import _ingest_deals
        out = _ingest_deals(payload)
    assert captured == []          # save_deal never called
    assert "4 invalid" in out


# --- Payload shapes ---

def test_accepts_json_string():
    captured, fake = _capture()
    payload = json.dumps([{"title": "Dell Monitor 27 inch", "asin": "B0ABCDEFGH", "price": 199.0}])
    with patch("src.database.save_deal", side_effect=fake):
        from src.agent import _ingest_deals
        out = _ingest_deals(payload)
    assert len(captured) == 1
    assert "1 saved" in out


def test_accepts_single_dict():
    captured, fake = _capture()
    with patch("src.database.save_deal", side_effect=fake):
        from src.agent import _ingest_deals
        _ingest_deals({"title": "Single deal keyboard", "asin": "B0ABCDEFGH", "price": 89.0})
    assert len(captured) == 1


def test_bad_json_string_returns_error():
    from src.agent import _ingest_deals
    out = _ingest_deals("{not valid json")
    assert "Ingest failed" in out


def test_non_list_payload_returns_error():
    from src.agent import _ingest_deals
    out = _ingest_deals(12345)
    assert "Ingest failed" in out


# --- Accounting: saved vs filtered (dedup) vs invalid ---

def test_counts_saved_filtered_and_invalid():
    # save_deal returns True for the first, False (duplicate/filtered) for the rest.
    results = iter([True, False])

    def _fake(deal):
        return next(results)

    payload = [
        {"title": "Keeper deal speaker", "asin": "B0AAAAAAAA", "price": 120.0},
        {"title": "Duplicate deal speaker", "asin": "B0BBBBBBBB", "price": 120.0},
        {"not": "a dict"},  # invalid shape
    ]
    with patch("src.database.save_deal", side_effect=_fake):
        from src.agent import _ingest_deals
        out = _ingest_deals(payload)
    assert "3 deal(s): 1 saved, 1 duplicate/filtered, 1 invalid" in out
