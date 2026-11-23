"""Regression tests: the MCP sidecar must never load heavy catalog blobs.

Incident (2026-11-23): team-scale MCP usage had the sidecar parse the
~284 MB workspace_catalog.json concurrently with the webapp's own heavy
loads — the container was OOM-killed and production flapped. Guards:

  1. get_workspace_reports serves from the thin report directory and the
     stub RAISES if anything tries to load workspace_catalog.json.
  2. Heavy index tools (get_unused_fields ~102 MB, get_field_usage ~54 MB) — P0 2026-11-23 also gates lookup_table_impact, get_table_impact_detail, get_report_sources and get_model_details
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

    def __init__(self):
        # Records the allow_catalog_fallback value passed by the MCP tool.
        self.directory_fallback_flags = []

    def get_report_directory_rows(self, allowed_workspace_ids, allow_catalog_fallback=True):
        self.directory_fallback_flags.append(allow_catalog_fallback)
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

    # --- newly gated impact/model tools (P0) -------------------------------
    def lookup_table(self, table_name):
        return [{"table": table_name, "datasets": []}]

    def impact_table_detail(self, table_key):
        return {"tableKey": table_key, "datasets": []}

    def impact_report_detail(self, report_id, allowed_workspace_ids=None):
        return {"reportId": report_id, "sources": []}

    def impact_model_details(self, dataset_id="", workspace_id=""):
        return {"datasetId": dataset_id, "tables": []}

    def search_reports(self, query="", allowed_workspace_ids=None, limit=50,
                       allow_catalog_fallback=True):
        self.directory_fallback_flags.append(allow_catalog_fallback)
        if not (query or "").strip():
            return {"success": True, "query": query, "count": 0, "total": 0, "rows": []}
        rows = [r for r in _ROWS if query.lower() in (r.get("reportName") or "").lower()]
        if allowed_workspace_ids is not None:
            rows = [r for r in rows if r.get("workspaceId") in allowed_workspace_ids]
        return {"success": True, "query": query, "count": len(rows),
                "total": len(rows), "rows": rows[: max(1, limit)]}


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


def test_heavy_gate_gates_impact_and_model_tools():
    """P0: the four impact/model tools parse multi-hundred-MB indexes and
    must serialize through _heavy_call like the fields tools."""
    assert server._HEAVY_GATE.acquire(blocking=False)
    try:
        calls = (
            lambda: server.lookup_table_impact("t"),
            lambda: server.get_table_impact_detail("k"),
            lambda: server.get_report_sources("r1"),
            lambda: server.get_model_details("d1"),
        )
        for call in calls:
            out = call()
            assert out["success"] is False and out.get("busy") is True, out
    finally:
        server._HEAVY_GATE.release()
    # Gate free -> all four succeed against the stub.
    assert server.lookup_table_impact("t")["success"] is True
    assert server.get_table_impact_detail("k")["success"] is True
    assert server.get_report_sources("r1")["success"] is True
    assert server.get_model_details("d1")["success"] is True


def test_workspace_reports_never_falls_back_to_catalog():
    """P0: the MCP tool must pass allow_catalog_fallback=False so a missing
    thin pack returns [] instead of triggering a heavy catalog rebuild."""
    out = server.get_workspace_reports("ws-1")
    assert out["success"] is True
    stub = server._svc_singleton
    assert stub.directory_fallback_flags, "expected at least one recorded flag"
    assert all(f is False for f in stub.directory_fallback_flags), (
        stub.directory_fallback_flags
    )


def test_search_reports_never_falls_back_to_catalog():
    """P1 re-review 2026-11-23: search_reports had the same heavy-rebuild
    hole as get_workspace_reports; the MCP tool must pass False too."""
    out = server.search_reports("alpha")
    assert out["success"] is True and out["data"]["count"] == 1, out
    stub = server._svc_singleton
    assert stub.directory_fallback_flags, "expected at least one recorded flag"
    assert all(f is False for f in stub.directory_fallback_flags), (
        stub.directory_fallback_flags
    )


if __name__ == "__main__":
    _setup()
    try:
        test_thin_workspace_reports()
        test_thin_reports_other_workspace_filtered_out()
        test_thin_reports_acl_denial()
        test_thin_reports_unknown_or_empty_workspace()
        test_heavy_gate_serializes()
        test_heavy_gate_gates_impact_and_model_tools()
        test_workspace_reports_never_falls_back_to_catalog()
        test_search_reports_never_falls_back_to_catalog()
    finally:
        _teardown()
    print("All tests passed.")
