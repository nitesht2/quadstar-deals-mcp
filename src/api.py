"""
api.py - FastAPI Server

Single entry point that runs everything:
  - Discord bot (deal cards with buttons)
  - APScheduler (cron every 2 hours — replaces GitHub Actions)
  - OpenClaw webhook endpoint (natural-language commands → agent)

Start with:
    uvicorn src.api:app --host 0.0.0.0 --port 8001
"""

import threading
from contextlib import asynccontextmanager

import asyncio
from fastapi import FastAPI, Request
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.agent import run_agent, _run_pipeline as _agent_pipeline
from config.settings import DISCORD_BOT_TOKEN


scheduler = AsyncIOScheduler()


def _start_discord_bot():
    """Run the Discord bot in a background thread with its own event loop."""
    if not DISCORD_BOT_TOKEN:
        print("  DISCORD_BOT_TOKEN not set — Discord bot disabled")
        return
    from src.discord_bot import bot
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.start(DISCORD_BOT_TOKEN))


def _log_job_error(job_name: str, exc: BaseException) -> None:
    """Log a scheduler job error without crashing the scheduler loop."""
    import traceback
    print(f"  [scheduler] {job_name} failed: {exc}", flush=True)
    traceback.print_exc()


async def _auto_run():
    """Scheduled job: scrape all categories, auto-post qualifying deals. No human involvement."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _run_pipeline)
    except Exception as e:
        _log_job_error("auto_run", e)


_ROTATION_CATEGORIES = ["home", "sports"]  # Rotates alongside tech each run
_rotation_index = 0  # Module-level counter — cycles through bonus categories


def _run_pipeline():
    global _rotation_index
    from src.amazon_scraper import run_amazon_scraper
    from src.rss_scraper import run_rss_scraper
    from src.agent import _run_pipeline as pipeline

    # Always scrape tech (primary), plus one rotating bonus category
    run_amazon_scraper(category_name="tech", fast_track=False)
    bonus = _ROTATION_CATEGORIES[_rotation_index % len(_ROTATION_CATEGORIES)]
    _rotation_index += 1
    try:
        bonus_count = run_amazon_scraper(category_name=bonus, fast_track=False)
        if bonus_count:
            print(f"  [scheduler] {bonus} category scraped {bonus_count} deals")
    except Exception as e:
        print(f"  [scheduler] {bonus} category scrape failed: {e}")

    rss_count = run_rss_scraper()
    if rss_count:
        print(f"  [scheduler] RSS scraped {rss_count} new deals")
    result = pipeline(limit=10)
    if result:
        print(f"  [scheduler] {result}")


async def _price_monitor_run():
    """Scheduled job: check watchlist ASINs for price drops."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _run_price_monitor)
    except Exception as e:
        _log_job_error("price_monitor", e)


def _run_price_monitor():
    from src.price_monitor import detect_drops
    drops = detect_drops()
    if drops:
        print(f"  [scheduler] Price monitor found {len(drops)} drops")


async def _reply_finder_run():
    """Scheduled job: find reply opportunities and send cards to Discord reply channel."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _run_reply_finder)
    except Exception as e:
        _log_job_error("reply_finder", e)


def _run_reply_finder():
    from src.reply_finder import run_reply_finder
    count = run_reply_finder()
    if count:
        print(f"  [scheduler] Reply finder: {count} cards sent to Discord", flush=True)


async def _ab_engagement_check():
    """Scheduled job: fetch engagement metrics for A/B test variants."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _run_ab_check)
    except Exception as e:
        _log_job_error("ab_engagement", e)


def _run_ab_check():
    from src.ab_testing import check_engagement
    updated = check_engagement()
    if updated:
        print(f"  [scheduler] Updated {updated} A/B engagement records")
    # Also collect tweet performance data
    from src.tweet_learner import collect_engagement as collect_tweet_engagement
    tweet_updated = collect_tweet_engagement()
    if tweet_updated:
        print(f"  [scheduler] Updated {tweet_updated} tweet performance records")


async def _weekly_digest_run():
    """Scheduled job: send weekly performance digest to Discord every Monday 9 AM."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _run_weekly_digest)


def _run_weekly_digest():
    from src.tweet_learner import send_weekly_digest
    send_weekly_digest()


async def _silence_check_run():
    """Scheduled job: alert if pipeline posted 0 deals in the last 24 hours."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _run_silence_check)
    except Exception as e:
        _log_job_error("silence_check", e)


def _run_silence_check():
    """Send Discord alert if no deals were auto-posted in the last 24 hours.

    Rate-limited to once per 24h so it doesn't spam if the drought continues.
    Skipped during quiet hours (before 8 AM PST) to avoid false alarms on fresh days.
    """
    import time, os
    from datetime import datetime

    # Only check during active hours — skip before 8 AM to avoid false alarm on new day
    pst_hour = (datetime.utcnow().hour - 7) % 24  # rough PST offset
    if pst_hour < 8:
        return

    cooldown_file = "/tmp/quadstar_silence_alert_ts"
    cooldown_secs = 86400  # 24h — one alert per drought
    try:
        if os.path.exists(cooldown_file):
            last = float(open(cooldown_file).read().strip())
            if time.time() - last < cooldown_secs:
                return
    except Exception:
        pass

    from src.database import _load_deals
    from datetime import timedelta

    # Count posts in last 24h (not just today's calendar day)
    deals = _load_deals()
    cutoff = datetime.now() - timedelta(hours=24)
    recent_posts = sum(
        1 for d in deals
        if d.get("is_posted") and d.get("posted_at")
        and datetime.fromisoformat(d["posted_at"]) >= cutoff
    )

    if recent_posts > 0:
        return  # All good — deals posted recently

    # No posts in 24h — fire alert
    try:
        open(cooldown_file, "w").write(str(time.time()))
    except Exception:
        pass

    from config.settings import DISCORD_WEBHOOK_URL
    if not DISCORD_WEBHOOK_URL:
        return
    import requests as _req
    now = datetime.now().strftime("%b %d, %I:%M %p")
    try:
        _req.post(
            DISCORD_WEBHOOK_URL,
            json={
                "username": "QuadStar System",
                "embeds": [{
                    "title": "⚠️ Pipeline Silent — No Posts in 24h",
                    "description": (
                        f"No deals have been auto-posted in the last 24 hours.\n\n"
                        f"**Possible causes:**\n"
                        f"• No deals passing score gate (≥58) or discount gate (≥25%)\n"
                        f"• Amazon scraper blocked or returning no results\n"
                        f"• Daily cap already hit earlier\n"
                        f"• Postiz API issue\n\n"
                        f"**Time:** {now}"
                    ),
                    "color": 0xFF6B35,
                    "footer": {"text": "Check /tmp/quadstar.log for details"},
                }],
            },
            timeout=5,
        )
        print(f"  [silence_check] Alert sent — no posts in 24h", flush=True)
    except Exception:
        pass


def _send_startup_alert():
    """Send a Discord webhook alert when the server starts or restarts.

    Rate-limited: suppressed if a startup alert was sent in the last 10 minutes
    to prevent spam during crash-loop restarts.
    """
    import time, os
    cooldown_file = "/tmp/quadstar_startup_ts"
    cooldown_secs = 21600  # 6 hours — one alert per reboot, no crash-loop spam
    try:
        if os.path.exists(cooldown_file):
            last = float(open(cooldown_file).read().strip())
            if time.time() - last < cooldown_secs:
                return  # Too soon — suppress
        open(cooldown_file, "w").write(str(time.time()))
    except Exception:
        pass

    from config.settings import DISCORD_WEBHOOK_URL
    if not DISCORD_WEBHOOK_URL:
        return
    from datetime import datetime
    import requests as _req
    now = datetime.now().strftime("%b %d, %I:%M %p")
    try:
        _req.post(
            DISCORD_WEBHOOK_URL,
            json={
                "username": "QuadStar System",
                "embeds": [{
                    "title": "Server Started",
                    "description": f"QuadStar Deals pipeline is online.\n**Time:** {now}\n**Cron:** deals 4:30AM–6:30PM hourly (PST), prices 6AM–6PM/2h, A/B 9AM+6PM",
                    "color": 0x2ECC71,
                    "footer": {"text": "Auto-restart via LaunchAgent"},
                }],
            },
            timeout=5,
        )
    except Exception:
        pass  # Don't crash startup over a notification


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start Discord bot in background thread
    bot_thread = threading.Thread(target=_start_discord_bot, daemon=True)
    bot_thread.start()

    # All jobs use max_instances=1 + coalesce=True so a long-running scrape
    # or Playwright hang cannot spawn a parallel run that corrupts JSON files
    # or fights over the same browser instance. misfire_grace_time caps how
    # late a delayed job can still fire before being skipped.
    # Active hours: 4:30 AM – 6:30 PM PST (no dead-night runs)
    # Pipeline: 4 peak times only — 8am, 12pm, 5pm, 7pm PST
    # Scheduler fires at :30 before each peak so scraping + content gen completes
    scheduler.add_job(
        _auto_run, "cron", hour="7,11,16,18", minute=30, id="auto_run",
        max_instances=1, coalesce=True, misfire_grace_time=600,
        jitter=1800,  # random 0-30 min delay — posts land at different times daily
    )
    # Price monitor: every 2h at :00, active 6 AM – 6 PM, with jitter
    scheduler.add_job(
        _price_monitor_run, "cron", hour="6-18/2", minute=0, id="price_monitor",
        max_instances=1, coalesce=True, misfire_grace_time=600,
        jitter=600,  # random 0-10 min
    )
    # A/B check: 9 AM and 6 PM (within active window)
    scheduler.add_job(
        _ab_engagement_check, "cron", hour="9,18", minute=45, id="ab_engagement",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    # Reply finder: DISABLED — was flagging account as inauthentic engagement
    # scheduler.add_job(
    #     _reply_finder_run, "cron", hour="6-18", minute="15,45", id="reply_finder",
    #     max_instances=1, coalesce=True, misfire_grace_time=300,
    # )
    # Weekly performance digest every Monday at 9 AM PST
    scheduler.add_job(
        _weekly_digest_run, "cron", day_of_week="mon", hour=9, minute=0, id="weekly_digest",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    # Silence check: 10 AM and 4 PM PST — alert if no posts in last 24h
    scheduler.add_job(
        _silence_check_run, "cron", hour="17,23", minute=0, id="silence_check",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    scheduler.start()
    print("  APScheduler started -- deals at 8am, 12pm, 5pm, 7pm PST, prices 6AM-6PM/2h, A/B 9AM+6PM, digest Mon 9AM")

    # Send startup alert to Discord so you know if server restarted overnight
    _send_startup_alert()

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(
    title="QuadStar Deals API",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    """Health check — confirms server is running."""
    return {"status": "ok"}


@app.post("/webhook/hermes")
@app.post("/webhook/openclaw")  # legacy alias — kept so old cron entries don't break
async def hermes_webhook(request: Request):
    """
    Receives a free-text command (from Hermes / cron / curl) and routes it through
    tool_router.dispatch() to the matching backend tool.

      Webhook URL: http://localhost:8001/webhook/hermes
      Method: POST
    Supported body formats: {"message": "..."} or {"content": "..."} or {"text": "..."}
    """
    body = await request.json()
    command = (
        body.get("message")
        or body.get("content")
        or body.get("text")
        or ""
    )
    if not command:
        return {"reply": "No command received."}

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(None, run_agent, command)
    return {"reply": response}


@app.post("/interact")
async def interact(request: Request):
    """
    Direct agent interaction endpoint for testing or custom integrations.
    Body: {"command": "scrape tech deals"}
    """
    body = await request.json()
    command = body.get("command", "")
    if not command:
        return {"reply": "No command provided."}
    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(None, run_agent, command)
    return {"reply": response}


@app.post("/debug/run-pipeline")
async def debug_run_pipeline():
    """Directly invoke agent _run_pipeline (auto-post path, no scraper) for testing Discord card format."""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _agent_pipeline)
    return {"result": result}


def _scrape_for_agent(category: str = "tech") -> str:
    """Scrape inventory for the agentic supervisor (no posting). Backend owns
    scraping — reliable Scrapling — so the agent only does judgment, not its
    own flaky browser-scrape. Returns a one-line count summary."""
    from src.amazon_scraper import run_amazon_scraper
    from src.rss_scraper import run_rss_scraper
    amz = run_amazon_scraper(category_name=category, fast_track=False)
    rss = 0
    try:
        rss = run_rss_scraper()
    except Exception as e:
        print(f"  [scrape] rss failed: {e}")
    return f"scraped {amz} amazon + {rss} rss ({category})"


@app.post("/tools/{tool}")
async def call_tool(tool: str, request: Request):
    """Run a bot-loop / posting-dependent tool IN THIS process (where the Discord
    bot's event loop lives). The MCP server delegates these here so that tools like
    generate_and_send_cards actually reach Discord — the MCP server runs in its own
    process and has no bot loop. Body carries typed args, e.g. {"limit": 3}.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    from src import agent as _a
    tools = {
        "generate_and_send_cards": lambda: _a._generate_and_send_cards(int(body.get("limit", 5))),
        "run_pipeline":            lambda: _a._run_pipeline(int(body.get("limit", 10))),
        "schedule_to_postiz":      lambda: _a._schedule_to_postiz(
                                       int(body.get("deal_id", 0) or 0),
                                       str(body.get("platforms", "")),
                                       bool(body.get("ab_test", False))),
        "check_price_drops":       lambda: _a._check_price_drops(),
        "scrape":                  lambda: _scrape_for_agent(str(body.get("category", "tech"))),
        # Agentic primitives — the agent's decision surface, guard-caged.
        "get_candidate_deals":     lambda: _a._get_candidate_deals(int(body.get("limit", 10))),
        "schedule_deal":           lambda: _a._schedule_deal(
                                       int(body.get("deal_id", 0) or 0),
                                       str(body.get("platforms", "")),
                                       str(body.get("scheduled_at", "")),
                                       str(body.get("copy_json", ""))),
    }
    fn = tools.get(tool)
    if not fn:
        return {"error": f"unknown tool '{tool}'"}
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, fn)
        return {"result": result}
    except Exception as exc:
        return {"error": f"tool '{tool}' failed: {exc}"}


@app.get("/status")
async def pipeline_status():
    """Pipeline health dashboard — deals scraped/posted/skipped today, source weights, score distribution."""
    from src.database import _load_deals
    from src.source_tracker import _load as _load_source_perf
    from datetime import datetime, timedelta
    from config.settings import PIPELINE_MAX_DAILY_POSTS

    deals = _load_deals()
    now = datetime.now()
    today = now.date().isoformat()
    cutoff_24h = now - timedelta(hours=24)

    posted_today = [
        d for d in deals
        if d.get("is_posted") and (d.get("posted_at") or "").startswith(today)
    ]
    posted_24h = [
        d for d in deals
        if d.get("is_posted") and d.get("posted_at")
        and datetime.fromisoformat(d["posted_at"]) >= cutoff_24h
    ]
    unposted = [d for d in deals if not d.get("is_posted") and d.get("is_active")]
    scraped_today = [
        d for d in deals
        if (d.get("scraped_at") or "").startswith(today)
    ]

    # Score distribution for unposted deals
    from src.database import score_deal
    scores = [score_deal(d) for d in unposted[:20]]  # Sample top 20
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0
    above_gate = sum(1 for s in scores if s >= 58)

    # Source performance
    try:
        source_data = _load_source_perf()  # {source_name: {scraped, posted, engagement}}
        sources = [
            {
                "source": k,
                "scraped": v.get("scraped", 0),
                "posted": v.get("posted", 0),
                "engagement": v.get("engagement", 0),
            }
            for k, v in source_data.items()
        ]
        sources.sort(key=lambda x: x["posted"], reverse=True)
    except Exception:
        sources = []

    return {
        "status": "ok",
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "today": {
            "scraped": len(scraped_today),
            "posted": len(posted_today),
            "daily_cap": PIPELINE_MAX_DAILY_POSTS,
            "cap_remaining": max(0, PIPELINE_MAX_DAILY_POSTS - len(posted_today)),
        },
        "last_24h": {
            "posted": len(posted_24h),
            "last_post": posted_24h[-1].get("posted_at", "none")[:19] if posted_24h else "none",
        },
        "queue": {
            "unposted_active": len(unposted),
            "avg_score_sample": avg_score,
            "above_score_gate": above_gate,
        },
        "sources": sources[:10],
        "next_run": "hourly at :30 (4:30AM–6:30PM PST)",
    }
