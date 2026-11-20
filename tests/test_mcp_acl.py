"""Unit tests for the MCP server's ACL allow-list (offline).

Covers:
  - MCP_ALLOWED_WORKSPACE_IDS parsing (unset / '*' / 'all' / CSV)
  - pass-through of the allow-list into catalog_service calls
  - aclFiltered transparency flag

Run standalone:  python tests/test_mcp_acl.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# UTF-8 stdout/stderr so emoji prints in the code under test never crash on
# Windows cp1252 consoles (same reconfigure app.py performs at startup).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import mcp_server.server as server

_ENV = "MCP_ALLOWED_WORKSPACE_IDS"
_ORIG = os.environ.get(_ENV)


def _restore():
    if _ORIG is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = _ORIG


def test_unset_means_tenant_wide():
    os.environ.pop(_ENV, None)
    try:
        assert server._allowed_workspace_ids() is None
    finally:
        _restore()


def test_all_and_star_mean_tenant_wide():
    try:
        os.environ[_ENV] = "all"
        assert server._allowed_workspace_ids() is None
        os.environ[_ENV] = "*"
        assert server._allowed_workspace_ids() is None
    finally:
        _restore()


def test_csv_parsed_to_set():
    try:
        os.environ[_ENV] = " ws-a , ws-b ,,\tws-c "
        assert server._allowed_workspace_ids() == {"ws-a", "ws-b", "ws-c"}
    finally:
        _restore()


def test_allowlist_passed_to_service():
    captured = {}

    class _StubService:
        def list_workspaces_for_user(self, allowed_workspace_ids):
            captured["allowed"] = allowed_workspace_ids
            return [{"id": "ws-a", "name": "A"}, {"id": "ws-b", "name": "B"}]

    _orig = server._svc_singleton
    try:
        server._svc_singleton = _StubService()
        os.environ[_ENV] = "ws-a"
        out = server.list_workspaces()
        # Pass-through is what this test proves; the stub does not filter.
        assert captured["allowed"] == {"ws-a"}, captured
        assert out["success"] is True and out["aclFiltered"] is True

        os.environ[_ENV] = "all"
        out = server.list_workspaces()
        assert captured["allowed"] is None
        assert out["aclFiltered"] is False
    finally:
        server._svc_singleton = _orig
        _restore()


if __name__ == "__main__":
    test_unset_means_tenant_wide()
    test_all_and_star_mean_tenant_wide()
    test_csv_parsed_to_set()
    test_allowlist_passed_to_service()
    print("All tests passed.")
