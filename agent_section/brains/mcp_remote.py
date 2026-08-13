"""
Power BI remote MCP server brain.

Gives you Copilot's own DAX generation without Copilot Studio in the middle.
Tools exposed by the server:

    Generate Query              NL -> DAX, same engine as Copilot for Power BI
    Execute Query               runs DAX; needs Build permission; RLS enforced
    Get Semantic Model Schema   tables/columns/measures/relationships + AI metadata
    Get Report Metadata         pages, visuals, field-to-role bindings, filters

TWO THINGS THAT WILL BITE YOU
  1. RLS is NOT enforced under Service Principal auth. The principal sees
     everything it is authorised for. This module therefore refuses to run
     without a delegated user token unless you explicitly opt out by setting
     PBI_MCP_ALLOW_SP=1 -- and you should only do that for a service account
     with no sensitive access.
  2. A tenant admin must enable "Users can use the Power BI Model Context
     Protocol server endpoint (preview)".

Transport is Streamable HTTP. This is deliberately a thin client rather than a
full MCP SDK dependency, so it stays installable in a locked-down App Service.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

DEFAULT_ENDPOINT = "https://api.powerbi.com/v1.0/myorg/mcp"


def _endpoint() -> str:
    return os.getenv("PBI_MCP_ENDPOINT", DEFAULT_ENDPOINT)


def _rpc(method: str, params: Dict[str, Any], token: str,
         timeout: float) -> Dict[str, Any]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(
        _endpoint(), data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")

    # Streamable HTTP may return SSE framing; take the last data: line.
    if raw.lstrip().startswith("event:") or "\ndata:" in raw:
        chunks = [ln[5:].strip() for ln in raw.splitlines()
                  if ln.startswith("data:")]
        raw = chunks[-1] if chunks else "{}"
    out = json.loads(raw)
    if "error" in out:
        raise RuntimeError(f"MCP error: {out['error']}")
    return out.get("result", {})


def call_tool(name: str, arguments: Dict[str, Any], token: str,
              timeout: Optional[float] = None) -> Dict[str, Any]:
    timeout = timeout or float(os.getenv("PBI_MCP_TIMEOUT", "120"))
    return _rpc("tools/call", {"name": name, "arguments": arguments},
                token, timeout)


def list_tools(token: str) -> Dict[str, Any]:
    return _rpc("tools/list", {}, token, 30)


def _text_of(result: Dict[str, Any]) -> str:
    parts = []
    for c in result.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
    return "\n".join(parts).strip()


def ask(question: str, *, user_upn: str, user_token: str,
        dataset_id: Optional[str] = None) -> Dict[str, Any]:
    """Generate DAX from the question, then execute it -- both as the user."""
    if not user_token and os.getenv("PBI_MCP_ALLOW_SP") != "1":
        raise PermissionError(
            "Refusing to query without a delegated user token: RLS is not "
            "enforced for Service Principal auth on the remote MCP server.")
    if not dataset_id:
        raise ValueError("dataset_id is required -- pick a semantic model first")

    warnings = []

    schema = {}
    try:
        schema = call_tool("Get Semantic Model Schema",
                           {"semanticModelId": dataset_id}, user_token)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"schema fetch failed: {exc}")

    dax = None
    if os.getenv("PBI_MCP_USE_GENERATE_QUERY", "1") == "1":
        # Consumes Copilot capacity and needs a Copilot licence. Set the env
        # var to 0 to have your own LLM write the DAX instead.
        try:
            gen = call_tool("Generate Query", {
                "semanticModelId": dataset_id,
                "prompt": question,
                "schemaContext": _text_of(schema)[:20000] if schema else "",
            }, user_token)
            dax = _text_of(gen) or None
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Generate Query failed: {exc}")

    rows: list = []
    answer_text = ""
    if dax:
        try:
            res = call_tool("Execute Query",
                            {"semanticModelId": dataset_id, "daxQuery": dax},
                            user_token)
            answer_text = _text_of(res)
            payload = res.get("structuredContent") or {}
            if isinstance(payload, dict):
                rows = payload.get("rows") or payload.get("results") or []
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Execute Query failed: {exc}")
    else:
        warnings.append("no DAX was generated")

    return {
        "brain": "mcp",
        "plane": "live",
        "answer": answer_text or "The query did not return a readable result.",
        "dax": dax,
        "rows": rows[:50] if isinstance(rows, list) else [],
        "row_count": len(rows) if isinstance(rows, list) else 0,
        "citations": [], "requires_approval": False, "plan": None,
        "warnings": warnings,
    }
