# QuadStar Deals — Full Setup / Disaster Recovery

Rebuild the entire automation from this repo on a fresh machine. The **process
runs on a Linux VPS**, not your laptop — your laptop is only the control machine
(git + SSH). So there are two recovery scenarios.

```
VPS (always-on)
├─ FastAPI backend (systemd :8001)  — scrape, score, gate, Discord bot, posting
├─ Postiz stack (Docker :4007)      — postiz + postgres + redis + temporal + elasticsearch
├─ Caddy (:80/:443)                 — HTTPS reverse proxy (Let's Encrypt) for Postiz OAuth
├─ Hermes Agent (CLI, run by cron)  — curate + write copy (deepseek-v4-flash); Scrapling skill
└─ cron (4×/day)                    — backend scrape  +  hermes mission
```

---

## Scenario A — laptop died, VPS still running (most common)
**The pipeline keeps running on the VPS untouched.** You only need control back:
1. New machine: `git clone https://github.com/nitesht2/quadstar-deals.git`
2. Restore SSH access to the VPS (add your new public key in Hostinger panel → SSH keys, or use the root password). Test: `ssh root@<VPS_IP>`.
3. To push code changes: `scripts/deploy.sh` (rsyncs → restarts service).

That's it — nothing else to rebuild.

---

## Scenario B — full rebuild on a fresh VPS
Provision Ubuntu 24.04 (≥8GB RAM — Postiz+Temporal+Elasticsearch need it). Then:

### 1. Clone + system deps + app
```bash
ssh root@<NEW_VPS_IP>
apt-get update && apt-get install -y python3.12-venv git docker.io docker-compose-v2 rsync curl
git clone https://github.com/nitesht2/quadstar-deals.git /opt/quadstar-deals
cd /opt/quadstar-deals
python3.12 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/playwright install chromium    # for the backend Scrapling/Playwright scraper
```

### 2. Secrets — create `/opt/quadstar-deals/.env`
Copy `.env.example` → `.env` and fill every key (see that file's comments). You
provide these from your own accounts; they are NOT in git:
DeepSeek, OpenRouter, Discord bot token + channel ID, Amazon affiliate tag,
Postiz API key + JWT secret + USER_ID + TWITTER_ID (filled after step 4).
`chmod 600 .env`

### 3. Postiz stack (Docker)
```bash
mkdir -p /opt/postiz && cp deploy/postiz.docker-compose.yaml /opt/postiz/docker-compose.yaml
# Edit /opt/postiz/docker-compose.yaml: replace every CHANGEME_* with strong secrets.
#   - set a real POSTGRES_PASSWORD (same value in all 3 places)
#   - set a long random JWT_SECRET
#   - X_API_KEY / X_API_SECRET = your X Developer App consumer keys (step 4b)
#   - set FRONTEND_URL / MAIN_URL / NEXT_PUBLIC_BACKEND_URL / X_URL to your https host
cd /opt/postiz && docker compose up -d
# Boot order matters: wait for elasticsearch green, then temporal SERVING, then postiz.
# If postiz backend won't bind :3000, it's waiting on Temporal — give it 1-2 min.
```

### 4. HTTPS (Caddy) — Postiz OAuth needs a stable https URL
```bash
apt-get install -y caddy   # (or the official cloudsmith repo)
# Point a stable hostname at the VPS (Hostinger gives srvXXXX.hstgr.cloud).
cp deploy/Caddyfile /etc/caddy/Caddyfile   # edit the hostname to yours
systemctl restart caddy                    # auto-provisions Let's Encrypt cert
```
**4a. Register a Postiz user:** open `https://<your-host>`, sign up (NOT_SECURED=true → no email confirm).
**4b. X Developer App** (developer.x.com): create app → User authentication settings →
OAuth 1.0a ON, App permissions Read+Write, Callback URI `https://<your-host>/integrations/social/x`,
Website `https://<your-host>`. Copy Consumer Key/Secret → into the compose `X_API_KEY`/`X_API_SECRET`,
`docker compose up -d` again.
**4c. Connect @quadstardeals** in Postiz (Add Channel → X → Authorize).
**4d. Fill `.env`:** POSTIZ_API_KEY + POSTIZ_TWITTER_ID + POSTIZ_USER_ID + POSTIZ_JWT_SECRET
(queries are in `.env.example` comments).

### 5. Backend service (systemd)
```bash
cp deploy/quadstar-deals.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now quadstar-deals
curl localhost:8001/health   # -> {"status":"ok"}
```

### 6. Hermes Agent (the brain)
Install Hermes per Nous Research docs, then:
```bash
hermes mcp add quadstar-deals --command /opt/quadstar-deals/scripts/run_mcp_server.sh   # confirm: y
hermes skills install official/research/scrapling                                        # confirm: y
/usr/local/lib/hermes-agent/venv/bin/pip install "scrapling[all]"                         # stealth scraper for Hermes
# Set ~/.hermes/.env: real DEEPSEEK_API_KEY + OPENROUTER_API_KEY (no box-drawing chars!).
# Optional fallback in ~/.hermes/config.yaml:
#   fallback_model:
#     provider: openrouter
#     model: deepseek/deepseek-chat
hermes mcp test quadstar-deals   # -> Connected, tools listed
```

### 7. Cron (4×/day autonomy)
```bash
( crontab -l 2>/dev/null; cat deploy/crontab.txt ) | crontab -
```
Mission runs deepseek-v4-flash (see `scripts/run_hermes_mission.sh`); model bug:
`anthropic/claude-opus-4.6` returns empty via `hermes -z` — keep a working `-m` pinned.

### One-shot option
`scripts/bootstrap_vps.sh` automates steps 1, 3, 5, 7 (the non-secret, non-OAuth parts).
Steps 2, 4, 6 still need your secrets / browser logins.

---

## What is NOT in git (and must be re-supplied)
- `/opt/quadstar-deals/.env` — all secrets (template: `.env.example`).
- `data/*.json` — runtime state (regenerates).
- Postiz DB contents (your account + connected @quadstardeals) — recreated in step 4.
- Real values for the `CHANGEME_*` in the compose.
Everything else (all app code + every infra config template) IS in this repo.
