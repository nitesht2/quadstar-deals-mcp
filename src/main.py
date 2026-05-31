"""
main.py - QuadStar Deals Entry Point

For one-off manual runs from the terminal. The normal flow is:
  uvicorn src.api:app --host 0.0.0.0 --port 8001

That starts the Discord bot + APScheduler (every 2h) + OpenClaw webhook.
This file is for quick tests or manual pipeline triggers.

Usage:
    python -m src.main                    # Full pipeline run via agent
    python -m src.main "get status"       # Custom agent command
"""

import sys
from src.agent import run_agent


def main():
    command = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        "Collect feedback from Discord, scrape tech deals, "
        "then send top 5 unposted deals to Discord for approval."
    )
    print(f"\n[main] Running: {command}")
    result = run_agent(command)
    print(f"\n[main] Result: {result}")


if __name__ == "__main__":
    main()
