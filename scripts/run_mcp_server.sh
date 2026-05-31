#!/usr/bin/env bash
# Launcher for the QuadStar Deals MCP server (the typed seam Hermes drives).
# Pins cwd to the project root so `-m src.mcp_server` resolves src/ + config/
# and DATA_DIR defaults to ./data. Registered in Hermes via:
#   hermes mcp add quadstar-deals --command <abs path to this script>
# Reused by the VPS deploy (Phase 4) — same entrypoint everywhere.
set -euo pipefail
cd "$(dirname "$0")/.."
exec venv/bin/python -m src.mcp_server
