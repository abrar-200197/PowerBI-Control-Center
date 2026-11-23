"""Integration test: Flask /mcp proxy -> uvicorn sidecar (offline).

Starts the MCP sidecar as a real subprocess, then exercises the Flask app's
/mcp proxy route with a test client:
  - 401 without a bearer token (fail-closed guard)
  - 200 + MCP initialize result with the correct bearer token
Run standalone:  python tests/test_mcp_flask_proxy.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# UTF-8 stdout/stderr (repo pattern for cp1252 consoles).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

REPO = Path(__file__).resolve().parents[1]
PORT = "8123"
TOKEN = "test-token"

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "smoke", "version": "0.0.0"},
    },
}


def test_mcp_proxy_end_to_end():
    os.environ["MCP_HTTP_PORT"] = PORT
    os.environ["MCP_HTTP_HOST"] = "127.0.0.1"
    os.environ["MCP_HTTP_TOKEN"] = TOKEN
    os.environ["MCP_SIDECAR_URL"] = f"http://127.0.0.1:{PORT}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.http_entry"],
        cwd=str(REPO),
        env={**os.environ},
    )
    try:
        time.sleep(4)  # let uvicorn bind

        import app as app_module  # noqa: PLC0415 - heavy import on purpose

        client = app_module.app.test_client()

        # 1) No token -> 401 before any proxying.
        r = client.post("/mcp", json=INIT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"

        # 2) Wrong token -> 401.
        r = client.post(
            "/mcp",
            headers={"Authorization": "Bearer wrong-token"},
            json=INIT,
        )
        assert r.status_code == 401, f"expected 401, got {r.status_code}"

        # 3) Correct token -> proxied to the sidecar; MCP initialize answers.
        r = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/json, text/event-stream",
            },
            json=INIT,
        )
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.data[:300]}"
        assert b"powerbi-control-center" in r.data, r.data[:300]
        print("proxy: 401 / 401 / 200 with MCP initialize response")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    test_mcp_proxy_end_to_end()
    print("OK")
