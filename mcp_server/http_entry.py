"""Streamable-HTTP entrypoint for REMOTE (hosted) deployment.

This is the "same behavior as the web app" path: the MCP server runs
centrally (Azure App Service / container) with credentials in App Service
settings, and colleagues connect with a URL - no local install, no .env:

    opencode mcp add powerbi-control-center --global --url "https://<app>.azurewebsites.net/mcp" --header "Authorization=Bearer <token>"

Deployment requirements (do NOT skip in production):
  - Set MCP_HTTP_TOKEN in the App Service settings; every request must carry
    "Authorization: Bearer <token>" (enforced by the guard below). Locally,
    leaving it unset runs unguarded for development only.
  - Put the app behind Entra/Easy Auth and/or APIM before exposing it beyond
    the team. Per-user workspace filtering (on-behalf-of Power BI access)
    is the planned next phase - the tools already accept an allow-list.

Run locally:  python -m mcp_server.http_entry   (needs: pip install uvicorn)
"""

from __future__ import annotations

import os

# Importing mcp_server.server loads .env (candidate search) and registers
# every tool on the shared FastMCP instance.
from mcp_server.server import mcp

_HTTP_TOKEN = (os.getenv("MCP_HTTP_TOKEN") or "").strip()


class _BearerGuard:
    """Minimal ASGI middleware: require Authorization: Bearer <MCP_HTTP_TOKEN>."""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _HTTP_TOKEN:
            await self._app(scope, receive, send)
            return
        headers = {k: v for k, v in scope.get("headers") or []}
        supplied = headers.get(b"authorization", b"").decode("latin-1").strip()
        if supplied != f"Bearer {_HTTP_TOKEN}":
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"Unauthorized: missing or invalid bearer token.",
                }
            )
            return
        await self._app(scope, receive, send)


def build_app():
    return _BearerGuard(mcp.streamable_http_app())


def main() -> None:
    if not _HTTP_TOKEN:
        print("WARNING: MCP_HTTP_TOKEN is not set - the endpoint is UNGUARDED.")
        print("         Set it for any shared/deployed instance.")
    import uvicorn

    host = os.getenv("MCP_HTTP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_HTTP_PORT", "8000"))
    # MCP clients connect to http(s)://<host>:<port>/mcp
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
