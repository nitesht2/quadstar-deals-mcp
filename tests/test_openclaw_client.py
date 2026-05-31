"""Unit tests for src/openclaw_client.py

Covers:
- is_configured() gate (no env vars = False)
- notify() sends to webhook with bearer auth
- notify() no-ops when not configured
- notify_* convenience wrappers format correctly
- browse() returns content from response
- browse() returns "" when OpenClaw is down
- All functions are exception-safe (never raise)
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ok(json_body=None):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = json_body or {"ok": True}
    return m


def _err(code=500):
    m = MagicMock()
    m.status_code = code
    m.json.return_value = {"ok": False}
    return m


def test_is_configured_false_without_url():
    from src import openclaw_client
    with patch("config.settings.OPENCLAW_WEBHOOK_URL", ""):
        assert openclaw_client.is_configured() is False


def test_is_configured_true_with_url():
    from src import openclaw_client
    with patch("config.settings.OPENCLAW_WEBHOOK_URL", "http://localhost:4000/hook"):
        assert openclaw_client.is_configured() is True


def test_notify_noop_when_not_configured():
    """No webhook URL set → notify returns False, does NOT call requests.post."""
    from src import openclaw_client
    with patch("config.settings.OPENCLAW_WEBHOOK_URL", ""), \
         patch("requests.post") as mock_post:
        result = openclaw_client.notify("hello")
    assert result is False
    assert mock_post.call_count == 0


def test_notify_posts_payload_and_bearer_auth():
    """Configured → POSTs JSON with Bearer token."""
    from src import openclaw_client
    with patch("config.settings.OPENCLAW_WEBHOOK_URL", "http://localhost:4000/hook"), \
         patch("config.settings.OPENCLAW_SECRET", "sekret"), \
         patch("config.settings.BRAND_NAME", "QuadStar Deals"), \
         patch("requests.post", return_value=_ok()) as mock_post:
        ok = openclaw_client.notify("deal posted", title="Done")
    assert ok is True
    assert mock_post.call_count == 1
    kwargs = mock_post.call_args.kwargs
    assert kwargs["json"]["message"] == "deal posted"
    assert kwargs["json"]["title"] == "Done"
    assert kwargs["headers"]["Authorization"] == "Bearer sekret"


def test_notify_returns_false_on_error_status():
    from src import openclaw_client
    with patch("config.settings.OPENCLAW_WEBHOOK_URL", "http://localhost:4000/hook"), \
         patch("config.settings.OPENCLAW_SECRET", ""), \
         patch("requests.post", return_value=_err(500)):
        assert openclaw_client.notify("x") is False


def test_notify_price_drop_formats_message():
    """notify_price_drop should call notify() with both prices and drop_pct."""
    from src import openclaw_client
    with patch.object(openclaw_client, "notify") as mock_notify:
        openclaw_client.notify_price_drop("Sony XM5", 399.99, 299.99, 25.0)
    assert mock_notify.call_count == 1
    msg = mock_notify.call_args.args[0]
    assert "Sony XM5" in msg
    assert "399.99" in msg
    assert "299.99" in msg
    assert "25.0%" in msg


def test_browse_returns_content_from_response():
    """browse() extracts content field from OpenClaw response."""
    from src import openclaw_client
    body = {"content": "Deal 1: $99\nDeal 2: $149"}
    with patch("config.settings.OPENCLAW_WEBHOOK_URL", "http://localhost:4000/hook"), \
         patch("config.settings.OPENCLAW_SECRET", ""), \
         patch("requests.post", return_value=_ok(body)):
        result = openclaw_client.browse("https://woot.com/deals")
    assert "Deal 1" in result
    assert "Deal 2" in result


def test_browse_returns_empty_when_not_configured():
    """browse() with no OPENCLAW_WEBHOOK_URL returns empty string, no call."""
    from src import openclaw_client
    with patch("config.settings.OPENCLAW_WEBHOOK_URL", ""), \
         patch("requests.post") as mock_post:
        result = openclaw_client.browse("https://woot.com/deals")
    assert result == ""
    assert mock_post.call_count == 0


def test_browse_returns_empty_on_network_error():
    """Network error during browse must return '' not raise."""
    from src import openclaw_client
    with patch("config.settings.OPENCLAW_WEBHOOK_URL", "http://localhost:4000/hook"), \
         patch("config.settings.OPENCLAW_SECRET", ""), \
         patch("requests.post", side_effect=Exception("boom")):
        result = openclaw_client.browse("https://woot.com/deals")
    assert result == ""


def test_notify_swallows_network_exception():
    """notify() must not raise even if requests.post throws."""
    from src import openclaw_client
    with patch("config.settings.OPENCLAW_WEBHOOK_URL", "http://localhost:4000/hook"), \
         patch("config.settings.OPENCLAW_SECRET", ""), \
         patch("requests.post", side_effect=Exception("network down")):
        # Should not raise
        result = openclaw_client.notify("anything")
    assert result is False
