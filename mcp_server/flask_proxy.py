"""Flask blueprint: expose the MCP streamable-HTTP endpoint at /mcp by
proxying to the uvicorn sidecar (mcp_server.http_entry) running in the same
container. Lets ONE App Service host both the web app and the MCP server —
no second deployment, no second pipeline.

Security model:
  - The sidecar binds 127.0.0.1 only — never directly reachable.
  - This blueprint FAILS CLOSED: without MCP_HTTP_TOKEN configured, /mcp
    returns 503 (it must never serve the tenant-wide catalog unauthenticated).
  - With MCP_HTTP_TOKEN set, requests must carry
    "Authorization: Bearer <token>" or they get 401 BEFORE any proxying.
  - This route deliberately has no @login_required: MCP clients have no
    webapp session; the bearer token IS the auth (same precedent as
    download_generated_file's documented exception).

Per-user ACL note: the proxied tools serve whatever the configured
allow-list says (MCP_ALLOWED_WORKSPACE_IDS); impact-analysis tools are
tenant-wide by design. See AGENTS.md "MCP server".
"""

from __future__ import annotations

import hmac
import os

import requests
from flask import Blueprint, Response, request

mcp_proxy_bp = Blueprint("mcp_proxy", __name__)

# Hop-by-hop + framing headers must not be forwarded verbatim (requests
# transparently decompresses, so content-encoding/length are dropped too).
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "host", "content-length", "content-encoding",
}

_REQUIRED_SUFFIX = "/mcp"


def _config() -> tuple[str, str] | None:
    """(sidecar /mcp URL, token) or None when not configured (fail closed)."""
    token = (os.getenv("MCP_HTTP_TOKEN") or "").strip()
    if not token:
        return None
    base = (os.getenv("MCP_SIDECAR_URL") or "http://127.0.0.1:8001").strip().rstrip("/")
    return f"{base}{_REQUIRED_SUFFIX}", token


@mcp_proxy_bp.route("/mcp", methods=["POST", "GET", "DELETE"])
def mcp_endpoint():
    cfg = _config()
    if cfg is None:
        return Response(
            "MCP endpoint not configured (set MCP_HTTP_TOKEN in app settings).\n",
            status=503,
            content_type="text/plain",
        )

    upstream_url, token = cfg
    # Constant-time compare (defense-in-depth: this is the only pre-auth on a
    # tenant-wide data surface; the sidecar re-validates the same token).
    supplied_auth = request.headers.get("Authorization", "").strip()
    expected_auth = f"Bearer {token}"
    if not hmac.compare_digest(supplied_auth.encode("utf-8"), expected_auth.encode("utf-8")):
        return Response(
            "Unauthorized: missing or invalid bearer token.\n",
            status=401,
            content_type="text/plain",
        )

    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    try:
        upstream_resp = requests.request(
            request.method,
            upstream_url,
            headers=forward_headers,
            data=request.get_data(),
            stream=True,
            timeout=(10, 300),
        )
    except requests.RequestException as exc:
        return Response(
            f"MCP sidecar unreachable: {exc}\n",
            status=503,
            content_type="text/plain",
        )

    resp_headers = [
        (k, v) for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
    ]

    def _stream():
        try:
            for chunk in upstream_resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            upstream_resp.close()

    return Response(_stream(), status=upstream_resp.status_code, headers=resp_headers)
