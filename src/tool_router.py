"""tool_router.py — Intent classifier + direct tool dispatcher.

The sole executor for natural-language commands (replaces the old LangGraph
ReAct loop). One LLM call (DeepSeek Flash → OpenRouter fallback) classifies
intent, then a direct call to the matching _tool_function() in src/agent.py.
No agent loop, no state machine — planning lives in the Hermes agent upstream.

Falls back to keyword matching if the LLM is unavailable.
"""
import json
import re


_INTENT_SCHEMA = """
Return ONLY valid JSON:
{
  "intent": one of [pipeline, scrape, ingest, cards, status, unposted, schedule,
                    telegram, feedback, price_check, watchlist, cancel_drop,
                    ab_results, tweet_perf, add_category, browse],
  "params": {
    "category": "tech",      // for scrape
    "deal_id": 0,            // for schedule / telegram / cancel_drop
    "limit": 5,              // for cards / unposted
    "asin": "",              // for watchlist
    "action": "list",        // for watchlist: add | remove | list
    "title": "",             // for watchlist add
    "url": "",               // for browse
    "instruction": "",       // for browse
    "keywords": ""           // for add_category
  }
}
"pipeline" = scrape THEN send cards (full cycle).
"browse" = use OpenClaw browser on a specific URL.
Return ONLY the JSON, no explanation.
"""


def _classify(command: str) -> dict:
    """Single LLM call (DeepSeek Flash → OpenRouter fallback) to classify the
    command into intent + params. Falls back to keyword matching if the LLM is
    unavailable or returns unparseable output."""
    try:
        from src.llm import generate
        raw = generate(
            f"{_INTENT_SCHEMA}\n\nCommand: {command}",
            max_tokens=200,
            system="You are an intent classifier. Output only JSON.",
        )
        if raw:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                return json.loads(m.group())
    except Exception as exc:
        print(f"  [tool_router] LLM classify failed ({exc}) — keyword fallback")

    return _keyword_classify(command)


def _keyword_classify(command: str) -> dict:
    """Fallback classifier when Ollama is unavailable. Pure regex/keywords."""
    cmd = command.lower()

    if "browse" in cmd or "woot" in cmd or "bestbuy" in cmd or "reddit" in cmd:
        url_m = re.search(r"https?://\S+", command)
        return {"intent": "browse", "params": {"url": url_m.group() if url_m else ""}}
    if "scrape" in cmd:
        cat = re.search(r"(?:for|category)\s+(\w+)", cmd)
        return {"intent": "scrape", "params": {"category": cat.group(1) if cat else "tech"}}
    if any(w in cmd for w in ("card", "send card", "generate card")):
        return {"intent": "cards", "params": {}}
    if "status" in cmd:
        return {"intent": "status", "params": {}}
    if any(w in cmd for w in ("price drop", "price check", "check price", "monitor price")):
        return {"intent": "price_check", "params": {}}
    if "telegram" in cmd:
        deal_id_m = re.search(r"\b(\d+)\b", cmd)
        return {
            "intent": "telegram",
            "params": {"deal_id": int(deal_id_m.group()) if deal_id_m else 0},
        }
    if "watchlist" in cmd:
        action = "list"
        if "add" in cmd:
            action = "add"
        elif "remove" in cmd or "unpin" in cmd:
            action = "remove"
        asin_m = re.search(r"\b(B0[0-9A-Z]{8})\b", command)
        return {
            "intent": "watchlist",
            "params": {"action": action, "asin": asin_m.group() if asin_m else ""},
        }
    if "unposted" in cmd or "queue" in cmd:
        return {"intent": "unposted", "params": {}}
    if "schedule" in cmd or "post to postiz" in cmd:
        deal_id_m = re.search(r"\b(\d+)\b", cmd)
        return {
            "intent": "schedule",
            "params": {"deal_id": int(deal_id_m.group()) if deal_id_m else 0},
        }
    if "cancel" in cmd and "drop" in cmd:
        deal_id_m = re.search(r"\b(\d+)\b", cmd)
        return {
            "intent": "cancel_drop",
            "params": {"deal_id": int(deal_id_m.group()) if deal_id_m else 0},
        }
    if "feedback" in cmd or "reaction" in cmd:
        return {"intent": "feedback", "params": {}}
    if "a/b" in cmd or "ab result" in cmd or "ab test" in cmd:
        return {"intent": "ab_results", "params": {}}
    if "tweet" in cmd and ("perf" in cmd or "analyze" in cmd or "engagement" in cmd):
        return {"intent": "tweet_perf", "params": {}}

    # Default: full pipeline (scrape + cards)
    return {"intent": "pipeline", "params": {}}


def dispatch(command: str) -> str:
    """Classify and execute a natural-language command. Returns result string."""
    from src import agent as _a

    classified = _classify(command)
    intent = classified.get("intent", "pipeline")
    p = classified.get("params", {}) or {}

    if intent == "pipeline":
        cat = p.get("category", "tech")
        scrape_out = _a._scrape_deals(cat)
        cards_out = _a._generate_and_send_cards()
        return f"{scrape_out}\n{cards_out}"

    dispatch_map = {
        "scrape":       lambda: _a._scrape_deals(p.get("category", "tech")),
        "ingest":       lambda: _a._ingest_deals(p.get("deals", [])),
        "cards":        lambda: _a._generate_and_send_cards(p.get("limit", 5)),
        "status":       lambda: _a._get_status(),
        "unposted":     lambda: _a._get_unposted_deals(p.get("limit", 5)),
        "schedule":     lambda: _a._schedule_to_postiz(
                            int(p.get("deal_id", 0) or 0),
                            str(p.get("platforms", "")),
                            bool(p.get("ab_test", False))),
        "telegram":     lambda: _a._post_to_telegram(int(p.get("deal_id", 0) or 0)),
        "feedback":     lambda: _a._read_feedback(),
        "price_check":  lambda: _a._check_price_drops(),
        "watchlist":    lambda: _a._manage_watchlist(
                            p.get("action", "list"),
                            p.get("asin", ""),
                            p.get("title", "")),
        "cancel_drop":  lambda: _a._cancel_price_drop(int(p.get("deal_id", 0) or 0)),
        "ab_results":   lambda: _a._check_ab_results(),
        "tweet_perf":   lambda: _a._analyze_tweet_performance(),
        "add_category": lambda: _a._add_category(
                            p.get("category", ""),
                            p.get("keywords", "")),
        "browse":       lambda: _a._browse_with_openclaw(
                            p.get("url", ""),
                            p.get("instruction", "")),
    }

    fn = dispatch_map.get(intent)
    if not fn:
        return f"Unknown intent: {intent}"
    try:
        return fn()
    except Exception as exc:
        return f"Tool '{intent}' failed: {exc}"
