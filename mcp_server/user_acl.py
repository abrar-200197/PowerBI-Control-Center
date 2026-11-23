"""Per-user identity and workspace scoping for the HOSTED MCP server.

Phase 2 (v1.4.0): the transport guard resolves the caller from a personal
bearer token (App Service setting MCP_USER_TOKENS) and injects that user's
workspace allow-list into the tool-call arguments, so the catalog service's
existing per-workspace filtering applies per person — the "webapp" model.

Format (App Service setting / .env MCP_USER_TOKENS):

    {"<personal-token>": {"upn": "name@ashleyfurniture.com",
                          "workspaces": ["<guid>", "..."]},
     "<personal-token-2>": {"upn": "..."}}

Semantics:
  - "workspaces" present (even empty) -> tools filter to that list.
  - "workspaces" absent               -> fall back to the global
    MCP_ALLOWED_WORKSPACE_IDS, then tenant-wide.
  - When MCP_USER_TOKENS is configured, an unknown bearer token is REJECTED
    (fail closed); the shared MCP_HTTP_TOKEN remains accepted as the
    "global scope" identity.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Set


def parse_user_map(raw: Optional[str]) -> Dict[str, Dict[str, object]]:
    """Parse MCP_USER_TOKENS into {token: {"upn": str, "workspaces": list|None}}.

    Malformed entries are skipped with a warning; the server never fails to
    start because of one bad entry.
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        print("WARNING: MCP_USER_TOKENS is not valid JSON - per-user ACL disabled")
        return {}
    if not isinstance(data, dict):
        print("WARNING: MCP_USER_TOKENS must be a JSON object - per-user ACL disabled")
        return {}
    out: Dict[str, Dict[str, object]] = {}
    for token, entry in data.items():
        token = (token or "").strip()
        if not token or not isinstance(entry, dict):
            continue
        upn = str(entry.get("upn") or "").strip()
        workspaces: Optional[List[str]] = None
        raw_ws = entry.get("workspaces")
        if isinstance(raw_ws, list):
            workspaces = [str(w).strip() for w in raw_ws if str(w).strip()]
        out[token] = {"upn": upn, "workspaces": workspaces}
    return out


def user_map_configured(raw: Optional[str] = None) -> bool:
    raw = os.getenv("MCP_USER_TOKENS") if raw is None else raw
    return bool(parse_user_map(raw))


def resolve_user(bearer_token: str, raw: Optional[str] = None) -> Optional[Dict[str, object]]:
    """Personal token -> user entry, or None when unknown."""
    token = (bearer_token or "").strip()
    if not token:
        return None
    return parse_user_map(raw).get(token)


def global_workspaces(raw: Optional[str] = None) -> Optional[Set[str]]:
    """The machine-wide allow-list (MCP_ALLOWED_WORKSPACE_IDS); None = tenant-wide."""
    raw = os.getenv("MCP_ALLOWED_WORKSPACE_IDS") if raw is None else raw
    raw = (raw or "").strip()
    if not raw or raw.lower() in ("all", "*"):
        return None
    return {s.strip() for s in raw.split(",") if s.strip()}


def effective_workspaces(entry: Dict[str, object]) -> Optional[Set[str]]:
    """Resolve a user entry to the workspace set tools should see.

    Explicit "workspaces" list wins (even when empty -> empty set).
    Absent -> global MCP_ALLOWED_WORKSPACE_IDS -> None (tenant-wide).
    """
    ws = entry.get("workspaces")
    if ws is not None:
        return {str(w).strip() for w in ws if str(w).strip()}
    return global_workspaces()


def inject_acl_into_body(body: bytes, workspace_ids: Set[str]) -> bytes:
    """Stamp _acl_workspace_ids into a tools/call JSON-RPC message.

    Only tools/call messages are touched; anything else (initialize,
    resources/list, non-JSON) passes through byte-identical.
    """
    if not body:
        return body
    try:
        msg = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if isinstance(msg, dict) and msg.get("method") == "tools/call":
        params = msg.setdefault("params", {})
        if isinstance(params, dict):
            args = params.setdefault("arguments", {})
            if isinstance(args, dict):
                args["acl_workspace_ids"] = sorted(workspace_ids)
                return json.dumps(msg).encode("utf-8")
    return body
