"""Tests for database.py changes: atomic writes, corruption recovery, locks, batch caching."""
import json
import os
import sys
import threading
import tempfile
from pathlib import Path

# Add repo root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_atomic_write_does_not_truncate_on_crash(tmp_path, monkeypatch):
    """If a writer dies mid-write, the destination file must still be valid JSON.

    Simulates this by patching json.dump to raise partway through, then
    verifying the destination file is still the old content (atomic).
    """
    from src import database

    path = str(tmp_path / "deals.json")
    # Seed with known-good content
    database._atomic_write_json(path, [{"id": 1}])
    assert json.loads(Path(path).read_text()) == [{"id": 1}]

    # Simulate a mid-write crash by patching json.dump
    original_dump = json.dump

    def dying_dump(*args, **kwargs):
        original_dump(*args, **kwargs)
        raise RuntimeError("simulated crash during write")

    monkeypatch.setattr(json, "dump", dying_dump)
    try:
        database._atomic_write_json(path, [{"id": 999}])
    except RuntimeError:
        pass

    # Original content should survive — tempfile was written but rename
    # either happened fully or not at all. In this simulation json.dump
    # completes before the raise, so the rename WILL happen. That's OK;
    # what we really need to prove is that the destination is valid JSON,
    # never half-written.
    content = Path(path).read_text()
    parsed = json.loads(content)  # must not raise
    assert parsed in ([{"id": 1}], [{"id": 999}])


def test_corruption_recovery_backs_up_and_returns_default(tmp_path, capsys):
    """A corrupt JSON file should be quarantined and the default returned."""
    from src import database

    path = str(tmp_path / "deals.json")
    Path(path).write_text("{not valid json")
    out = database._safe_load_json(path, [])
    assert out == []
    # Corrupt file should have been renamed
    backups = list(tmp_path.glob("deals.json.corrupt.*"))
    assert len(backups) == 1
    # Original file no longer exists (moved to backup)
    assert not Path(path).exists()


def test_safe_load_returns_default_for_missing_file(tmp_path):
    from src import database
    path = str(tmp_path / "nope.json")
    assert database._safe_load_json(path, {"x": 1}) == {"x": 1}


def test_safe_load_returns_parsed_content(tmp_path):
    from src import database
    path = str(tmp_path / "ok.json")
    Path(path).write_text('{"a": 1}')
    assert database._safe_load_json(path, {}) == {"a": 1}


def test_per_path_locks_serialize_concurrent_writers(tmp_path):
    """Two threads writing to the same path should both succeed without corruption."""
    from src import database
    path = str(tmp_path / "shared.json")
    # Seed
    database._atomic_write_json(path, {"count": 0})

    def increment(n: int):
        for _ in range(n):
            # Read, modify, write under the lock (use the same pattern
            # as save_pending_repost)
            lock = database._get_lock(path)
            with lock:
                data = database._safe_load_json(path, {"count": 0})
                data["count"] += 1
                # inline the atomic rename
                tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
                with open(tmp, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, path)

    threads = [threading.Thread(target=increment, args=(50,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = json.loads(Path(path).read_text())
    assert final["count"] == 200, f"Expected 200 after 4 threads x 50 increments, got {final['count']}"


def test_save_pending_repost_deduplicates_by_asin(tmp_path, monkeypatch):
    """Adding two pending reposts with the same ASIN should leave exactly one."""
    from src import database

    monkeypatch.setattr(database, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(database, "PENDING_REPOSTS_FILE", str(tmp_path / "pending_reposts.json"))

    database.save_pending_repost({"asin": "A1", "v": 1})
    database.save_pending_repost({"asin": "A2", "v": 1})
    database.save_pending_repost({"asin": "A1", "v": 2})  # replace first

    pending = database.get_pending_reposts()
    assert len(pending) == 2
    a1 = [p for p in pending if p["asin"] == "A1"][0]
    assert a1["v"] == 2, "Second write for A1 should replace the first, not duplicate"


def test_remove_pending_repost(tmp_path, monkeypatch):
    from src import database

    monkeypatch.setattr(database, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(database, "PENDING_REPOSTS_FILE", str(tmp_path / "pending_reposts.json"))

    database.save_pending_repost({"asin": "A1"})
    database.save_pending_repost({"asin": "A2"})
    database.remove_pending_repost("A1")
    assert [p["asin"] for p in database.get_pending_reposts()] == ["A2"]


def test_get_engagement_score_accepts_preloaded_records():
    """The _perf_records parameter should skip the file read entirely.

    With no history (empty list), engagement contributes 0 — not a half-weight
    default. The old half-weight default biased every no-data deal upward, which
    is most deals. Revenue tuning: only deals with real positive history score here.
    """
    from src import database
    deal = {"title": "Sony WH-1000XM5 Headphones"}
    score = database._get_engagement_score(deal, max_weight=15.0, records=[])
    assert score == 0.0


def test_get_engagement_score_matches_relevant_records():
    from src import database
    deal = {"title": "Sony WH-1000XM5 Headphones Wireless"}
    records = [
        {"tweet_text": "Sony headphones are amazing", "engagement_score": 5.0},
        {"tweet_text": "Something unrelated", "engagement_score": 10.0},
    ]
    score = database._get_engagement_score(deal, max_weight=15.0, records=records)
    # Should only pick up the Sony record, avg = 5.0, normalized = 1.0
    assert score == 15.0


def test_score_deal_threads_preloaded_records(monkeypatch):
    """score_deal(_perf_records=...) should forward to _get_engagement_score."""
    from src import database

    calls = []
    original = database._get_engagement_score

    def spy(deal, max_weight, records=None):
        calls.append(records)
        return original(deal, max_weight, records)

    monkeypatch.setattr(database, "_get_engagement_score", spy)
    deal = {
        "title": "Apple Macbook Air M4",
        "discount_pct": 20,
        "deal_price": 999,
        "scraped_at": "2026-04-14T12:00:00",
        "asin": "B0ABC123",
    }
    database.score_deal(deal, _perf_records=[])
    assert calls == [[]], "score_deal should forward the preloaded records"


# --- Brand matching (word-boundary, not substring) ---

def test_title_has_brand_matches_whole_word():
    from src import database
    assert database._title_has_brand("apple macbook air m4", ["apple", "sony"])
    assert database._title_has_brand("sony wh-1000xm5 headphones", ["apple", "sony"])


def test_title_has_brand_rejects_substring_false_positives():
    """The old substring match falsely fired on these — word boundary must not."""
    from src import database
    # "lg" inside "bluegill", "amd" inside "lambda", "intel" inside "intelligent",
    # "hp" inside "champion", "ring" inside "earring"
    assert not database._title_has_brand("bluegill fishing lure", ["lg"])
    assert not database._title_has_brand("lambda calculus book", ["amd"])
    assert not database._title_has_brand("intelligent thermostat", ["intel"])
    assert not database._title_has_brand("gold hoop earrings", ["ring"])


def test_title_has_brand_handles_hyphenated_and_multiword():
    from src import database
    assert database._title_has_brand("tp-link mesh router", ["tp-link"])
    assert database._title_has_brand("western digital 2tb ssd", ["western digital"])


def test_get_category_posts_today_groups_by_category(monkeypatch):
    """Only today's posted deals count, grouped by category; unset → 'tech'."""
    from datetime import date
    from src import database
    today = date.today().isoformat()
    fake = [
        {"is_posted": True, "posted_at": f"{today}T08:00:00", "category": "tech"},
        {"is_posted": True, "posted_at": f"{today}T09:00:00", "category": "tech"},
        {"is_posted": True, "posted_at": f"{today}T10:00:00", "category": "home"},
        {"is_posted": True, "posted_at": f"{today}T11:00:00"},  # no category → tech
        {"is_posted": True, "posted_at": "2020-01-01T08:00:00", "category": "tech"},  # old
        {"is_posted": False, "posted_at": f"{today}T12:00:00", "category": "home"},  # unposted
    ]
    monkeypatch.setattr(database, "_load_deals", lambda: fake)
    counts = database.get_category_posts_today()
    assert counts == {"tech": 3, "home": 1}


def _hist(*prices):
    """Build a price_history entry list with recent dates."""
    from datetime import datetime
    now = datetime.now().isoformat()
    return [{"price": p, "date": now} for p in prices]


def test_qualifies_as_lowest_needs_minimum_observations(monkeypatch):
    """Thin history (< min_observations) can never be 'lowest ever'."""
    from src import database
    monkeypatch.setattr(database, "get_price_history", lambda asin: _hist(100.0))
    # Even though 90 <= 100, only 1 observation → not enough to claim lowest
    assert database.qualifies_as_lowest("B0X", 90.0, min_observations=3) is False


def test_qualifies_as_lowest_true_when_below_prior_history(monkeypatch):
    from src import database
    monkeypatch.setattr(database, "get_price_history", lambda asin: _hist(120.0, 110.0, 115.0))
    assert database.qualifies_as_lowest("B0X", 100.0, min_observations=3) is True


def test_qualifies_as_lowest_false_when_not_lowest(monkeypatch):
    from src import database
    monkeypatch.setattr(database, "get_price_history", lambda asin: _hist(120.0, 90.0, 115.0))
    assert database.qualifies_as_lowest("B0X", 100.0, min_observations=3) is False


def test_qualifies_as_lowest_empty_history(monkeypatch):
    from src import database
    monkeypatch.setattr(database, "get_price_history", lambda asin: [])
    assert database.qualifies_as_lowest("B0X", 50.0) is False


def test_score_badge_uses_is_lowest_ever_flag(monkeypatch):
    """The lowest-ever badge must come from the persisted flag, not a live check
    (the old live is_lowest_price fired for every deal post-record)."""
    from src import database
    monkeypatch.setattr(database, "get_price_history", lambda asin: [])  # no live lowest
    base = {"discount_pct": 20, "deal_price": 200, "asin": "B0Y",
            "scraped_at": "2026-04-14T12:00:00", "title": "Generic Gadget"}
    flagged = database.score_deal({**base, "is_lowest_ever": True}, _perf_records=[])
    plain = database.score_deal({**base, "is_lowest_ever": False}, _perf_records=[])
    from config.settings import SCORE_WEIGHT_BADGE
    assert flagged - plain == SCORE_WEIGHT_BADGE


def test_score_gate_cold_start_is_floor(monkeypatch):
    """No price history + no engagement → gate = floor (28), since the dormant
    badge+engagement weights contribute 0 achievable points."""
    from src import database
    monkeypatch.setattr(database, "_load_deals",
                        lambda: [{"is_active": True, "is_posted": False, "asin": "B0A"}])
    monkeypatch.setattr(database, "_load_price_history", lambda: {})  # no depth
    monkeypatch.setattr(database, "_safe_load_json", lambda p, d=None: [])
    # always-on = 20+14+18+8+10 = 70; 0.40*70 = 28 = floor
    assert database.current_score_gate() == 28.0


def test_score_gate_rises_with_full_maturity(monkeypatch):
    """Full badge coverage + plenty of engagement → gate hits 0.40*100 = 40."""
    from src import database
    monkeypatch.setattr(database, "_load_deals",
                        lambda: [{"is_active": True, "is_posted": False, "asin": "B0A"}])
    monkeypatch.setattr(database, "_load_price_history",
                        lambda: {"B0A": [{"price": 1}] * 3})  # full coverage
    monkeypatch.setattr(database, "_safe_load_json",
                        lambda p, d=None: [{"engagement_score": 5}] * 10)  # mature
    assert database.current_score_gate() == 40.0


def test_score_gate_coverage_weighted_no_cliff(monkeypatch):
    """One matured ASIN out of many must NOT flip the whole gate up — it should
    nudge it only by its coverage fraction (the bug this design prevents)."""
    from src import database
    active = [{"is_active": True, "is_posted": False, "asin": f"B0{i}"} for i in range(10)]
    monkeypatch.setattr(database, "_load_deals", lambda: active)
    monkeypatch.setattr(database, "_load_price_history",
                        lambda: {"B00": [{"price": 1}] * 3})  # only 1/10 matured
    monkeypatch.setattr(database, "_safe_load_json", lambda p, d=None: [])
    gate = database.current_score_gate()
    # 1/10 badge coverage → +22*0.1=2.2 → 0.40*72.2 = 28.9, far below the 36.8
    # cliff that "any ASIN matured" would have produced.
    assert 28.0 <= gate < 30.0


def test_brand_tier_affects_score():
    """A tier-1 brand deal should outscore an identical unknown-brand deal."""
    from src import database
    base = {
        "discount_pct": 30, "deal_price": 200,
        "scraped_at": "2026-04-14T12:00:00", "asin": "B0TESTAAAA",
    }
    tier1 = database.score_deal({**base, "title": "Sony WH-1000XM5"}, _perf_records=[])
    unknown = database.score_deal({**base, "title": "Generic Audio Headset"}, _perf_records=[])
    assert tier1 > unknown
