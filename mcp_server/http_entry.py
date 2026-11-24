"""Streamable-HTTP entrypoint for REMOTE (hosted) deployment.

This is the "same behavior as the web app" path: the MCP server runs
centrally (Azure App Service / container) with credentials in App Service
settings, and colleagues connect with a URL - no local install, no .env:

    opencode mcp add powerbi-control-center --global --url "https://<app>.azurewebsites.net/mcp" --header "Authorization=Bearer <token>"

Auth model (the guard below resolves, in order):
  1. MCP_USER_TOKENS  (JSON: personal token -> {"upn", "workspaces"}):
     per-user identity; the user's workspace allow-list is INJECTED into
     every tools/call argument (_acl_workspace_ids) so tools filter per
     person. Unknown tokens are rejected (fail closed) when this map is
     configured.
  2. MCP_HTTP_TOKEN   (shared transport token): global scope - tools use
     the machine-wide MCP_ALLOWED_WORKSPACE_IDS.
  3. Neither configured: endpoint is UNGUARDED (local development only).

Deployment requirements (do NOT skip in production):
  - Set MCP_HTTP_TOKEN and/or MCP_USER_TOKENS in App Service settings.
  - Put the app behind Entra/Easy Auth and/or APIM before exposing it beyond
    the team. Live per-user Power BI access is a planned future phase
    (the admin artifactAccess API is retired - see tests/user_acl docs).

Run locally:  python -m mcp_server.http_entry   (needs: pip install uvicorn)
"""

from __future__ import annotations

import hmac
import os
import threading

# Importing mcp_server.server loads .env (candidate search) and registers
# every tool on the shared FastMCP instance.
from mcp_server import user_acl
from mcp_server.server import mcp

HTTP_TOKEN_ENV = "MCP_HTTP_TOKEN"


class _BearerGuard:
    """ASGI middleware: auth + per-user ACL injection for /mcp requests."""

    def __init__(self, app):
        self._app = app

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _bearer(headers_list) -> str:
        for key, value in headers_list:
            if key.lower() == b"authorization":
                supplied = value.decode("latin-1").strip()
                if supplied.lower().startswith("bearer "):
                    return supplied[7:].strip()
        return ""

    @staticmethod
    async def _drain(receive) -> bytes:
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"") or b""
            if not msg.get("more_body", False):
                break
        return body

    @staticmethod
    def _replay_receive(body: bytes):
        import anyio

        sent = {"done": False}

        async def receive():
            if sent["done"]:
                # Post-body receives must PARK like the real uvicorn stream:
                # returning http.disconnect here makes the streamable
                # transport tear the session down mid-response (truncated
                # SSE body). Parking is cancelled on task teardown.
                await anyio.sleep(3600 * 24)
                return {"type": "http.disconnect"}
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive

    @staticmethod
    def _with_length(scope, body: bytes):
        headers = [(k, v) for k, v in scope.get("headers") or [] if k.lower() != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode("latin-1")))
        scope = dict(scope)
        scope["headers"] = headers
        return scope

    @staticmethod
    async def _reject(send, status: int, message: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": message})

    # -- main -------------------------------------------------------------
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        supplied = self._bearer(scope.get("headers") or [])
        shared = (os.getenv(HTTP_TOKEN_ENV) or "").strip()
        users = user_acl.parse_user_map(os.getenv("MCP_USER_TOKENS"))

        acl: set | None = None
        if users:
            entry = users.get(supplied)
            if entry is not None:
                acl = user_acl.effective_workspaces(entry)
            elif shared and hmac.compare_digest(supplied.encode("utf-8"), shared.encode("utf-8")):
                acl = None  # global scope identity: tools use machine-wide env
            else:
                if os.getenv("MCP_GUARD_DEBUG"):
                    print(f"GUARD: {scope.get('method')} bearer=...{supplied[-4:]} -> 401 unknown", flush=True)
                await self._reject(
                    send, 401,
                    b"Unauthorized: unknown bearer token (per-user MCP tokens are enforced).",
                )
                return
        elif shared:
            if not hmac.compare_digest(supplied.encode("utf-8"), shared.encode("utf-8")):
                await self._reject(send, 401, b"Unauthorized: missing or invalid bearer token.")
                return
        # else: no auth configured - unguarded (development only).

        if os.getenv("MCP_GUARD_DEBUG"):
            print(f"GUARD: {scope.get('method')} /mcp bearer=...{supplied[-4:]} "
                  f"acl={'len ' + str(len(acl)) if acl is not None else 'None'}", flush=True)

        # Intercept ONLY POSTs when a per-user ACL is active. The GET SSE
        # stream and DELETE must pass through with their REAL receive - a
        # replayed receive reports an instant disconnect, which kills the
        # long-lived stream and hangs the client.
        if acl is not None and scope.get("method") == "POST":
            body = await self._drain(receive)
            new_body = user_acl.inject_acl_into_body(body, acl)
            # ALWAYS install the replayed receive after draining - even when
            # the body was not rewritten (e.g. initialize): the drained
            # original receive would otherwise surface http.disconnect and
            # Starlette would raise ClientDisconnect reading the body.
            scope = self._with_length(scope, new_body)
            receive = self._replay_receive(new_body)

        await self._app(scope, receive, send)


def build_app():
    return _BearerGuard(mcp.streamable_http_app())


def _warm_catalog_thin() -> None:
    """Background thin-pack warm-up (the webapp's warm pattern, OOM-safe).

    Loads ONLY the thin packs (ui_home_index, summary) so the first agent
    call does not pay cold-start latency. Never touches heavy blobs
    (workspace_catalog/impact_index) — see AGENTS.md gotcha #11.
    """
    from mcp_server.server import _svc

    def _run():
        try:
            svc = _svc()
            svc.get_json("ui_home_index.json")
            svc.get_json("summary.json")
            print("MCP warm-up: thin catalog packs loaded", flush=True)
        except Exception as exc:  # noqa: BLE001 - warm-up is best effort
            print(f"MCP warm-up skipped: {type(exc).__name__}: {exc}", flush=True)

    threading.Thread(target=_run, name="mcp-catalog-warm", daemon=True).start()


def main() -> None:
    if not (os.getenv(HTTP_TOKEN_ENV) or "").strip() and not user_acl.user_map_configured():
        print("WARNING: neither MCP_HTTP_TOKEN nor MCP_USER_TOKENS is set -")
        print("         the endpoint is UNGUARDED. Set one for any shared instance.")
    import uvicorn

    _warm_catalog_thin()
    # P1: default to loopback — the standalone pip-installed entrypoint used
    # to bind 0.0.0.0 by default while unguarded, exposing the tenant catalog
    # to the LAN (start-webapp.sh explicitly sets 127.0.0.1 for the container;
    # the two defaults now agree).
    host = os.getenv("MCP_HTTP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_HTTP_PORT", "8000"))
    # MCP clients connect to http(s)://<host>:<port>/mcp
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
