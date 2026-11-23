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
# orphan and keeps supervising.
#
# P3 hang watchdog: a sidecar that HANGS (event loop stuck, no HTTP answer)
# never exits, so exit-only supervision would not recover it. The watchdog
# probes 127.0.0.1:<port>/mcp with a 10s timeout — ANY HTTP answer counts as
# alive (the bearer guard's 401 proves the loop is dispatching); no answer
# within the timeout kills the process so the loop restarts it. The probe is
# python-based (no curl dependency in slim images). All wait/kill exit codes
# are guarded so set -e cannot kill the supervisor loop.
(
  while true; do
    python -m mcp_server.http_entry &
    sidcar_pid=$!
    (
      sleep 90  # first boot imports are heavy; do not probe a still-starting sidecar
      while true; do
        probe_code=$(python -c '
import os, urllib.request
try:
    r = urllib.request.urlopen(
        "http://127.0.0.1:%s/mcp" % os.environ.get("MCP_HTTP_PORT", "8001"),
        timeout=10)
    print(r.status)
except Exception as exc:
    print(getattr(exc, "code", 0) or 0)
' 2>/dev/null) || true
        if [ -z "$probe_code" ] || [ "$probe_code" = "0" ]; then
          echo "[sidecar-watchdog] sidecar not answering HTTP - killing for restart" >&2
          kill "$sidcar_pid" 2>/dev/null || true
          break
        fi
        sleep 30
      done
    ) &
    watchdog_pid=$!
    wait "$sidcar_pid" || true
    kill "$watchdog_pid" 2>/dev/null || true
    wait "$watchdog_pid" 2>/dev/null || true
    echo "[sidecar-supervisor] sidecar exited; restarting in 5s..." >&2
    sleep 5
  done
) &

# Front door: unchanged gunicorn configuration - see the Dockerfile comment
# for why the worker count must stay at 1 (OAuth session-file race).
exec gunicorn --bind=0.0.0.0:8000 --timeout 900 --workers 1 --threads 8 \
  --worker-class gthread --max-requests 500 --max-requests-jitter 50 \
  --access-logfile - --error-logfile - app:app
