#!/usr/bin/env bash
# Autonomous QuadStar agent run: Hermes scrapes + curates + writes copy, then
# proposes Discord approval cards (human approves). Model pinned to a working one
# (opus-4.6 returns empty via Hermes streaming). cwd=/root for Hermes state.
cd /root
exec /usr/local/bin/hermes -z "$(cat /opt/quadstar-deals/.hermes-mission.txt)" \
  -m anthropic/claude-4.6-sonnet --yolo
