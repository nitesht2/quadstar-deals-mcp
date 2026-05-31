"""Unit tests for src.agent._post_to_telegram

Covers the agent's on-demand Telegram side-channel tool. All external
dependencies (telegram_client, database, notifier) are mocked — these
tests never touch the network and don't need Telegram credentials.
"""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_post_to_telegram_returns_error_when_not_configured():
    """No TELEGRAM_BOT_TOKEN / CHANNEL_ID → friendly error, no DB lookup."""
    from src import agent

    with patch("src.telegram_client.is_configured", return_value=False), \
         patch("src.database.get_deal_by_id") as mock_get_deal, \
         patch("src.telegram_client.send_deal") as mock_send:
        result = agent._post_to_telegram(42)

    assert "not configured" in result.lower()
    mock_get_deal.assert_not_called()
    mock_send.assert_not_called()


def test_post_to_telegram_reports_missing_deal():
    """Deal id not in DB → 'Deal X not found', no telegram call."""
    from src import agent

    with patch("src.telegram_client.is_configured", return_value=True), \
         patch("src.database.get_deal_by_id", return_value=None), \
         patch("src.telegram_client.send_deal") as mock_send:
        result = agent._post_to_telegram(999)

    assert "999" in result
    assert "not found" in result.lower()
    mock_send.assert_not_called()


def test_post_to_telegram_happy_path_reports_success():
    """Valid deal + successful send → success message including deal title."""
    from src import agent

    deal = {"id": 42, "title": "Sony WH-1000XM5 Wireless Headphones", "image_url": "https://example.com/x.jpg"}
    content = {"tweet_1": "hook", "tweet_2": "link"}

    with patch("src.telegram_client.is_configured", return_value=True), \
         patch("src.database.get_deal_by_id", return_value=deal), \
         patch("src.notifier.generate_deal_content", return_value=content), \
         patch("src.telegram_client.send_deal", return_value=True) as mock_send:
        result = agent._post_to_telegram(42)

    assert "Posted deal 42" in result
    assert "Sony WH-1000XM5" in result
    # send_deal must be called with the resolved deal and generated content
    mock_send.assert_called_once_with(deal, content)


def test_post_to_telegram_reports_failure_when_send_fails():
    """send_deal returns False → failure message, does not raise."""
    from src import agent

    deal = {"id": 7, "title": "Test deal", "image_url": ""}
    content = {"tweet_1": "hook", "tweet_2": "link"}

    with patch("src.telegram_client.is_configured", return_value=True), \
         patch("src.database.get_deal_by_id", return_value=deal), \
         patch("src.notifier.generate_deal_content", return_value=content), \
         patch("src.telegram_client.send_deal", return_value=False):
        result = agent._post_to_telegram(7)

    assert "Failed" in result
    assert "7" in result


def test_telegram_tool_reachable_through_dispatch(monkeypatch):
    """A 'telegram' command must route through tool_router.dispatch to
    _post_to_telegram with the parsed deal_id (no agent framework involved)."""
    from src import agent as agent_mod
    import src.tool_router as tr

    captured = {}

    def fake_telegram(deal_id):
        captured["deal_id"] = deal_id
        return "ok"

    monkeypatch.setattr(agent_mod, "_post_to_telegram", fake_telegram)
    # Force the classifier down the keyword path (no LLM/network).
    monkeypatch.setattr(tr, "_classify", tr._keyword_classify)

    result = tr.dispatch("post deal 42 to telegram")
    assert captured["deal_id"] == 42
    assert result == "ok"
