"""Unit tests for the agentic primitives in src.agent.

Verifies the core "agent decides, code enforces" contract:
- schedule_deal REFUSES when the guard cage fails (and does not post)
- schedule_deal posts when the cage passes, honoring agent-supplied copy
- get_candidate_deals returns a scored menu WITHOUT gating anything out
All external deps (DB, Postiz, notifier, guards) are mocked — no network.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import agent, guards  # noqa: E402


def _deal(**over):
    d = {"id": 42, "asin": "B0XYZ12345", "title": "Sony WH-1000XM5",
         "deal_price": 199.0, "category": "audio", "is_posted": False}
    d.update(over)
    return d


def _common_patches(stack, deal):
    """Patch the shared DB/context deps schedule_deal pulls in."""
    stack.enter_context(patch("src.database.get_deal_by_id", return_value=deal))
    stack.enter_context(patch("src.database.get_posts_today_count", return_value=0))
    stack.enter_context(patch("src.database.get_category_posts_today", return_value={}))
    stack.enter_context(patch("src.database.get_watchlist_asins", return_value=[]))
    stack.enter_context(patch.object(agent, "_get_bot_loop", return_value=None))


def test_schedule_deal_refuses_when_guard_fails():
    """Guard cage returns not-ok → no post, structured refusal with the code."""
    from contextlib import ExitStack
    with ExitStack() as s:
        _common_patches(s, _deal())
        s.enter_context(patch("src.notifier.generate_deal_content",
                              return_value={"tweet_1": "hi", "confidence": 1.0}))
        s.enter_context(patch("src.guards.enforce_guards",
                              return_value=guards.GuardResult(False, "price_unverified", "mismatch")))
        sched = s.enter_context(patch("src.postiz_client.schedule_post"))
        marked = s.enter_context(patch("src.database.mark_as_posted"))
        out = json.loads(agent._schedule_deal(42))
    assert out["ok"] is False and out["code"] == "price_unverified"
    sched.assert_not_called()
    marked.assert_not_called()


def test_schedule_deal_posts_when_guard_passes():
    from contextlib import ExitStack
    with ExitStack() as s:
        _common_patches(s, _deal())
        s.enter_context(patch("src.notifier.generate_deal_content",
                              return_value={"tweet_1": "hook", "tweet_2": "link", "confidence": 1.0}))
        s.enter_context(patch("src.guards.enforce_guards",
                              return_value=guards.GuardResult(True, "ok", "passed")))
        s.enter_context(patch("src.platform_router.select_platforms", return_value=["twitter"]))
        s.enter_context(patch("src.postiz_client.get_smart_time",
                              return_value=("2026-06-02T17:00:00.000Z", "5pm PT")))
        s.enter_context(patch("src.postiz_client.schedule_post", return_value={"status": "ok"}))
        s.enter_context(patch("src.postiz_client.extract_postiz_id", return_value="pz1"))
        marked = s.enter_context(patch("src.database.mark_as_posted"))
        s.enter_context(patch("src.tweet_learner.record_tweet"))
        out = json.loads(agent._schedule_deal(42, platforms="twitter"))
    assert out["ok"] is True and out["code"] == "scheduled"
    assert out["scheduled_at"] == "2026-06-02T17:00:00.000Z"
    marked.assert_called_once_with(42)


def test_schedule_deal_uses_agent_copy():
    """When the agent supplies copy_json, that voice is used verbatim (not the LLM)."""
    from contextlib import ExitStack
    captured = {}
    def _capture(deal, content, platforms, scheduled_at=None):
        captured["content"] = content
        return {"status": "ok"}
    with ExitStack() as s:
        _common_patches(s, _deal())
        gen = s.enter_context(patch("src.notifier.generate_deal_content"))
        s.enter_context(patch("src.guards.enforce_guards",
                              return_value=guards.GuardResult(True, "ok", "passed")))
        s.enter_context(patch("src.platform_router.select_platforms", return_value=["twitter"]))
        s.enter_context(patch("src.postiz_client.get_smart_time", return_value=("t", "l")))
        s.enter_context(patch("src.postiz_client.schedule_post", side_effect=_capture))
        s.enter_context(patch("src.postiz_client.extract_postiz_id", return_value="pz1"))
        s.enter_context(patch("src.database.mark_as_posted"))
        s.enter_context(patch("src.tweet_learner.record_tweet"))
        agent._schedule_deal(42, copy_json=json.dumps({"tweet_1": "AGENT HOOK", "tweet_2": "x"}))
    assert captured["content"]["tweet_1"] == "AGENT HOOK"
    gen.assert_not_called()  # agent copy used, backend LLM skipped


def test_schedule_deal_not_found():
    with patch("src.database.get_deal_by_id", return_value=None):
        out = json.loads(agent._schedule_deal(999))
    assert out["ok"] is False and out["code"] == "not_found"


def test_get_candidate_deals_returns_menu_without_gating():
    """Menu includes a sub-min-discount deal (not dropped) flagged ineligible."""
    deals = [
        _deal(id=1, discount_pct=40, title="Big Discount"),
        _deal(id=2, discount_pct=5, title="Thin Discount"),
    ]
    with patch("src.database.get_top_unposted_deals", return_value=deals), \
         patch("src.database.score_deal", return_value=60.0), \
         patch("src.database.get_watchlist_asins", return_value=[]), \
         patch("src.database._safe_load_json", return_value=[]):
        out = json.loads(agent._get_candidate_deals(10))
    assert len(out) == 2  # nothing gated out
    thin = next(d for d in out if d["id"] == 2)
    assert thin["eligible"] is False  # flagged, but still present for the agent to judge
