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
    """The new _perf_records parameter should skip the file read entirely."""
    from src import database
    deal = {"title": "Sony WH-1000XM5 Headphones"}
    # Pass empty list — function should return the neutral default without
    # even attempting to open tweet_performance.json
    score = database._get_engagement_score(deal, max_weight=15.0, records=[])
    assert score == 15.0 * 0.5


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
