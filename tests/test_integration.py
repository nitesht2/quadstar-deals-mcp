"""End-to-end integration tests — exercise the dispatch pipeline.

The agent layer is now deterministic: run_agent() routes every command through
tool_router.dispatch() (one classify call, then a direct tool call). These tests
mock the LLM/tool layer so there's no network, but wire through the real
run_agent → dispatch → keyword-fallback path to catch real wiring bugs.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_run_agent_routes_through_dispatch(monkeypatch):
    """run_agent() must delegate to tool_router.dispatch() and return its result."""
    from src import agent as agent_mod
    import src.tool_router as tr

    captured = {}

    def fake_dispatch(command):
        captured["command"] = command
        return "dispatched: ok"

    monkeypatch.setattr(tr, "dispatch", fake_dispatch)
    result = agent_mod.run_agent("scrape tech deals")
    assert captured["command"] == "scrape tech deals"
    assert result == "dispatched: ok"


def test_run_agent_handles_dispatch_error(monkeypatch):
    """A dispatch exception must be caught and surfaced as a string, never raised."""
    from src import agent as agent_mod
    import src.tool_router as tr

    def boom(command):
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(tr, "dispatch", boom)
    result = agent_mod.run_agent("status")
    assert isinstance(result, str)
    assert "Dispatch error" in result


def test_keyword_fallback_classifies_without_llm():
    """The keyword classifier must work with zero LLM (the VPS-safe path)."""
    from src.tool_router import _keyword_classify

    assert _keyword_classify("scrape tech deals")["intent"] == "scrape"
    assert _keyword_classify("show pipeline status")["intent"] == "status"
    assert _keyword_classify("check price drops")["intent"] == "price_check"
    # Unknown commands default to the full pipeline.
    assert _keyword_classify("do the thing")["intent"] == "pipeline"


def test_structured_output_falls_back_on_provider_error(monkeypatch):
    """If generate_structured raises, notifier should fall back to templates."""
    from src import notifier

    # Provide a deal with a URL so the fallback templates work
    deal = {
        "id": 1,
        "title": "Sony WH-1000XM5 Wireless Headphones",
        "affiliate_url": "https://amazon.com/dp/TEST?tag=x",
        "discount_pct": 20,
    }

    # Force LLM available so we enter the structured path
    monkeypatch.setattr(notifier, "LLM_API_KEY", "fake-key")
    monkeypatch.setattr(notifier, "LLM_PROVIDER", "anthropic")

    def boom(prompt, schema, max_tokens=1200):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(notifier, "llm_generate_structured", boom)
    # Also make the legacy path return None so both paths fail → template
    monkeypatch.setattr(notifier, "llm_generate", lambda prompt, max_tokens=400: None)
    # match_platforms is called by the code
    monkeypatch.setattr("src.platform_router.match_platforms", lambda deal: [])

    content = notifier._generate_content(deal)
    # Must never raise, must always return the three keys
    assert "tweet_1" in content
    assert "tweet_2" in content
    assert "linkedin_post" in content
    assert "confidence" in content


def test_structured_output_uses_real_pydantic_path(monkeypatch):
    """Happy path: llm_generate_structured returns a DealContent instance."""
    from src import notifier
    Schema = notifier._deal_content_schema()

    deal = {
        "id": 1,
        "title": "Apple MacBook Air M4",
        "affiliate_url": "https://amazon.com/dp/TEST?tag=x",
        "discount_pct": 15,
    }
    monkeypatch.setattr(notifier, "LLM_API_KEY", "fake-key")
    monkeypatch.setattr(notifier, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr("src.platform_router.match_platforms", lambda deal: [])

    def fake_structured(prompt, schema, max_tokens=1200):
        obj = schema(
            tweet_1="BIG DEAL ON MACBOOK\n\nMacBook Air M4\n\n✅ 13\" display\n✅ M4 chip\n✅ 18h battery",
            tweet_2="Grab it.\n\nhttps://amazon.com/dp/TEST?tag=x\n\nFollow @quadstardeals for daily tech deals.",
            linkedin_post="",
        )
        return obj, 0.92

    monkeypatch.setattr(notifier, "llm_generate_structured", fake_structured)
    content = notifier._generate_content(deal)
    assert content["confidence"] == 0.92
    assert "MacBook" in content["tweet_1"] or "MACBOOK" in content["tweet_1"]
    assert "https://amazon.com/dp/TEST" in content["tweet_2"]
    assert "@quadstardeals" in content["tweet_2"]


def test_database_and_agent_have_no_circular_import():
    """Importing the main modules in order should not raise."""
    import importlib
    for mod in ("config.settings", "src.database", "src.llm", "src.postiz_client",
                "src.notifier", "src.agent"):
        importlib.import_module(mod)


def test_all_module_files_compile():
    """Every Python file under src/ and config/ must compile cleanly."""
    import py_compile
    for base in (ROOT / "src", ROOT / "config"):
        for p in base.rglob("*.py"):
            py_compile.compile(str(p), doraise=True)
