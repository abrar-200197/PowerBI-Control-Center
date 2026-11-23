"""Unit tests for Phase-2 per-user ACL (offline).

Covers: MCP_USER_TOKENS parsing, token resolution, workspace precedence
(explicit list > global env > tenant-wide), JSON-RPC argument injection,
and the injected list reaching the catalog service call.
Run standalone:  python tests/test_mcp_user_acl.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# UTF-8 stdout/stderr (repo pattern for cp1252 consoles).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import mcp_server.server as server
import mcp_server.user_acl as user_acl

ENV = "MCP_USER_TOKENS"
_ORIG = os.environ.get(ENV)

_MAP = json.dumps({
    "tok-alice": {"upn": "alice@ashleyfurniture.com", "workspaces": ["ws-1", "ws-2"]},
    "tok-bob": {"upn": "bob@ashleyfurniture.com"},
    "tok-empty": {"upn": "empty@ashleyfurniture.com", "workspaces": []},
})


def _restore_env():
    if _ORIG is None:
        os.environ.pop(ENV, None)
    else:
        os.environ[ENV] = _ORIG


def test_parse_user_map():
    m = user_acl.parse_user_map(_MAP)
    assert set(m) == {"tok-alice", "tok-bob", "tok-empty"}
    assert m["tok-alice"]["workspaces"] == ["ws-1", "ws-2"]
    assert m["tok-bob"]["workspaces"] is None
    assert m["tok-empty"]["workspaces"] == []
    assert user_acl.parse_user_map("not json") == {}
    assert user_acl.parse_user_map("") == {}
    assert user_acl.parse_user_map("[1,2]") == {}
    assert user_acl.user_map_configured(_MAP) is True
    assert user_acl.user_map_configured("") is False


def test_resolve_user():
    m = user_acl.parse_user_map(_MAP)
    assert user_acl.resolve_user("tok-alice", _MAP)["upn"] == "alice@ashleyfurniture.com"
    assert user_acl.resolve_user("unknown", _MAP) is None
    assert user_acl.resolve_user("", _MAP) is None
    assert user_acl.resolve_user("  tok-bob  ", _MAP)["upn"] == "bob@ashleyfurniture.com"


def test_effective_workspaces_precedence():
    _global = os.environ.pop("MCP_ALLOWED_WORKSPACE_IDS", None)
    try:
        # Explicit list wins (even empty).
        alice = user_acl.resolve_user("tok-alice", _MAP)
        assert user_acl.effective_workspaces(alice) == {"ws-1", "ws-2"}
        assert user_acl.effective_workspaces(user_acl.resolve_user("tok-empty", _MAP)) == set()
        # Absent -> global env (unset here) -> tenant-wide None.
        bob = user_acl.resolve_user("tok-bob", _MAP)
        assert user_acl.effective_workspaces(bob) is None
        # Absent -> global env (set) -> that list.
        os.environ["MCP_ALLOWED_WORKSPACE_IDS"] = "gw-1"
        assert user_acl.effective_workspaces(bob) == {"gw-1"}
    finally:
        if _global is None:
            os.environ.pop("MCP_ALLOWED_WORKSPACE_IDS", None)
        else:
            os.environ["MCP_ALLOWED_WORKSPACE_IDS"] = _global
        _restore_env()


def test_inject_into_tools_call():
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_workspaces", "arguments": {}},
    }).encode()
    out = json.loads(user_acl.inject_acl_into_body(body, {"ws-9", "ws-3"}))
    assert out["params"]["arguments"]["acl_workspace_ids"] == ["ws-3", "ws-9"]

    other = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode()
    assert user_acl.inject_acl_into_body(other, {"ws-9"}) == other
    assert user_acl.inject_acl_into_body(b"", {"ws-9"}) == b""
    assert user_acl.inject_acl_into_body(b"\xff\xfe-not-json", {"ws-9"}) == b"\xff\xfe-not-json"


def test_tool_uses_injected_list():
    captured = {}

    class _Stub:
        def list_workspaces_for_user(self, allowed_workspace_ids):
            captured["allowed"] = allowed_workspace_ids
            return []

    _orig_svc = server._svc_singleton
    _global = os.environ.pop("MCP_ALLOWED_WORKSPACE_IDS", None)
    try:
        server._svc_singleton = _Stub()
        out = server.list_workspaces(acl_workspace_ids=["ws-x"])
        assert captured["allowed"] == {"ws-x"}, captured
        assert out["aclFiltered"] is True

        server.list_workspaces()
        assert captured["allowed"] is None
    finally:
        server._svc_singleton = _orig_svc
        if _global is None:
            os.environ.pop("MCP_ALLOWED_WORKSPACE_IDS", None)
        else:
            os.environ["MCP_ALLOWED_WORKSPACE_IDS"] = _global
        _restore_env()


if __name__ == "__main__":
    test_parse_user_map()
    test_resolve_user()
    test_effective_workspaces_precedence()
    test_inject_into_tools_call()
    test_tool_uses_injected_list()
    print("All tests passed.")
