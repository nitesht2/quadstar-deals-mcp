"""Tests for Discord bot button hardening and error reporting."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_interaction(response_done=False):
    """Build a mock discord.Interaction."""
    interaction = MagicMock()
    interaction.response.is_done = MagicMock(return_value=response_done)
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.message = MagicMock()
    interaction.message.delete = AsyncMock()
    interaction.user = "tester"
    return interaction


def test_report_callback_error_uses_response_when_not_done():
    """If the interaction hasn't been responded to, send_message should fire."""
    from src.discord_bot import _report_callback_error
    inter = _make_interaction(response_done=False)
    asyncio.run(_report_callback_error(inter, "Approve", RuntimeError("boom")))
    inter.response.send_message.assert_awaited_once()
    inter.followup.send.assert_not_awaited()


def test_report_callback_error_uses_followup_when_already_deferred():
    """If interaction.response.is_done() is True, followup.send should be used."""
    from src.discord_bot import _report_callback_error
    inter = _make_interaction(response_done=True)
    asyncio.run(_report_callback_error(inter, "Post Now", RuntimeError("oh no")))
    inter.followup.send.assert_awaited_once()
    inter.response.send_message.assert_not_awaited()


def test_report_callback_error_swallows_discord_errors():
    """The error helper itself must never raise (best-effort)."""
    from src.discord_bot import _report_callback_error
    inter = _make_interaction(response_done=False)
    inter.response.send_message = AsyncMock(side_effect=RuntimeError("discord is down"))
    # Must not raise
    asyncio.run(_report_callback_error(inter, "Reject", RuntimeError("original")))


def test_approve_button_defers_first():
    """DealApproveButton.callback should call defer() before doing work."""
    from src.discord_bot import DealApproveButton
    inter = _make_interaction(response_done=False)
    btn = DealApproveButton(deal_id=42, label_hint="peak", platform="twitter")
    # Patch _schedule_deal to return "ok" so the callback takes the happy path
    with patch("src.discord_bot._schedule_deal", return_value="ok"):
        with patch("src.discord_bot.get_smart_time", return_value=("2026-04-14T20:00:00.000Z", "Today 1 PM PDT"), create=True):
            with patch("src.discord_bot._truncate_title", return_value="Deal #42"):
                with patch("src.discord_bot._send_action_notification", new=AsyncMock()):
                    asyncio.run(btn.callback(inter))
    inter.response.defer.assert_awaited_once()


def test_reject_button_tolerates_already_deleted_message():
    """Rejecting a card whose message a user already deleted must NOT crash."""
    import discord
    from src.discord_bot import DealRejectButton
    inter = _make_interaction(response_done=False)
    # Simulate the message being already gone
    inter.message.delete = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "already deleted"))
    btn = DealRejectButton(deal_id=42, platform="twitter")
    with patch("src.discord_bot.update_deal", create=True):
        with patch("src.discord_bot._truncate_title", return_value="Deal #42"):
            with patch("src.discord_bot._send_action_notification", new=AsyncMock()):
                # Must not raise
                asyncio.run(btn.callback(inter))


def test_callback_exception_is_reported_not_raised():
    """If _schedule_deal throws, the user should see a friendly message."""
    from src.discord_bot import DealApproveButton
    inter = _make_interaction(response_done=False)
    btn = DealApproveButton(deal_id=42, label_hint="peak", platform="twitter")
    # _schedule_deal raises unexpectedly
    with patch("src.discord_bot._schedule_deal", side_effect=RuntimeError("bad state")):
        with patch("src.discord_bot.get_smart_time", return_value=("x", "y"), create=True):
            asyncio.run(btn.callback(inter))
    # We should have deferred AND reported the error via followup (because deferred=done)
    inter.response.defer.assert_awaited_once()
    # Either followup or response send_message must have fired once
    total_responses = inter.followup.send.await_count + inter.response.send_message.await_count
    assert total_responses >= 1, "User must see an error message"
