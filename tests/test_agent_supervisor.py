"""Unit tests for src.agent_supervisor.run_cycle — the reliability harness.

Hermes-driven contract: the agent's DECISION is respected when it genuinely
ran. The deterministic fallback fires ONLY on true agent failure (empty output
/ crash / timeout) with postable inventory — not when the agent ran and chose
to post nothing.
  agent posts something           -> agent_posted (no fallback)
  agent ran clean, posted 0        -> agent_declined (decision respected, no fallback)
  agent FAILED, deals eligible     -> fallback (never silently empty)
  agent FAILED, none eligible      -> correctly_silent
Subprocess + HTTP are mocked — no agent, no network.
"""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import agent_supervisor as sup  # noqa: E402


def test_agent_posted_no_fallback():
    with patch.object(sup, "_service", return_value="scraped 3"), \
         patch.object(sup, "_posts_today", side_effect=[1, 3]), \
         patch.object(sup, "_run_agent", return_value=(True, True)), \
         patch.object(sup, "_count_eligible", return_value=5):
        out = sup.run_cycle()
    assert out["status"] == "agent_posted"
    assert out["posted_by_agent"] == 2
    assert out["fallback_used"] is False


def test_agent_ran_and_declined_is_respected():
    """Agent ran clean (output) but posted 0 → its decision stands, NO fallback."""
    calls = []
    with patch.object(sup, "_service", side_effect=lambda t, **k: calls.append(t) or "x"), \
         patch.object(sup, "_posts_today", side_effect=[2, 2]), \
         patch.object(sup, "_run_agent", return_value=(True, True)), \
         patch.object(sup, "_count_eligible", return_value=3):
        out = sup.run_cycle()
    assert out["status"] == "agent_declined"
    assert out["fallback_used"] is False
    assert "run_pipeline" not in calls  # the agent's "post nothing" is trusted


def test_fallback_only_on_true_agent_failure():
    """Agent produced no output (no-show) + eligible deals exist → fallback."""
    calls = []
    def fake_service(tool, **kw):
        calls.append(tool)
        return "Pipeline: 2 scheduled" if tool == "run_pipeline" else "scraped 4"
    with patch.object(sup, "_service", side_effect=fake_service), \
         patch.object(sup, "_posts_today", side_effect=[0, 0]), \
         patch.object(sup, "_run_agent", return_value=(True, False)), \
         patch.object(sup, "_count_eligible", return_value=4):
        out = sup.run_cycle()
    assert out["status"] == "fallback"
    assert out["fallback_used"] is True
    assert "run_pipeline" in calls


def test_agent_crash_with_no_eligible_is_silent():
    """Agent crashed but nothing was postable → no fallback, nothing lost."""
    calls = []
    with patch.object(sup, "_service", side_effect=lambda t, **k: calls.append(t) or "x"), \
         patch.object(sup, "_posts_today", side_effect=[0, 0]), \
         patch.object(sup, "_run_agent", return_value=(False, False)), \
         patch.object(sup, "_count_eligible", return_value=0):
        out = sup.run_cycle()
    assert out["status"] == "correctly_silent"
    assert out["fallback_used"] is False
    assert "run_pipeline" not in calls
