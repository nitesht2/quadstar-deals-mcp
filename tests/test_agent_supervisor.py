"""Unit tests for src.agent_supervisor.run_cycle — human-approval model.

Nothing auto-posts: the agent PROPOSES approval cards. Verification is "did the
agent run?" The fallback also PROPOSES (generate_and_send_cards), only on a true
agent failure with eligible inventory.
  agent ran (output)              -> agent_ran (proposals sent; no fallback)
  agent FAILED, deals eligible    -> fallback_proposed (generate_and_send_cards)
  agent FAILED, none eligible     -> correctly_silent
Subprocess + HTTP are mocked.
"""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import agent_supervisor as sup  # noqa: E402


def test_agent_ran_respected_no_fallback():
    calls = []
    with patch.object(sup, "_service", side_effect=lambda t, **k: calls.append(t) or "x"), \
         patch.object(sup, "_run_agent", return_value=(True, True)), \
         patch.object(sup, "_count_eligible", return_value=3):
        out = sup.run_cycle()
    assert out["status"] == "agent_ran"
    assert out["fallback_used"] is False
    assert "generate_and_send_cards" not in calls
    assert "run_pipeline" not in calls  # nothing auto-posts


def test_fallback_proposes_cards_on_true_agent_failure():
    """Agent produced no output + eligible deals → fallback sends approval cards."""
    calls = []
    def fake_service(tool, **kw):
        calls.append(tool)
        return "Sent 2 Discord approval card(s)." if tool == "generate_and_send_cards" else "scraped 4"
    with patch.object(sup, "_service", side_effect=fake_service), \
         patch.object(sup, "_run_agent", return_value=(True, False)), \
         patch.object(sup, "_count_eligible", return_value=4):
        out = sup.run_cycle()
    assert out["status"] == "fallback_proposed"
    assert out["fallback_used"] is True
    assert "generate_and_send_cards" in calls
    assert "run_pipeline" not in calls  # fallback proposes, never auto-posts


def test_agent_failure_with_no_eligible_is_silent():
    calls = []
    with patch.object(sup, "_service", side_effect=lambda t, **k: calls.append(t) or "x"), \
         patch.object(sup, "_run_agent", return_value=(False, False)), \
         patch.object(sup, "_count_eligible", return_value=0):
        out = sup.run_cycle()
    assert out["status"] == "correctly_silent"
    assert out["fallback_used"] is False
    assert "generate_and_send_cards" not in calls
