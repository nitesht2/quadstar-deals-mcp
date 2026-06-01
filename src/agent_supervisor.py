"""agent_supervisor.py — reliability harness around the (flaky) Hermes agent.

The agentic runtime, made safe to be load-bearing. Human-approval model —
NOTHING auto-posts; the agent PROPOSES, the human approves. One supervised cycle:

  1. SCRAPE   — backend pulls inventory (reliable Scrapling), via the service.
  2. AGENT    — run Hermes (judgment: read candidates, choose, propose_deal which
                sends a Discord approval card). Subprocess, hard timeout.
  3. VERIFY   — did the agent genuinely run (clean exit + substantive output)?
  4. FALLBACK — only if the agent truly FAILED (empty/crash/timeout) AND eligible
                inventory exists, propose deterministically (generate_and_send_cards)
                so the human still gets approval cards. Else respect the agent /
                stay silent. No path auto-posts.

This is "agent decides, human approves, code guarantees the cards": the agent
gets full judgment, a deterministic path covers its failures, and every post
passes a human gate. Net: agentic, reliable, and human-approved.

Run by cron (replaces the raw `hermes -z` mission runner):
    python -m src.agent_supervisor
Env:
    QUADSTAR_SERVICE_URL   backend base (default http://127.0.0.1:8001)
    HERMES_BIN             hermes binary (default /usr/local/bin/hermes)
    HERMES_MODEL           model (default deepseek/deepseek-chat)
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
HERMES_MODEL = os.getenv("HERMES_MODEL", "deepseek/deepseek-chat")
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


def _count_eligible() -> int:
    """How many candidate deals the deterministic gate considers postable now.
    Used only to decide whether a FAILED agent warrants a fallback proposal."""
    raw = _service("get_candidate_deals", limit=25)
    if not raw:
        return 0
    try:
        return sum(1 for d in json.loads(raw) if d.get("eligible"))
    except (ValueError, TypeError):
        return 0


# An agent run counts as "ran" only if it exited clean AND produced substantive
# output. The cheap deepseek model returns empty via `hermes -z` (true no-show);
# a working model emits its reasoning + a result line. This threshold separates
# "agent failed" from "agent ran and decided".
_MIN_AGENT_OUTPUT_CHARS = 40


def _run_agent() -> tuple[bool, bool]:
    """Run the Hermes judgment mission. Returns (ran_clean, produced_output).

    ran_clean    — exited 0 within the timeout (didn't crash/hang).
    produced_output — emitted substantive text (not the empty no-show).
    Both true ⇒ the agent genuinely ran and its decision should be respected.
    """
    if not os.path.exists(HERMES_MISSION_FILE):
        print(f"  [supervisor] mission file missing: {HERMES_MISSION_FILE}", flush=True)
        return False, False
    if not os.path.exists(HERMES_BIN):
        print(f"  [supervisor] hermes binary missing: {HERMES_BIN}", flush=True)
        return False, False
    mission = open(HERMES_MISSION_FILE).read()
    try:
        proc = subprocess.run(
            [HERMES_BIN, "-z", mission, "-m", HERMES_MODEL, "--yolo"],
            timeout=AGENT_TIMEOUT_SECS, capture_output=True, text=True,
        )
        out = (proc.stdout or "").strip()
        has_output = len(out) >= _MIN_AGENT_OUTPUT_CHARS
        if proc.returncode != 0:
            print(f"  [supervisor] agent exit {proc.returncode}: {proc.stderr[-300:]}", flush=True)
        if not has_output:
            print("  [supervisor] agent produced no substantive output (no-show)", flush=True)
        return proc.returncode == 0, has_output
    except subprocess.TimeoutExpired:
        print(f"  [supervisor] agent timed out after {AGENT_TIMEOUT_SECS}s", flush=True)
        return False, False
    except Exception as exc:
        print(f"  [supervisor] agent launch failed: {exc}", flush=True)
        return False, False


def run_cycle(scrape: bool = True) -> dict:
    """One supervised agentic cycle (human-approval model). Returns an outcome dict.

    Nothing auto-posts: the agent PROPOSES deals (Discord approval cards), the
    human approves. Verification is "did the agent run and propose?" — not "did
    it post". The deterministic fallback also PROPOSES (generate_and_send_cards)
    so the human still gets cards if the agent truly failed.
    """
    if scrape:
        print(f"  [supervisor] scrape: {_service('scrape', category='tech')}", flush=True)

    ran_clean, has_output = _run_agent()
    agent_ran = ran_clean and has_output  # genuinely ran (not empty / crash / hang)

    outcome = {"agent_ran": agent_ran, "fallback_used": False, "fallback_result": ""}

    # Agent ran → respect its judgment (it proposed cards, or declined junk/stale).
    if agent_ran:
        outcome["status"] = "agent_ran"
        print("  [supervisor] agent ran — proposals (if any) sent for approval; done", flush=True)
        return outcome

    # Agent truly FAILED. Propose deterministically so the human still gets cards
    # to approve — but only if there's eligible inventory.
    eligible = _count_eligible()
    if eligible == 0:
        outcome["status"] = "correctly_silent"
        print("  [supervisor] agent failed but 0 eligible — nothing to propose", flush=True)
        return outcome

    print(f"  [supervisor] agent FAILED (no output/crash), {eligible} eligible → fallback proposes cards", flush=True)
    outcome["fallback_used"] = True
    outcome["fallback_result"] = _service("generate_and_send_cards", limit=3)
    outcome["status"] = "fallback_proposed"
    return outcome


if __name__ == "__main__":
    result = run_cycle()
    print(f"[supervisor] cycle complete: {json.dumps(result)}", flush=True)
