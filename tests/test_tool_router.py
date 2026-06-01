"""Unit tests for src/tool_router.py — the OpenClaw-path command dispatcher.

Verifies intent classification (keyword fallback + LLM success) and that
dispatch() calls the correct tool function in src/agent.py.
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --- Keyword fallback classifier ---

def test_keyword_classify_scrape():
    from src.tool_router import _keyword_classify
    assert _keyword_classify("scrape for gaming")["intent"] == "scrape"
    assert _keyword_classify("scrape for gaming")["params"]["category"] == "gaming"


def test_keyword_classify_status():
    from src.tool_router import _keyword_classify
    assert _keyword_classify("show pipeline status")["intent"] == "status"


def test_keyword_classify_cards():
    from src.tool_router import _keyword_classify
    assert _keyword_classify("send cards")["intent"] == "cards"


def test_keyword_classify_browse_with_url():
    from src.tool_router import _keyword_classify
    out = _keyword_classify("browse https://woot.com/deals")
    assert out["intent"] == "browse"
    assert out["params"]["url"] == "https://woot.com/deals"


def test_keyword_classify_price_check():
    from src.tool_router import _keyword_classify
    assert _keyword_classify("check price drops")["intent"] == "price_check"


def test_keyword_classify_watchlist_add():
    from src.tool_router import _keyword_classify
    out = _keyword_classify("add B0ABCDEFGH to watchlist")
    assert out["intent"] == "watchlist"
    assert out["params"]["action"] == "add"
    assert out["params"]["asin"] == "B0ABCDEFGH"


def test_keyword_classify_default_is_pipeline():
    from src.tool_router import _keyword_classify
    assert _keyword_classify("do the thing")["intent"] == "pipeline"


# --- Classify uses the LLM, falls back gracefully ---

def test_classify_falls_back_when_llm_unavailable():
    """When src.llm.generate returns None (no key / error), falls back to keyword."""
    from src import tool_router
    with patch("src.llm.generate", return_value=None):
        out = tool_router._classify("show status")
    assert out["intent"] == "status"


def test_classify_falls_back_when_llm_raises():
    """When src.llm.generate raises, _classify swallows it and uses keyword."""
    from src import tool_router
    with patch("src.llm.generate", side_effect=Exception("boom")):
        out = tool_router._classify("show status")
    assert out["intent"] == "status"


def test_classify_uses_llm_response_when_valid():
    """When the LLM returns valid JSON, _classify parses and uses it directly."""
    from src import tool_router
    with patch(
        "src.llm.generate",
        return_value='{"intent": "scrape", "params": {"category": "tech"}}',
    ):
        out = tool_router._classify("get me some tech deals")
    assert out["intent"] == "scrape"
    assert out["params"]["category"] == "tech"


def test_classify_extracts_json_from_noisy_llm_output():
    """LLM may wrap JSON in prose/markdown — _classify must still extract it."""
    from src import tool_router
    noisy = 'Sure!\n```json\n{"intent": "status", "params": {}}\n```'
    with patch("src.llm.generate", return_value=noisy):
        out = tool_router._classify("how are things")
    assert out["intent"] == "status"


# --- Dispatch calls the right tool ---

def test_dispatch_scrape_calls_scrape_deals():
    from src import tool_router
    with patch("src.agent._scrape_deals", return_value="OK scraped") as mock_scrape, \
         patch("src.tool_router._classify",
               return_value={"intent": "scrape", "params": {"category": "gaming"}}):
        result = tool_router.dispatch("scrape gaming")
    assert result == "OK scraped"
    mock_scrape.assert_called_once_with("gaming")


def test_dispatch_status_calls_get_status():
    from src import tool_router
    with patch("src.agent._get_status", return_value="STATUS OK") as mock_st, \
         patch("src.tool_router._classify",
               return_value={"intent": "status", "params": {}}):
        result = tool_router.dispatch("status")
    assert result == "STATUS OK"
    mock_st.assert_called_once()


def test_dispatch_pipeline_runs_scrape_then_cards():
    from src import tool_router
    with patch("src.agent._scrape_deals", return_value="scrape-ok") as m_scrape, \
         patch("src.agent._generate_and_send_cards", return_value="cards-ok") as m_cards, \
         patch("src.tool_router._classify",
               return_value={"intent": "pipeline", "params": {}}):
        result = tool_router.dispatch("run the pipeline")
    assert "scrape-ok" in result
    assert "cards-ok" in result
    m_scrape.assert_called_once()
    m_cards.assert_called_once()


def test_dispatch_browse_calls_browse_tool():
    from src import tool_router
    with patch("src.agent._browse_with_openclaw", return_value="browsed") as m_browse, \
         patch("src.tool_router._classify",
               return_value={"intent": "browse", "params": {"url": "https://woot.com"}}):
        result = tool_router.dispatch("browse woot")
    assert result == "browsed"
    m_browse.assert_called_once()


def test_dispatch_returns_error_string_on_tool_exception():
    """Tool raising must be caught — dispatch returns error string, no propagation."""
    from src import tool_router
    with patch("src.agent._get_status", side_effect=RuntimeError("kaboom")), \
         patch("src.tool_router._classify",
               return_value={"intent": "status", "params": {}}):
        result = tool_router.dispatch("status")
    assert "failed" in result.lower() or "kaboom" in result


def test_dispatch_unknown_intent_returns_message():
    from src import tool_router
    with patch("src.tool_router._classify",
               return_value={"intent": "nonsense", "params": {}}):
        result = tool_router.dispatch("????")
    assert "Unknown intent" in result
