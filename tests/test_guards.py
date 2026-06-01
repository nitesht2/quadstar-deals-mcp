"""Unit tests for src.guards — the deterministic post-time cage.

These lock the invariants that must hold no matter who (deterministic pipeline
or the agent) decides to post. All network (price verify) and scoring are mocked.
"""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import guards  # noqa: E402


def _deal(**over):
    d = {
        "asin": "B0TESTAAAA", "title": "Sony WH-1000XM5", "deal_price": 199.0,
        "discount_pct": 30.0, "category": "audio", "is_posted": False,
        "affiliate_url": "https://www.amazon.com/dp/B0TESTAAAA?tag=quadstar0e-20",
    }
    d.update(over)
    return d


def _ctx(**over):
    return guards._ctx_from_settings(**over)


# --- affiliate tag (revenue invariant) ---

def test_affiliate_tag_present_true():
    with patch("config.settings.AMAZON_AFFILIATE_TAG", "quadstar0e-20"):
        assert guards.affiliate_tag_present(_deal()) is True


def test_affiliate_tag_missing_false():
    with patch("config.settings.AMAZON_AFFILIATE_TAG", "quadstar0e-20"):
        assert guards.affiliate_tag_present(_deal(affiliate_url="https://amazon.com/dp/X")) is False


# --- hard guards (the cage) ---

def test_guard_blocks_already_posted():
    res = guards.enforce_guards(_deal(is_posted=True), None, _ctx(), verify_price=False)
    assert not res.ok and res.code == "already_posted"


def test_guard_blocks_missing_affiliate_tag():
    with patch("config.settings.AMAZON_AFFILIATE_TAG", "quadstar0e-20"):
        res = guards.enforce_guards(_deal(affiliate_url="https://amazon.com/dp/X"),
                                    None, _ctx(), verify_price=False)
    assert not res.ok and res.code == "no_affiliate_tag"


def test_guard_blocks_daily_cap():
    res = guards.enforce_guards(_deal(), None,
                                _ctx(posts_today=4, max_daily=4), verify_price=False)
    assert not res.ok and res.code == "daily_cap"


def test_guard_blocks_category_cap():
    res = guards.enforce_guards(_deal(category="audio"), None,
                                _ctx(cat_counts={"audio": 2}, max_per_category=2),
                                verify_price=False)
    assert not res.ok and res.code == "category_cap"


def test_guard_blocks_low_confidence():
    ctx = _ctx(min_confidence=0.85)
    res = guards.enforce_guards(_deal(), {"tweet_1": "hi", "confidence": 0.5}, ctx,
                                verify_price=False)
    assert not res.ok and res.code == "low_confidence"


def test_guard_blocks_failed_price_verify():
    with patch("src.price_verifier.verify_deal_price", return_value=(False, "price mismatch")):
        res = guards.enforce_guards(_deal(), {"tweet_1": "hi", "confidence": 1.0},
                                    _ctx(max_daily=10, max_per_category=10))
    assert not res.ok and res.code == "price_unverified"


def test_guard_passes_clean_deal():
    with patch("src.price_verifier.verify_deal_price", return_value=(True, "verified $199")):
        res = guards.enforce_guards(_deal(), {"tweet_1": "hi", "confidence": 1.0},
                                    _ctx(max_daily=10, max_per_category=10, min_confidence=0.85))
    assert res.ok and res.code == "ok"


# --- eligibility (soft signals the agent may override) ---

def test_eligibility_below_min_discount():
    res = guards.eligibility(_deal(discount_pct=10), _ctx(min_discount=15), _perf_records=[])
    assert not res.ok and res.code == "below_min_discount"


def test_eligibility_below_min_score():
    with patch("src.database.score_deal", return_value=20.0):
        res = guards.eligibility(_deal(discount_pct=40), _ctx(min_discount=15, min_score=35),
                                 _perf_records=[])
    assert not res.ok and res.code == "below_min_score"


def test_eligibility_asin_cooldown():
    with patch("src.database.score_deal", return_value=80.0):
        res = guards.eligibility(_deal(), _ctx(min_discount=15, min_score=35,
                                               recent_asins={"B0TESTAAAA"}), _perf_records=[])
    assert not res.ok and res.code == "asin_cooldown"


def test_eligibility_ok():
    with patch("src.database.score_deal", return_value=80.0):
        res = guards.eligibility(_deal(discount_pct=40), _ctx(min_discount=15, min_score=35),
                                 _perf_records=[])
    assert res.ok and res.code == "ok"
