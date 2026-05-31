# Legacy / archived

Superseded code kept for reference only. Not imported by the live app.
The canonical service is `src/api.py` (FastAPI + APScheduler + `src/discord_bot.py`).

| File | Was | Why archived |
|---|---|---|
| `scheduler.py` | Standalone 4x/day pipeline, ✅/❌ Discord reactions | Orphaned (imported by nothing). Has a DeepSeek "research-boost" step worth porting into `src/agent._run_pipeline` if desired. |
| `run_pipeline.py` | Scraper/poster wrapper used by `scheduler.py` | Orphaned with scheduler.py. |
| `run_pipeline_v2.py` | Hermes-based research variant | Superseded. |
| `run_pipeline_v3.py` | DuckDuckGo+DeepSeek variant | Superseded. |
| `hermes-bot-bot.py` | Standalone discord.py button bot (Apr 2026) | Superseded by `src/discord_bot.py`, which `src/api.py` starts. |

To restore any of these, `git mv` it back to the project root.
