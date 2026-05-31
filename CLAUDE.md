## Home Checklist (owner action needed)

1. **Find OpenClaw webhook URL** — check openclaw.ai dashboard, add to `.env`:
   `OPENCLAW_WEBHOOK_URL=<url>` and `OPENCLAW_SECRET=<token>`
2. **Check Hermes Agent** — is it running on port 8000 on home computer? Report back to wire it in.
3. **Run Graphify on Windows** — `graphify claude install` then `graphify update .` in this project folder
4. **Verify price flood fix** — Postiz should show max 2 price drop posts per cycle, not 4+
5. **Check `data/source_performance.json`** — should have scraped/posted counts per source building up

## Bigger tasks queued (implement when ready)

- Pipeline health `/status` dashboard (deals scraped, posted, engagement, source weights)
- Merge `tweet_learner` + `ab_testing` into one system (Karpathy audit)
- `get_style_guidance()` in `src/tweet_learner.py` — verified NOT dead (style field exists in all records, Apr 25)

---

## graphify

This project is indexed at `graphify-out/` (377 nodes, 655 edges).

Rules:
- Before answering architecture or codebase questions, ALWAYS read `graphify-out/GRAPH_REPORT.md` first
- If `graphify-out/wiki/index.md` exists, navigate it instead of reading raw files
- After modifying code files in this session, run `/Users/nitesh/.local/bin/graphify update .` to keep the graph current (AST-only, no API cost)
