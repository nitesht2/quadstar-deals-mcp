#!/usr/bin/env bash
# Autonomous QuadStar agent run (lean split): backend scrapes (Scrapling), Hermes
# curates + writes copy in brand voice, then proposes Discord approval cards
# (human approves). Model = deepseek-v4-flash via OpenRouter (cheap; opus-4.6
# returns empty via Hermes streaming, sonnet is pricey). timeout caps runaway
# runs so they can't pile up across the 4 daily cron fires. cwd=/root for state.
cd /root
exec timeout 900 /usr/local/bin/hermes -z "$(cat /opt/quadstar-deals/.hermes-mission.txt)" \
  -m openrouter/deepseek/deepseek-v4-flash --yolo
