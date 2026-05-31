"""Unit tests for src/source_tracker.py"""
import sys
import json
import os
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _tracker(tmp_path):
    """Return source_tracker module with DATA_DIR patched to tmp_path."""
    import importlib
    from src import source_tracker
    importlib.reload(source_tracker)  # Reset module state
    return source_tracker


# --- record_scraped ---

def test_record_scraped_creates_entry(tmp_path):
    from src import source_tracker
    perf_file = tmp_path / "source_performance.json"
    with patch.object(source_tracker, "_PERF_FILE", str(perf_file)), \
         patch.object(source_tracker, "DATA_DIR", str(tmp_path)):
        source_tracker.record_scraped("Slickdeals", 10)
        data = json.loads(perf_file.read_text())
    assert data["Slickdeals"]["scraped"] == 10


def test_record_scraped_accumulates(tmp_path):
    from src import source_tracker
    perf_file = tmp_path / "source_performance.json"
    with patch.object(source_tracker, "_PERF_FILE", str(perf_file)), \
         patch.object(source_tracker, "DATA_DIR", str(tmp_path)):
        source_tracker.record_scraped("Slickdeals", 5)
        source_tracker.record_scraped("Slickdeals", 3)
        data = json.loads(perf_file.read_text())
    assert data["Slickdeals"]["scraped"] == 8


# --- record_posted ---

def test_record_posted_increments(tmp_path):
    from src import source_tracker
    perf_file = tmp_path / "source_performance.json"
    with patch.object(source_tracker, "_PERF_FILE", str(perf_file)), \
         patch.object(source_tracker, "DATA_DIR", str(tmp_path)):
        source_tracker.record_scraped("DealNews", 20)
        source_tracker.record_posted("DealNews")
        source_tracker.record_posted("DealNews")
        data = json.loads(perf_file.read_text())
    assert data["DealNews"]["posted"] == 2


# --- record_engagement ---

def test_record_engagement_accumulates(tmp_path):
    from src import source_tracker
    perf_file = tmp_path / "source_performance.json"
    with patch.object(source_tracker, "_PERF_FILE", str(perf_file)), \
         patch.object(source_tracker, "DATA_DIR", str(tmp_path)):
        source_tracker.record_engagement("Slickdeals", 50)
        source_tracker.record_engagement("Slickdeals", 30)
        data = json.loads(perf_file.read_text())
    assert data["Slickdeals"]["engagement"] == 80


def test_record_engagement_ignores_zero_or_negative(tmp_path):
    from src import source_tracker
    perf_file = tmp_path / "source_performance.json"
    with patch.object(source_tracker, "_PERF_FILE", str(perf_file)), \
         patch.object(source_tracker, "DATA_DIR", str(tmp_path)):
        source_tracker.record_engagement("Slickdeals", 0)
        source_tracker.record_engagement("Slickdeals", -5)
        # File should not have been created (nothing to write)
        if perf_file.exists():
            data = json.loads(perf_file.read_text())
            assert data.get("Slickdeals", {}).get("engagement", 0) == 0


# --- get_source_weights ---

def test_get_source_weights_returns_neutral_for_new_sources(tmp_path):
    """Sources with < 5 scraped deals get weight 1.0."""
    from src import source_tracker
    perf_file = tmp_path / "source_performance.json"
    with patch.object(source_tracker, "_PERF_FILE", str(perf_file)), \
         patch.object(source_tracker, "DATA_DIR", str(tmp_path)):
        source_tracker.record_scraped("Slickdeals", 3)
        weights = source_tracker.get_source_weights()
    assert weights.get("Slickdeals") == 1.0


def test_get_source_weights_higher_post_rate_gets_higher_weight(tmp_path):
    """Source with higher post_rate should get a higher weight."""
    from src import source_tracker
    perf_file = tmp_path / "source_performance.json"
    with patch.object(source_tracker, "_PERF_FILE", str(perf_file)), \
         patch.object(source_tracker, "DATA_DIR", str(tmp_path)):
        # Slickdeals: 10/20 = 50% post rate
        source_tracker.record_scraped("Slickdeals", 20)
        for _ in range(10):
            source_tracker.record_posted("Slickdeals")
        # DealNews: 2/20 = 10% post rate
        source_tracker.record_scraped("DealNews", 20)
        for _ in range(2):
            source_tracker.record_posted("DealNews")

        weights = source_tracker.get_source_weights()

    assert weights["Slickdeals"] > weights["DealNews"]


def test_get_source_weights_returns_empty_when_no_data(tmp_path):
    from src import source_tracker
    perf_file = tmp_path / "source_performance.json"
    with patch.object(source_tracker, "_PERF_FILE", str(perf_file)), \
         patch.object(source_tracker, "DATA_DIR", str(tmp_path)):
        weights = source_tracker.get_source_weights()
    assert weights == {}


# --- get_report ---

def test_get_report_returns_string(tmp_path):
    from src import source_tracker
    perf_file = tmp_path / "source_performance.json"
    with patch.object(source_tracker, "_PERF_FILE", str(perf_file)), \
         patch.object(source_tracker, "DATA_DIR", str(tmp_path)):
        source_tracker.record_scraped("Slickdeals", 5)
        source_tracker.record_posted("Slickdeals")
        report = source_tracker.get_report()
    assert "Slickdeals" in report
    assert "scraped" in report


def test_get_report_handles_no_data(tmp_path):
    from src import source_tracker
    perf_file = tmp_path / "source_performance.json"
    with patch.object(source_tracker, "_PERF_FILE", str(perf_file)), \
         patch.object(source_tracker, "DATA_DIR", str(tmp_path)):
        report = source_tracker.get_report()
    assert "No source performance data" in report
