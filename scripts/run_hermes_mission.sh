#!/usr/bin/env bash
# Autonomous QuadStar agentic cycle — via the reliability supervisor.
#
# The supervisor (src/agent_supervisor.py) scrapes inventory (backend), runs the
# Hermes judgment mission (read candidates -> choose -> write voice copy ->
# schedule_deal, all guard-caged), verifies the agent actually posted, and falls
# back to the deterministic run_pipeline if the agent no-showed while postable
# deals existed. So the cycle is agentic AND never goes empty.
#
# Model / timeout / mission-file are configurable via env (see agent_supervisor.py).
# Replaces the old raw `hermes -z` invocation.
set -euo pipefail
cd "$(dirname "$0")/.."
# deepseek/deepseek-chat (DIRECT via DEEPSEEK_API_KEY) — drives the lean mission
# reliably (verified: emits tool calls + a decision). NOTE the no-show was the
# *flash* model via OpenRouter (openrouter/deepseek/deepseek-v4-flash returns
# empty through `hermes -z`); the direct deepseek-chat model is reliable + cheap.
export HERMES_MODEL="${HERMES_MODEL:-deepseek/deepseek-chat}"
export HERMES_MISSION_FILE="${HERMES_MISSION_FILE:-$(pwd)/.hermes-mission.txt}"
exec venv/bin/python -m src.agent_supervisor
