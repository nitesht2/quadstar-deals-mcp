"""Unit tests for src/telegram_client.py

Follows the platform isolation rule: tests are self-contained, no network
calls, no dependency on other platform clients.
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _force_active_hour():
    """Force _is_active_hour() to return True so tests don't depend on wall-clock UTC hour.

    Without this, tests silently queue instead of posting when run outside
    7am-10pm UTC. The send_deal/sendPhoto tests should be testing the network
    layer, not the active-hours gate (which has its own dedicated tests).
    """
    with patch("src.telegram_client._is_active_hour", return_value=True):
        yield


def _make_ok_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"ok": True}
    return mock


def _make_err_response(code=400, description="Bad Request"):
    mock = MagicMock()
    mock.status_code = code
    mock.json.return_value = {"ok": False, "description": description}
    return mock


# --- Happy path ---

def test_send_deal_calls_sendphoto_when_image_present():
    """Happy path: image_url present → sendPhoto endpoint called."""
    from src import telegram_client
    deal = {"title": "Sony WH-1000XM5", "image_url": "https://example.com/img.jpg"}
    content = {"tweet_1": "Great headphones deal", "tweet_2": "https://amzn.to/x Follow @quadstardeals"}

    with patch("config.settings.TELEGRAM_BOT_TOKEN", "testtoken"), \
         patch("config.settings.TELEGRAM_CHANNEL_ID", "@testchan"), \
         patch("requests.post", return_value=_make_ok_response()) as mock_post:
        result = telegram_client.send_deal(deal, content)

    assert result is True
    assert mock_post.call_count == 1
    url = mock_post.call_args[0][0]
    assert "sendPhoto" in url
    assert "testtoken" in url


def test_send_deal_caption_combines_tweet1_and_tweet2():
    """Caption sent to Telegram must be tweet_1 + newlines + tweet_2."""
    from src import telegram_client
    deal = {"title": "Test", "image_url": "https://example.com/img.jpg"}
    content = {"tweet_1": "Hook text", "tweet_2": "Link text"}

    with patch("config.settings.TELEGRAM_BOT_TOKEN", "t"), \
         patch("config.settings.TELEGRAM_CHANNEL_ID", "@c"), \
         patch("requests.post", return_value=_make_ok_response()) as mock_post:
        telegram_client.send_deal(deal, content)

    payload = mock_post.call_args[1]["json"]
    assert payload["caption"] == "Hook text\n\nLink text"


# --- Image fallback ---

def test_send_deal_uses_sendmessage_when_no_image():
    """Empty image_url → uses sendMessage, not sendPhoto."""
    from src import telegram_client
    deal = {"title": "Test", "image_url": ""}
    content = {"tweet_1": "hook", "tweet_2": "link"}

    with patch("config.settings.TELEGRAM_BOT_TOKEN", "t"), \
         patch("config.settings.TELEGRAM_CHANNEL_ID", "@c"), \
         patch("requests.post", return_value=_make_ok_response()) as mock_post:
        result = telegram_client.send_deal(deal, content)

    assert result is True
    url = mock_post.call_args[0][0]
    assert "sendMessage" in url
    assert "sendPhoto" not in url


def test_send_deal_uses_sendmessage_for_data_uri():
    """data: URI image → uses sendMessage (Telegram can't fetch data URIs)."""
    from src import telegram_client
    deal = {"title": "Test", "image_url": "data:image/jpeg;base64,/9j/abc123"}
    content = {"tweet_1": "hook", "tweet_2": "link"}

    with patch("config.settings.TELEGRAM_BOT_TOKEN", "t"), \
         patch("config.settings.TELEGRAM_CHANNEL_ID", "@c"), \
         patch("requests.post", return_value=_make_ok_response()) as mock_post:
        result = telegram_client.send_deal(deal, content)

    assert result is True
    url = mock_post.call_args[0][0]
    assert "sendMessage" in url


def test_send_deal_falls_back_to_sendmessage_when_sendphoto_fails():
    """sendPhoto returns 400 (bad URL) → retries as sendMessage → True."""
    from src import telegram_client
    deal = {"title": "Test", "image_url": "https://broken.example.com/img.jpg"}
    content = {"tweet_1": "hook", "tweet_2": "link"}

    responses = [_make_err_response(400, "failed to get HTTP URL content"), _make_ok_response()]

    with patch("config.settings.TELEGRAM_BOT_TOKEN", "t"), \
         patch("config.settings.TELEGRAM_CHANNEL_ID", "@c"), \
         patch("requests.post", side_effect=responses) as mock_post:
        result = telegram_client.send_deal(deal, content)

    assert result is True
    assert mock_post.call_count == 2
    assert "sendPhoto" in mock_post.call_args_list[0][0][0]
    assert "sendMessage" in mock_post.call_args_list[1][0][0]


# --- Error handling ---

def test_send_deal_returns_false_on_api_error():
    """Both sendPhoto and sendMessage fail → returns False, never raises."""
    from src import telegram_client
    deal = {"title": "Test", "image_url": "https://example.com/img.jpg"}
    content = {"tweet_1": "hook", "tweet_2": "link"}

    with patch("config.settings.TELEGRAM_BOT_TOKEN", "t"), \
         patch("config.settings.TELEGRAM_CHANNEL_ID", "@c"), \
         patch("requests.post", return_value=_make_err_response(400)):
        result = telegram_client.send_deal(deal, content)

    assert result is False


def test_send_deal_returns_false_when_not_configured():
    """Both env vars unset → False immediately, no network call."""
    from src import telegram_client

    with patch("config.settings.TELEGRAM_BOT_TOKEN", ""), \
         patch("config.settings.TELEGRAM_CHANNEL_ID", ""), \
         patch("requests.post") as mock_post:
        result = telegram_client.send_deal({}, {})

    assert result is False
    mock_post.assert_not_called()


def test_send_deal_returns_false_on_network_error():
    """requests.post raises ConnectionError → returns False, never raises."""
    from src import telegram_client

    with patch("config.settings.TELEGRAM_BOT_TOKEN", "t"), \
         patch("config.settings.TELEGRAM_CHANNEL_ID", "@c"), \
         patch("requests.post", side_effect=ConnectionError("network down")):
        result = telegram_client.send_deal({"image_url": ""}, {"tweet_1": "", "tweet_2": ""})

    assert result is False


# --- is_configured helper ---

def test_is_configured_true_when_both_vars_set():
    from src import telegram_client
    with patch("config.settings.TELEGRAM_BOT_TOKEN", "abc"), \
         patch("config.settings.TELEGRAM_CHANNEL_ID", "@chan"):
        assert telegram_client.is_configured() is True


def test_is_configured_false_when_token_missing():
    from src import telegram_client
    with patch("config.settings.TELEGRAM_BOT_TOKEN", ""), \
         patch("config.settings.TELEGRAM_CHANNEL_ID", "@chan"):
        assert telegram_client.is_configured() is False
