# Hermes Agent Migration Plan

## Context
Pipeline is fully working on OpenClaw with 2 agents. Migrating to a single Hermes agent for the learning loop (auto-writes skills from experience). OpenClaw stays for Chanakya AI (personal assistant) in other channels.

## Architecture Change

### Before (OpenClaw)
```
@QuadStar Deals Agent (OpenClaw, deals only)
@QuadStar Schedule and Analytics (OpenClaw, analytics only)
QuadstarDeals#3389 (discord.py, buttons only)
```

### After (Hermes)
```
@QuadStar Deals Agent (Hermes, deals + analytics combined)
QuadstarDeals#3389 (discord.py, buttons only)
```

## Single Hermes Agent Config
- **Discord bot:** QuadStar Deals Agent (token: MTQ5MjMx...AGo, ID: 1492314174242426950)
- **Channel:** #quadstar-deal (1487564741097427044)
- **Model:** Ollama qwen3.5:9b (via host.docker.internal:11434)
- **Respond on:** @mention only
- **Skills:** Combined deals + analytics skill file
- **Cron jobs:** deal-pipeline (every 3h), daily-analytics (daily 9am)
- **Learning loop:** Enabled (writes skills after ~15 tool iterations)

## Migration Steps

### 1. Update Hermes (v0.6.0 -> latest)
```bash
# Rebuild container from latest source
docker stop hermes-agent
docker rm hermes-agent
# Pull latest, rebuild, run with same hermes-data volume
```

### 2. Mount OpenClaw data and migrate
```bash
# Add volume mount for ~/.openclaw
docker exec hermes-agent hermes claw migrate --source /host-openclaw --migrate-secrets --dry-run
docker exec hermes-agent hermes claw migrate --source /host-openclaw --migrate-secrets
```

### 3. Configure Discord
Set in Hermes config:
- DISCORD_BOT_TOKEN = QuadStar Deals Agent token
- DISCORD_IGNORE_NO_MENTION = true (only respond when @mentioned)
- DISCORD_ALLOWED_USERS = your Discord user ID

### 4. Add combined skill file
Create a single skill that covers both deals and analytics:
- Scrape deals, send cards, check status, add categories
- Schedule to Postiz, read feedback, daily reports
- All via curl to http://localhost:8001/webhook/openclaw

### 5. Create cron jobs
```bash
hermes cron create "every 3h" "Scrape tech deals then send deal cards to Discord" \
  --name deal-pipeline --deliver discord
hermes cron create "0 9 * * *" "Collect feedback then report pipeline status" \
  --name daily-analytics --deliver discord
```

### 6. Stop OpenClaw agents for this channel
- Remove deals + analytics accounts from openclaw.json
- Keep default (Chanakya AI) account running for other channels
- Restart OpenClaw gateway

### 7. Retire QuadStar Schedule and Analytics bot
- Remove from Discord server (optional, or just leave idle)
- Its token is no longer used by anything

## What Stays the Same
- FastAPI server on port 8001 (LangGraph agent, deal cards, Postiz scheduling)
- QuadstarDeals#3389 discord.py bot (button clicks)
- Ollama on port 11434
- Postiz on port 4007
- All deal data in data/deals.json

## Verification
1. `docker exec hermes-agent hermes status` -- shows Discord connected
2. @mention in #quadstar-deal -- Hermes responds
3. `hermes cron list` -- shows both jobs
4. Wait for 3h cron -- deal cards appear
5. Click Approve -- Postiz schedules post
6. After a few days -- check ~/.hermes/skills/ for auto-generated skills

## Risk
- Low. The FastAPI backend doesn't change. Only the Discord conversation layer switches from OpenClaw to Hermes.
- Rollback: re-enable OpenClaw agents, restart gateway. Takes 2 minutes.
