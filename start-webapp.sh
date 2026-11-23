#!/bin/sh
# One App Service, two servers:
#   - MCP streamable-HTTP sidecar on 127.0.0.1:8001 (uvicorn; proxied by the
#     Flask app at /mcp - see mcp_server/flask_proxy.py)
#   - Flask web app on 0.0.0.0:8000 (gunicorn, unchanged serving model)
set -e

export MCP_HTTP_HOST="${MCP_HTTP_HOST:-127.0.0.1}"
export MCP_HTTP_PORT="${MCP_HTTP_PORT:-8001}"

# Sidecar in the background. If it dies the web app keeps serving and /mcp
# fails closed (503) until the container restarts.
python -m mcp_server.http_entry &

# Front door: unchanged gunicorn configuration - see the Dockerfile comment
# for why the worker count must stay at 1 (OAuth session-file race).
exec gunicorn --bind=0.0.0.0:8000 --timeout 900 --workers 1 --threads 8 \
  --worker-class gthread --max-requests 500 --max-requests-jitter 50 \
  --access-logfile - --error-logfile - app:app
