"""agent_supervisor.py — reliability harness around the (flaky) Hermes agent.

The agentic runtime, made safe to be load-bearing. One supervised cycle:

  1. SCRAPE   — backend pulls inventory (reliable Scrapling), via the service.
  2. AGENT    — run Hermes (judgment: read candidates, choose, write voice copy,
                schedule_deal). Subprocess, hard timeout.
  3. VERIFY   — did the agent actually post? Snapshot posts-today before/after.
  4. FALLBACK — if the agent posted nothing BUT eligible candidates exist, it
                no-showed (cheap models drop tool calls) → run the deterministic
                run_pipeline so the cycle never goes empty. If nothing was
                eligible, silence is correct — no fallback.

This is "agent decides, code guarantees": the agent gets full judgment, but a
deterministic path catches its failures. Net: agentic AND reliable.

Run by cron (replaces the raw `hermes -z` mission runner):
    python -m src.agent_supervisor
Env:
    QUADSTAR_SERVICE_URL   backend base (default http://127.0.0.1:8001)
    HERMES_BIN             hermes binary (default /usr/local/bin/hermes)
    HERMES_MODEL           model (default openrouter/deepseek/deepseek-v4-flash)
    HERMES_MISSION_FILE    mission text path (default /opt/quadstar-deals/.hermes-mission.txt)
    AGENT_TIMEOUT_SECS     subprocess cap (default 900)
"""
from __future__ import annotations

import json
import os
import subprocess

import requests

SERVICE_URL = os.getenv("QUADSTAR_SERVICE_URL", "http://127.0.0.1:8001")
HERMES_BIN = os.getenv("HERMES_BIN", "/usr/local/bin/hermes")
HERMES_MODEL = os.getenv("HERMES_MODEL", "openrouter/deepseek/deepseek-v4-flash")
HERMES_MISSION_FILE = os.getenv("HERMES_MISSION_FILE", "/opt/quadstar-deals/.hermes-mission.txt")
AGENT_TIMEOUT_SECS = int(os.getenv("AGENT_TIMEOUT_SECS", "900"))


# Per-tool HTTP timeouts. Scrape walks ~20 Amazon pages twice (Playwright +
# Scrapling) and legitimately takes minutes — give it room; others are quick.
_TOOL_TIMEOUTS = {"scrape": 600, "run_pipeline": 300}


def _service(tool: str, **payload) -> str:
    """POST to a backend /tools route. Returns the result string ('' on error)."""
    try:
        r = requests.post(f"{SERVICE_URL}/tools/{tool}", json=payload,
                          timeout=_TOOL_TIMEOUTS.get(tool, 60))
        data = r.json()
        return data.get("result") or data.get("error") or ""
    except Exception as exc:
        print(f"  [supervisor] service '{tool}' failed: {exc}", flush=True)
        return ""


def _posts_today() -> int:
    try:
        r = requests.get(f"{SERVICE_URL}/status", timeout=15)
        return int(r.json().get("today", {}).get("posted", 0))
    except Exception:
        return 0


def _count_eligible() -> int:
    """How many candidate deals the deterministic gate considers postable now.
    This is the verification signal: if >0 and the agent posted 0, it no-showed."""
    raw = _service("get_candidate_deals", limit=25)
    if not raw:
        return 0
    try:
        return sum(1 for d in json.loads(raw) if d.get("eligible"))
    except (ValueError, TypeError):
        return 0


def _run_agent() -> bool:
    """Run the Hermes judgment mission as a subprocess. True if it exits clean."""
    if not os.path.exists(HERMES_MISSION_FILE):
        print(f"  [supervisor] mission file missing: {HERMES_MISSION_FILE}", flush=True)
        return False
    if not os.path.exists(HERMES_BIN):
        print(f"  [supervisor] hermes binary missing: {HERMES_BIN}", flush=True)
        return False
    mission = open(HERMES_MISSION_FILE).read()
    try:
        proc = subprocess.run(
            [HERMES_BIN, "-z", mission, "-m", HERMES_MODEL, "--yolo"],
            timeout=AGENT_TIMEOUT_SECS, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f"  [supervisor] agent exit {proc.returncode}: {proc.stderr[-300:]}", flush=True)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"  [supervisor] agent timed out after {AGENT_TIMEOUT_SECS}s", flush=True)
        return False
    except Exception as exc:
        print(f"  [supervisor] agent launch failed: {exc}", flush=True)
        return False


def run_cycle(scrape: bool = True) -> dict:
    """One supervised agentic cycle. Returns a structured outcome dict."""
    if scrape:
        print(f"  [supervisor] scrape: {_service('scrape', category='tech')}", flush=True)

    before = _posts_today()
    agent_ok = _run_agent()
    after = _posts_today()
    posted_by_agent = max(0, after - before)

    outcome = {"agent_ok": agent_ok, "posted_by_agent": posted_by_agent,
               "fallback_used": False, "fallback_result": ""}

    if posted_by_agent > 0:
        outcome["status"] = "agent_posted"
        print(f"  [supervisor] agent posted {posted_by_agent} — no fallback needed", flush=True)
        return outcome

    eligible = _count_eligible()
    if eligible == 0:
        outcome["status"] = "correctly_silent"
        print("  [supervisor] agent posted 0, 0 eligible — correctly silent", flush=True)
        return outcome

    # Agent posted nothing while postable inventory exists → no-show. Guarantee a run.
    print(f"  [supervisor] agent no-show (posted 0, {eligible} eligible) → deterministic fallback", flush=True)
    outcome["fallback_used"] = True
    outcome["fallback_result"] = _service("run_pipeline", limit=10)
    outcome["status"] = "fallback"
    return outcome


if __name__ == "__main__":
    result = run_cycle()
    print(f"[supervisor] cycle complete: {json.dumps(result)}", flush=True)
