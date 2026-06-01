"""Unit tests for the agentic primitives in src.agent (human-approval model).

Contract: the agent PROPOSES (sends an approval card); nothing auto-posts.
- propose_deal REFUSES when the guard cage fails (no card sent)
- propose_deal sends an approval card when the cage passes (no schedule, no mark-posted)
- propose_deal honors agent-supplied copy
- get_candidate_deals returns a scored menu WITHOUT gating anything out
All external deps (DB, Postiz, notifier, guards, Discord) are mocked — no network.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import agent, guards  # noqa: E402


def _deal(**over):
    d = {"id": 42, "asin": "B0XYZ12345", "title": "Sony WH-1000XM5",
         "deal_price": 199.0, "category": "audio", "is_posted": False}
    d.update(over)
    return d


def _common_patches(stack, deal):
    stack.enter_context(patch("src.database.get_deal_by_id", return_value=deal))
    stack.enter_context(patch("src.database.get_posts_today_count", return_value=0))
    stack.enter_context(patch("src.database.get_category_posts_today", return_value={}))
    stack.enter_context(patch("src.database.get_watchlist_asins", return_value=[]))
    stack.enter_context(patch("src.database.update_deal"))


def test_propose_refuses_when_guard_fails():
    """Cage fails → no approval card, structured refusal with the code."""
    from contextlib import ExitStack
    with ExitStack() as s:
        _common_patches(s, _deal())
        s.enter_context(patch("src.notifier.generate_deal_content",
                              return_value={"tweet_1": "hi", "confidence": 1.0}))
        s.enter_context(patch("src.guards.enforce_guards",
                              return_value=guards.GuardResult(False, "price_unverified", "mismatch")))
        s.enter_context(patch.object(agent, "_get_bot_loop", return_value=MagicMock()))
        card = s.enter_context(patch("src.discord_bot.send_deal_card"))
        out = json.loads(agent._propose_deal(42))
    assert out["ok"] is False and out["code"] == "price_unverified"
    card.assert_not_called()


def test_propose_sends_card_when_guard_passes():
    """Cage passes → approval card sent; NOTHING scheduled or marked posted."""
    from contextlib import ExitStack
    with ExitStack() as s:
        _common_patches(s, _deal())
        s.enter_context(patch("src.notifier.generate_deal_content",
                              return_value={"tweet_1": "hook", "tweet_2": "link", "confidence": 1.0}))
        s.enter_context(patch("src.guards.enforce_guards",
                              return_value=guards.GuardResult(True, "ok", "passed")))
        s.enter_context(patch.object(agent, "_get_bot_loop", return_value=MagicMock()))
        s.enter_context(patch("asyncio.run_coroutine_threadsafe"))
        card = s.enter_context(patch("src.discord_bot.send_deal_card"))
        sched = s.enter_context(patch("src.postiz_client.schedule_post"))
        marked = s.enter_context(patch("src.database.mark_as_posted"))
        out = json.loads(agent._propose_deal(42))
    assert out["ok"] is True and out["code"] == "proposed"
    card.assert_called_once()
    sched.assert_not_called()   # propose never schedules
    marked.assert_not_called()  # propose never marks posted


def test_propose_persists_agent_copy():
    """Agent copy_json is stored on the deal so Approve uses what the human saw."""
    from contextlib import ExitStack
    with ExitStack() as s:
        s.enter_context(patch("src.database.get_deal_by_id", return_value=_deal()))
        s.enter_context(patch("src.database.get_posts_today_count", return_value=0))
        s.enter_context(patch("src.database.get_category_posts_today", return_value={}))
        s.enter_context(patch("src.database.get_watchlist_asins", return_value=[]))
        upd = s.enter_context(patch("src.database.update_deal"))
        s.enter_context(patch("src.notifier.generate_deal_content"))
        s.enter_context(patch("src.guards.enforce_guards",
                              return_value=guards.GuardResult(True, "ok", "passed")))
        s.enter_context(patch.object(agent, "_get_bot_loop", return_value=MagicMock()))
        s.enter_context(patch("asyncio.run_coroutine_threadsafe"))
        s.enter_context(patch("src.discord_bot.send_deal_card"))
        agent._propose_deal(42, copy_json=json.dumps({"tweet_1": "AGENT HOOK", "tweet_2": "x"}))
    # update_deal called with the agent copy stored as hermes_tweet_1
    patches = [c.args[1] for c in upd.call_args_list if len(c.args) > 1]
    assert any(p.get("hermes_tweet_1") == "AGENT HOOK" and p.get("copy_source") == "hermes"
               for p in patches)


def test_propose_not_found():
    with patch("src.database.get_deal_by_id", return_value=None):
        out = json.loads(agent._propose_deal(999))
    assert out["ok"] is False and out["code"] == "not_found"


def test_get_candidate_deals_returns_menu_without_gating():
    deals = [
        _deal(id=1, discount_pct=40, title="Big Discount"),
        _deal(id=2, discount_pct=5, title="Thin Discount"),
    ]
    with patch("src.database.get_top_unposted_deals", return_value=deals), \
         patch("src.database.score_deal", return_value=60.0), \
         patch("src.database.get_watchlist_asins", return_value=[]), \
         patch("src.database._safe_load_json", return_value=[]):
        out = json.loads(agent._get_candidate_deals(10))
    assert len(out) == 2
    thin = next(d for d in out if d["id"] == 2)
    assert thin["eligible"] is False
