"""mcp_server.py — Typed MCP seam for the Hermes brain (Phase 3a).

Exposes the backend's deterministic tools as native MCP tools so Hermes drives
the pipeline with TYPED arguments — no natural-language re-classification. This
is the PRIMARY agentic interface. The /webhook/openclaw + tool_router.dispatch()
path stays only for free-text Discord chat (lossy fallback).

The closed loop Hermes is meant to run each tick:
  1. read_feedback / check_ab_results / analyze_tweet_performance   (learn first)
  2. ingest_deals(<deals it scraped+extracted>)                     (supply data)
  3. run_pipeline()                                                 (code gates+posts)
  4. get_status()                                                   (report)

Every tool is a thin wrapper over the matching _tool function in src/agent.py,
which holds the real business logic (reused verbatim by tool_router.dispatch).

Run standalone (stdio transport):  python -m src.mcp_server
Register in Hermes config as an MCP server pointing at that command.
"""
import os

import requests
from mcp.server.fastmcp import FastMCP

from src import agent

mcp = FastMCP("quadstar-deals")

# The MCP server runs in its OWN process (spawned per call) and has no Discord
# bot loop — that lives only in the long-running FastAPI service. Tools that need
# the bot loop (approval cards) or that perform posting are delegated to the
# service over HTTP so they execute where the bot + scheduler actually run.
_SERVICE_URL = os.getenv("QUADSTAR_SERVICE_URL", "http://127.0.0.1:8001")


def _via_service(tool: str, **args) -> str:
    """Delegate a bot-loop/posting-dependent tool to the running FastAPI service."""
    try:
        r = requests.post(f"{_SERVICE_URL}/tools/{tool}", json=args, timeout=180)
        data = r.json()
        return data.get("result") or data.get("error") or str(data)
    except Exception as exc:
        return f"Service call '{tool}' failed: {exc} (is the quadstar-deals service up on :8001?)"


# ── Deal intake (the Phase-3 ingest loop) ──────────────────────────────────────

@mcp.tool()
def ingest_deals(deals: list[dict]) -> str:
    """Persist deals you scraped + extracted into the pipeline database.

    Pass a list of deal objects. Each needs at minimum: title, asin, price.
    Optional: list_price, discount, image_url, url, rating, review_count, category.
    The backend recomputes discount from prices (never trusts your arithmetic),
    dedups by ASIN/URL/fuzzy-title, filters by image quality + tech keywords,
    and stores qualifying deals. This is how you feed deals in — run_pipeline
    then scores, verifies live prices, and schedules them.
    """
    return agent._ingest_deals(deals)


@mcp.tool()
def scrape_deals(category: str = "tech") -> str:
    """Legacy in-backend scraper (Scrapling/Playwright). Optional during the
    transition to Hermes-driven scraping — prefer ingest_deals. Returns a count."""
    return agent._scrape_deals(category)


# ── Pipeline + posting ─────────────────────────────────────────────────────────

@mcp.tool()
def run_pipeline(limit: int = 10) -> str:
    """Score, gate, and auto-schedule already-ingested deals to Postiz.

    Gates (all must pass): discount >= threshold, deal score >= threshold,
    content confidence >= threshold, live Amazon price re-verify, ASIN cooldown.
    Idempotent: ASIN dedup + daily cap mean a retry never double-posts. Returns
    a summary, or an empty string when nothing qualified (stay silent)."""
    return _via_service("run_pipeline", limit=limit)


@mcp.tool()
def get_unposted_deals(limit: int = 5) -> str:
    """Return the top-ranked unposted deals (JSON list) ready for review."""
    return agent._get_unposted_deals(limit)


@mcp.tool()
def generate_and_send_cards(limit: int = 5) -> str:
    """Generate tweet content for top unposted deals and send Discord approval
    cards (the human gate). Returns how many cards were sent."""
    return _via_service("generate_and_send_cards", limit=limit)


@mcp.tool()
def schedule_to_postiz(deal_id: int, platforms: str = "", ab_test: bool = False) -> str:
    """Schedule an approved deal to social platforms via Postiz. platforms is a
    comma-separated list (empty = auto-route). ab_test=True posts two variants."""
    return _via_service("schedule_to_postiz", deal_id=deal_id, platforms=platforms, ab_test=ab_test)


@mcp.tool()
def post_to_telegram(deal_id: int) -> str:
    """Post a deal directly to Telegram, bypassing Postiz (side-channel)."""
    return _via_service("post_to_telegram", deal_id=deal_id)


# ── Learning loop (call these FIRST each tick) ─────────────────────────────────

@mcp.tool()
def read_feedback() -> str:
    """Read Discord reactions/comments on posted deals and update preferences."""
    return agent._read_feedback()


@mcp.tool()
def check_ab_results() -> str:
    """Check engagement on A/B test variants and return a summary."""
    return agent._check_ab_results()


@mcp.tool()
def analyze_tweet_performance() -> str:
    """Collect engagement data and return a tweet performance report."""
    return agent._analyze_tweet_performance()


# ── Price monitoring + watchlist ───────────────────────────────────────────────

@mcp.tool()
def check_price_drops() -> str:
    """Check watchlist ASINs for price drops; queue eligible auto-reposts."""
    return _via_service("check_price_drops")


@mcp.tool()
def manage_watchlist(action: str, asin: str = "", title: str = "") -> str:
    """Add/remove/list manually-pinned ASINs for price monitoring.
    action is 'add' | 'remove' | 'list'."""
    return agent._manage_watchlist(action, asin, title)


@mcp.tool()
def cancel_price_drop(deal_id: int) -> str:
    """Cancel a pending price-drop repost before its 15-min timer fires."""
    return agent._cancel_price_drop(deal_id)


# ── Status + config ────────────────────────────────────────────────────────────

@mcp.tool()
def get_status() -> str:
    """Pipeline stats: active deals, posted, scheduled, categories (JSON)."""
    return agent._get_status()


@mcp.tool()
def get_posting_insights() -> str:
    """Smart-scheduling insights (JSON): engagement-by-PST-hour from your past posts,
    default peak hours, and slots already booked in Postiz to avoid. Use this to
    choose each deal's schedule_at (ISO 8601 UTC) when you ingest it."""
    return agent._get_posting_insights()


@mcp.tool()
def add_category(name: str, keywords: str) -> str:
    """Register a new product category. keywords = comma-separated list."""
    return agent._add_category(name, keywords)


@mcp.tool()
def browse_with_openclaw(url: str, instruction: str = "") -> str:
    """Browse a bot-blocked URL via OpenClaw as a real user. Optional fallback."""
    return agent._browse_with_openclaw(url, instruction)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
