"""Regression tests: the MCP sidecar must never load heavy catalog blobs.

Incident (2026-11-23): team-scale MCP usage had the sidecar parse the
~284 MB workspace_catalog.json concurrently with the webapp's own heavy
loads — the container was OOM-killed and production flapped. Guards:

  1. get_workspace_reports serves from the thin report directory and the
     stub RAISES if anything tries to load workspace_catalog.json.
  2. Heavy index tools (get_unused_fields ~102 MB, get_field_usage ~54 MB)
     pass through _heavy_call: concurrent callers get a friendly busy
     response instead of a second simultaneous parse.

Run standalone:  python tests/test_mcp_thin_workspace_reports.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import mcp_server.server as server  # noqa: E402

_ROWS = [
    {"reportId": "r2", "reportName": "Beta Report", "workspaceId": "ws-1",
     "workspaceName": "WS One", "datasetId": "d2"},
    {"reportId": "r1", "reportName": "Alpha Report", "workspaceId": "ws-1",
     "workspaceName": "WS One", "datasetId": "d1"},
    {"reportId": "r3", "reportName": "Other WS Report", "workspaceId": "ws-2",
     "workspaceName": "WS Two", "datasetId": "d3"},
]


class _ThinStub:
    """Thin-pack-backed service. Any heavy-blob access FAILS the test."""

    def get_report_directory_rows(self, allowed_workspace_ids):
        rows = list(_ROWS)
        if allowed_workspace_ids is not None:
            rows = [r for r in rows if r.get("workspaceId") in allowed_workspace_ids]
        return rows

    def get_workspace_catalog(self, force_refresh=False):
        raise AssertionError("sidecar attempted to load the ~284 MB workspace_catalog")

    def unused_fields_rows(self, allowed_workspace_ids):
        return {"rows": [{"fieldKey": "t.m"}], "caveat": "test"}

    def field_usage_detail(self, field_key, allowed_workspace_ids):
        return {"fieldKey": field_key, "usedIn": []}


_ORIG_SVC = server._svc_singleton
_ORIG_ENV = os.environ.get("MCP_ALLOWED_WORKSPACE_IDS")


def _setup():
    server._svc_singleton = _ThinStub()
    os.environ.pop("MCP_ALLOWED_WORKSPACE_IDS", None)


def _teardown():
    server._svc_singleton = _ORIG_SVC
    if _ORIG_ENV is None:
        os.environ.pop("MCP_ALLOWED_WORKSPACE_IDS", None)
    else:
        os.environ["MCP_ALLOWED_WORKSPACE_IDS"] = _ORIG_ENV


def test_thin_workspace_reports():
    out = server.get_workspace_reports("ws-1")
    assert out["success"] is True, out
    assert out["detailLevel"] == "thin"
    assert out["workspace_name"] == "WS One"
    assert out["count"] == 2
    names = [r["name"] for r in out["reports"]]
    assert names == ["Alpha Report", "Beta Report"], names
    ids = [r["id"] for r in out["reports"]]
    assert ids == ["r1", "r2"]
    assert out["reports"][0]["datasetId"] == "d1"
    assert "webapp" in out["detailNote"]


def test_thin_reports_other_workspace_filtered_out():
    out = server.get_workspace_reports("ws-1")
    assert all(r["id"] != "r3" for r in out["reports"])


def test_thin_reports_acl_denial():
    out = server.get_workspace_reports("ws-1", acl_workspace_ids=["ws-9"])
    assert out["success"] is False and out["aclFiltered"] is True


def test_thin_reports_unknown_or_empty_workspace():
    out = server.get_workspace_reports("ws-zzz")
    assert out["success"] is True and out["count"] == 0


def test_heavy_gate_serializes():
    # Hold the gate -> concurrent caller gets a friendly busy response.
    assert server._HEAVY_GATE.acquire(blocking=False)
    try:
        out = server.get_unused_fields(limit=10)
        assert out["success"] is False and out.get("busy") is True, out
        out = server.get_field_usage("t.m")
        assert out["success"] is False and out.get("busy") is True, out
    finally:
        server._HEAVY_GATE.release()
    # Gate free -> calls succeed.
    out = server.get_unused_fields(limit=10)
    assert out["success"] is True and out["data"]["rows"], out
    out = server.get_field_usage("t.m")
    assert out["success"] is True and out["data"]["fieldKey"] == "t.m"


if __name__ == "__main__":
    _setup()
    try:
        test_thin_workspace_reports()
        test_thin_reports_other_workspace_filtered_out()
        test_thin_reports_acl_denial()
        test_thin_reports_unknown_or_empty_workspace()
        test_heavy_gate_serializes()
    finally:
        _teardown()
    print("All tests passed.")
