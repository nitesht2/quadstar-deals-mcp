#!/usr/bin/env bash
# Bootstrap a FRESH Ubuntu 24.04 VPS for QuadStar Deals from this repo.
# Automates the non-secret, non-OAuth steps. See SETUP.md for the full runbook +
# the manual bits this can't do (your .env secrets, X Developer App, Postiz
# account + OAuth connect, Hermes install). Run as root, from the repo root:
#   bash scripts/bootstrap_vps.sh
set -euo pipefail
REPO=/opt/quadstar-deals
HERE="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> [1/6] system deps"
apt-get update -qq
apt-get install -y -qq python3.12-venv git docker.io docker-compose-v2 rsync curl caddy || \
  apt-get install -y -qq python3.12-venv git docker.io rsync curl   # caddy may need its own repo

echo "==> [2/6] app code -> $REPO"
if [ "$HERE" != "$REPO" ]; then
  [ -d "$REPO/.git" ] || git clone https://github.com/nitesht2/quadstar-deals.git "$REPO"
  ( cd "$REPO" && git pull --ff-only || true )
else
  echo "    already in $REPO"
fi
cd "$REPO"
python3.12 -m venv venv 2>/dev/null || true
venv/bin/pip install -q -r requirements.txt
venv/bin/playwright install chromium || echo "    (playwright chromium install skipped/failed — backend scraper falls back to Scrapling)"

echo "==> [3/6] Postiz stack (Docker)"
mkdir -p /opt/postiz
if [ ! -f /opt/postiz/docker-compose.yaml ]; then
  cp deploy/postiz.docker-compose.yaml /opt/postiz/docker-compose.yaml
  echo "    !! EDIT /opt/postiz/docker-compose.yaml — replace every CHANGEME_* before 'docker compose up -d'"
else
  echo "    /opt/postiz/docker-compose.yaml exists — left as-is"
fi

echo "==> [4/6] Caddy (HTTPS) config"
if [ -f deploy/Caddyfile ]; then
  cp deploy/Caddyfile /etc/caddy/Caddyfile 2>/dev/null && systemctl restart caddy 2>/dev/null || \
    echo "    (edit /etc/caddy/Caddyfile hostname to yours, then: systemctl restart caddy)"
fi

echo "==> [5/6] backend systemd service"
cp deploy/quadstar-deals.service /etc/systemd/system/
systemctl daemon-reload
if [ -f "$REPO/.env" ]; then
  systemctl enable --now quadstar-deals
  sleep 4 && curl -s localhost:8001/health || true
else
  echo "    !! create $REPO/.env first (cp .env.example .env; fill secrets), then: systemctl enable --now quadstar-deals"
fi

echo "==> [6/6] cron (4x/day)"
( crontab -l 2>/dev/null | grep -vE "scrape tech|run_hermes_mission"; cat deploy/crontab.txt ) | crontab -
echo "    cron installed:"; crontab -l | grep -E "scrape tech|run_hermes" || true

cat <<'NEXT'

==> Bootstrap done. STILL MANUAL (see SETUP.md):
  • /opt/quadstar-deals/.env       — your secrets (cp .env.example .env)
  • /opt/postiz compose CHANGEME_* — pg password, JWT_SECRET, X app keys; then: cd /opt/postiz && docker compose up -d
  • X Developer App + Postiz account + connect @quadstardeals (browser/OAuth)
  • Hermes: install, then `hermes mcp add quadstar-deals --command /opt/quadstar-deals/scripts/run_mcp_server.sh`
           + `hermes skills install official/research/scrapling` + set ~/.hermes/.env LLM keys
NEXT
