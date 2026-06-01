"""Unit tests for src.agent_supervisor.run_cycle — the reliability harness.

The contract that makes a flaky agent safe to be load-bearing:
- agent posts something        -> no fallback
- agent posts 0, deals eligible-> deterministic fallback fires (never empty)
- agent posts 0, none eligible -> correctly silent (no double-run)
Subprocess + HTTP are mocked — no agent, no network.
"""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import agent_supervisor as sup  # noqa: E402


def test_no_fallback_when_agent_posts():
    with patch.object(sup, "_service", return_value="scraped 3 amazon"), \
         patch.object(sup, "_posts_today", side_effect=[1, 3]), \
         patch.object(sup, "_run_agent", return_value=True), \
         patch.object(sup, "_count_eligible", return_value=5):
        out = sup.run_cycle()
    assert out["status"] == "agent_posted"
    assert out["posted_by_agent"] == 2
    assert out["fallback_used"] is False


def test_fallback_when_agent_no_show_with_eligible_deals():
    """Agent posted 0 but eligible deals exist → deterministic fallback runs."""
    calls = []
    def fake_service(tool, **kw):
        calls.append(tool)
        return "Pipeline: 2 scheduled" if tool == "run_pipeline" else "scraped 4"
    with patch.object(sup, "_service", side_effect=fake_service), \
         patch.object(sup, "_posts_today", side_effect=[0, 0]), \
         patch.object(sup, "_run_agent", return_value=True), \
         patch.object(sup, "_count_eligible", return_value=4):
        out = sup.run_cycle()
    assert out["status"] == "fallback"
    assert out["fallback_used"] is True
    assert "run_pipeline" in calls
    assert out["fallback_result"] == "Pipeline: 2 scheduled"


def test_correctly_silent_when_nothing_eligible():
    """Agent posted 0 and nothing was eligible → silence is correct, no fallback."""
    calls = []
    def fake_service(tool, **kw):
        calls.append(tool)
        return "scraped 0"
    with patch.object(sup, "_service", side_effect=fake_service), \
         patch.object(sup, "_posts_today", side_effect=[2, 2]), \
         patch.object(sup, "_run_agent", return_value=False), \
         patch.object(sup, "_count_eligible", return_value=0):
        out = sup.run_cycle()
    assert out["status"] == "correctly_silent"
    assert out["fallback_used"] is False
    assert "run_pipeline" not in calls  # no double-run when legitimately quiet


def test_agent_crash_still_falls_back():
    """Agent subprocess fails (exit!=0) but eligible deals exist → fallback covers it."""
    with patch.object(sup, "_service", return_value="ok"), \
         patch.object(sup, "_posts_today", side_effect=[0, 0]), \
         patch.object(sup, "_run_agent", return_value=False), \
         patch.object(sup, "_count_eligible", return_value=3):
        out = sup.run_cycle()
    assert out["status"] == "fallback" and out["fallback_used"] is True
