#!/usr/bin/env bash
# One-shot deploy: push the local repo state to the VPS and restart the service.
# Run from anywhere: scripts/deploy.sh
#
# Syncs code (NOT venv/.env/data — those live on the server), reinstalls deps if
# requirements changed, restarts the systemd service, and health-checks it.
# Secrets never leave the server; the VPS .env is preserved across deploys.
set -euo pipefail

VPS="${QUADSTAR_VPS:-root@2.25.135.103}"
DEST=/opt/quadstar-deals

cd "$(dirname "$0")/.."

echo "→ rsync → ${VPS}:${DEST}"
# NOTE: intentionally NO --delete. The VPS holds runtime-only state not in the
# repo (.env, data/, and previously .voice/) — --delete once silently wiped the
# voice files. Additive sync only; prune the VPS by hand if ever needed.
rsync -az \
  --exclude 'venv/' --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude 'graphify-out/' --exclude 'data/' --exclude '.env' \
  -e 'ssh -o BatchMode=yes' \
  ./ "${VPS}:${DEST}/"

echo "→ pip install (no-op if unchanged) + restart service"
ssh -o BatchMode=yes "${VPS}" "cd ${DEST} \
  && venv/bin/pip install -q -r requirements.txt \
  && systemctl restart quadstar-deals \
  && sleep 4 \
  && echo -n 'service: ' && systemctl is-active quadstar-deals \
  && echo -n 'health: ' && curl -s --max-time 5 http://127.0.0.1:8001/health && echo"

echo "✓ deployed"
