#!/bin/bash
# QuadStar Deals - Start server
# Port 8000 is taken by hermes-agent Docker container, so we use 8001.

set -a
source .env 2>/dev/null
set +a

PORT=${APP_PORT:-8001}

source venv/bin/activate
echo "Starting QuadStar Deals on port $PORT..."
uvicorn src.api:app --host 0.0.0.0 --port "$PORT"
