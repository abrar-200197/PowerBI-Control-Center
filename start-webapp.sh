#!/bin/sh
# One App Service, two servers:
#   - MCP streamable-HTTP sidecar on 127.0.0.1:8001 (uvicorn; proxied by the
#     Flask app at /mcp - see mcp_server/flask_proxy.py)
#   - Flask web app on 0.0.0.0:8000 (gunicorn, unchanged serving model)
set -e

export MCP_HTTP_HOST="${MCP_HTTP_HOST:-127.0.0.1}"
export MCP_HTTP_PORT="${MCP_HTTP_PORT:-8001}"

# Sidecar supervision (P2): the old one-shot background start left /mcp
# failing closed (503) until a full container restart whenever the sidecar
# process died. A dedicated background subshell keeps restarting it; `exec
# gunicorn` below replaces the parent shell, but the subshell survives as an
# orphan and keeps supervising. `|| echo` stops set -e from killing the loop
# when the sidecar exits non-zero.
(
  while true; do
    python -m mcp_server.http_entry \
      || echo "[sidecar-supervisor] sidecar exited; restarting in 5s..." >&2
    sleep 5
  done
) &

# Front door: unchanged gunicorn configuration - see the Dockerfile comment
# for why the worker count must stay at 1 (OAuth session-file race).
exec gunicorn --bind=0.0.0.0:8000 --timeout 900 --workers 1 --threads 8 \
  --worker-class gthread --max-requests 500 --max-requests-jitter 50 \
  --access-logfile - --error-logfile - app:app
