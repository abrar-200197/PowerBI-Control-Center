"""Post-deploy verification for the Power BI Control Center web app.

Run after every deploy (the Azure pipeline now smoke-checks /health; this
script additionally verifies the MCP surface and the ACL/thin behaviors):

    python scripts/verify-deploy.py                    # against prod
    python scripts/verify-deploy.py --app-url https://<app>.azurewebsites.net
    MCP_HTTP_TOKEN=... python scripts/verify-deploy.py  # or pass --token

The MCP token is read from the MCP_HTTP_TOKEN environment variable or the
--token argument — never hardcode it, never commit it.

Checks:
  1. GET  /health                     -> 200
  2. POST /mcp without a bearer token -> 401 (proxy fails closed)
  3. MCP initialize + tools/list      -> 200, tools present, and the expected
     build marker string appears in a tool description (proves THIS build is
     the one running — the check that caught the 2026-11-23 false deploy)
  4. get_workspace_reports with a foreign acl_workspace_ids -> aclFiltered denial
  5. get_workspace_reports for a real workspace      -> thin success shape

Exit code 0 = all checks passed; 1 = at least one failed.
"""
import argparse
import json
import os
import sys

import requests

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

DEFAULT_APP = "https://afi-powerbi-documentation-prod-app.azurewebsites.net"
DEFAULT_MARKER = "returns an empty list instead of falling back"

_failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--app-url", default=DEFAULT_APP)
    ap.add_argument("--token", default=os.getenv("MCP_HTTP_TOKEN", ""))
    ap.add_argument("--marker", default=DEFAULT_MARKER,
                    help="substring expected in an MCP tool description once "
                         "the intended build is deployed")
    args = ap.parse_args()

    base = args.app_url.rstrip("/")
    token = args.token
    if not token:
        print("NOTE: no MCP token provided (--token / MCP_HTTP_TOKEN) — "
              "authenticated MCP checks will be skipped.")
    H = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    print(f"Verifying {base}")

    # 1) health
    try:
        r = requests.get(f"{base}/health", timeout=30)
        check("health returns 200", r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as exc:
        check("health returns 200", False, str(exc))

    # 2) /mcp fails closed without a bearer
    try:
        r = requests.post(f"{base}/mcp", json={}, timeout=30)
        check("/mcp unauthenticated -> 401", r.status_code == 401,
              f"HTTP {r.status_code}")
    except Exception as exc:
        check("/mcp unauthenticated -> 401", False, str(exc))

    if not token:
        print(f"\nRESULT: {_failures and 'FAILED' or 'PASSED'} "
              f"(limited scope — no token)")
        return 1 if _failures else 0

    s = requests.Session()
    try:
        r = s.post(base + "/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "verify-deploy", "version": "1.0"},
            },
        }, headers=H, timeout=90)
        sid = r.headers.get("mcp-session-id") or ""
        if sid:
            s.post(base + "/mcp", json={"jsonrpc": "2.0",
                   "method": "notifications/initialized"},
                   headers={**H, "Mcp-Session-Id": sid}, timeout=30)
        check("MCP initialize", r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as exc:
        check("MCP initialize", False, str(exc))
        print(f"\nRESULT: FAILED")
        return 1

    def call(name, a_args, i):
        body = {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                "params": {"name": name, "arguments": a_args}}
        hh = dict(H)
        if sid:
            hh["Mcp-Session-Id"] = sid
        rr = s.post(base + "/mcp", json=body, headers=hh, timeout=300)
        data = [ln[5:].strip() for ln in rr.text.splitlines()
                if ln.startswith("data:")]
        obj = json.loads("\n".join(data) if data else rr.text)
        return json.loads(obj["result"]["content"][0]["text"])

    # 3) build marker in tool descriptions
    hh = dict(H)
    if sid:
        hh["Mcp-Session-Id"] = sid
    rr = s.post(base + "/mcp", json={"jsonrpc": "2.0", "id": 2,
                "method": "tools/list"}, headers=hh, timeout=90)
    data = [ln[5:].strip() for ln in rr.text.splitlines() if ln.startswith("data:")]
    tools = json.loads("\n".join(data) if data else rr.text)["result"]["tools"]
    marker_found = any(args.marker in (t.get("description") or "") for t in tools)
    check("build marker in tool descriptions", marker_found,
          f"marker={args.marker!r} ({len(tools)} tools)")

    # 4) pick a real workspace from the thin pack
    ws_id = None
    try:
        inner = call("list_workspaces", {}, 10)
        ws = inner.get("workspaces") or []
        ws_id = ws[0].get("id") if ws else None
        check("list_workspaces serves thin pack", bool(ws_id),
              f"{inner.get('count')} workspaces")
    except Exception as exc:
        check("list_workspaces serves thin pack", False, str(exc))

    if ws_id:
        try:
            inner = call("get_workspace_reports", {"workspace_id": ws_id}, 11)
            check("thin workspace reports", inner.get("success") is True
                  and inner.get("detailLevel") == "thin",
                  f"count={inner.get('count')}")
        except Exception as exc:
            check("thin workspace reports", False, str(exc))
        try:
            inner = call("get_workspace_reports", {
                "workspace_id": ws_id,
                "acl_workspace_ids": ["00000000-0000-0000-0000-000000000000"],
            }, 12)
            check("ACL denial for foreign workspace",
                  inner.get("success") is False and inner.get("aclFiltered") is True)
        except Exception as exc:
            check("ACL denial for foreign workspace", False, str(exc))

    print(f"\nRESULT: {'FAILED' if _failures else 'PASSED'} "
          f"({len(_failures)} failed)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
