"""
Power BI Control Center — MCP server (agent-facing catalog tools).

Wraps the ``catalog_service`` thin query APIs as MCP tools so AI agents
(AshleyCode, Copilot Studio, Claude, ...) can query the precomputed Power BI
catalog: reverse impact analysis, field usage, unused fields, workspace and
report search. Read-only; serves whatever the catalog has published to
SharePoint ``latest/`` (or the local disk mirror in ``data/catalog_cache/``).

Run (stdio transport — the default for local agent use):

    python mcp_server/server.py

Connect from AshleyCode:

    opencode mcp add powerbi-control-center --global \
        --command python --args "<repo>/mcp_server/server.py"
    (exact flags: ``opencode mcp add --help``; or use the ``mcp.servers``
    config block documented in AGENTS.md)

SECURITY: these tools serve tenant-wide catalog data with NO per-user ACL
filtering (the Flask app filters by the signed-in user; this server does not
have a user session). Run it locally over stdio like a dev tool. Do not
expose it over HTTP without adding authentication and ACL filtering first.

Data freshness follows the catalog batch job: workspace/impact data refresh
6-hourly; field-usage and unused-fields indexes are weekly (built on
``--fresh`` runs) and may be absent until the first weekly run completes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load .env BEFORE the catalog import: catalog_config binds ~35 constants from
# os.getenv at import time (same ordering constraint as app.py — AGENTS.md
# gotcha #1). Explicit path so the server works from any working directory.
from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env", override=True)

try:
    from catalog_service import catalog_service as _svc_singleton
    _IMPORT_ERROR = ""
except Exception as _exc:  # surface a diagnosable error through every tool
    _svc_singleton = None
    _IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "powerbi-control-center",
    instructions=(
        "Read-only tools over the Power BI Control Center catalog: tenant-wide "
        "report/metadata inventory, reverse impact analysis (physical table → "
        "model tables → datasets → reports → workspaces), field-usage and "
        "unused-field indexes. Call catalog_status first to check freshness."
    ),
)


def _svc():
    """Return the catalog singleton or raise a diagnosable error."""
    if _svc_singleton is None:
        raise RuntimeError(f"catalog_service import failed: {_IMPORT_ERROR}")
    return _svc_singleton


def _not_found(what: str) -> Dict[str, Any]:
    return {"success": False, "error": f"{what} not found (or catalog unavailable)"}


def _error(exc: Exception) -> Dict[str, Any]:
    return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- status


@mcp.tool()
def catalog_status() -> Dict[str, Any]:
    """Catalog availability and freshness: mode, generatedAt timestamp,
    SharePoint folder, cache TTLs, and which artifacts are on the disk mirror.

    Call this FIRST — other tools are only useful when the catalog is
    available and reasonably fresh."""
    try:
        s = _svc()
        out = s.status()
        out["available"] = s.is_available()
        return {"success": True, "data": out}
    except Exception as exc:
        return _error(exc)


# ---------------------------------------------------------------- discovery


@mcp.tool()
def list_workspaces() -> Dict[str, Any]:
    """All workspaces in the catalog with report/dataset counts.

    Use to find a workspace_id before calling other tools."""
    try:
        rows = _svc().list_workspaces_for_user(None)
        return {"success": True, "count": len(rows), "workspaces": rows}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def search_reports(query: str, limit: int = 20) -> Dict[str, Any]:
    """Search reports by name/id across the tenant (case-insensitive substring).

    Returns reportId, reportName, workspaceId, workspaceName rows.
    Provide a non-empty query — an empty query intentionally returns nothing."""
    try:
        out = _svc().search_reports(query=query, allowed_workspace_ids=None, limit=limit)
        return {"success": True, "data": out}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def get_workspace_reports(workspace_id: str) -> Dict[str, Any]:
    """All reports in one workspace, with dataset binding, last refresh
    status/time, refresh schedule and report type per report."""
    try:
        out = _svc().get_workspace_reports(workspace_id, None)
        if out is None:
            return _not_found(f"Workspace {workspace_id}")
        return {"success": True, "data": out}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def get_home_summary() -> Dict[str, Any]:
    """Tenant-wide KPI summary from the latest ops run (summary.json):
    workspace/report counts, refresh and usage statistics."""
    try:
        out = _svc().get_summary()
        if out is None:
            return _not_found("summary.json")
        return {"success": True, "data": out}
    except Exception as exc:
        return _error(exc)


# ---------------------------------------------------------------- impact


@mcp.tool()
def top_impact_tables(n: int = 15) -> Dict[str, Any]:
    """The n physical source tables with the biggest blast radius
    (most dependent datasets/reports/workspaces), sorted by reportCount."""
    try:
        rows = _svc().top_impact_tables(n=max(1, min(int(n), 100)))
        return {"success": True, "count": len(rows), "tables": rows}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def lookup_table_impact(table_name: str) -> Dict[str, Any]:
    """Reverse impact lookup by physical table (or model table) name,
    case-insensitive: which datasets/reports/workspaces consume it.

    Ideal for "what breaks if we change/drop table X" questions."""
    try:
        rows = _svc().lookup_table(table_name)
        if not rows:
            return _not_found(f"Table {table_name!r}")
        return {"success": True, "count": len(rows), "matches": rows}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def get_table_impact_detail(table_key: str) -> Dict[str, Any]:
    """Full impact entry for one source table (by tableKey or table name):
    model table names, dependent datasets with their reports, impact summary."""
    try:
        out = _svc().impact_table_detail(table_key)
        if out is None:
            return _not_found(f"Table key {table_key!r}")
        return {"success": True, "data": out}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def get_report_sources(report_id: str) -> Dict[str, Any]:
    """Every source used by one report (SQL servers/databases/tables, Excel,
    SharePoint files, model tables) plus report metadata. Accepts a report
    GUID; use search_reports first if you only have a report name."""
    try:
        out = _svc().impact_report_detail(report_id, None)
        if out is None:
            return _not_found(f"Report {report_id}")
        return {"success": True, "data": out}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def get_model_details(
    dataset_id: str, workspace_id: str = ""
) -> Dict[str, Any]:
    """Thin model view of one semantic model/dataset: tables, columns,
    measures, relationships. Optionally pass workspace_id to disambiguate."""
    try:
        out = _svc().impact_model_details(
            dataset_id=dataset_id, workspace_id=workspace_id
        )
        if out is None:
            return _not_found(f"Dataset {dataset_id}")
        return {"success": True, "data": out}
    except Exception as exc:
        return _error(exc)


# ---------------------------------------------------------------- fields


@mcp.tool()
def get_unused_fields(limit: int = 50) -> Dict[str, Any]:
    """Tenant-wide 'truly unused' columns/measures — safe-to-delete
    candidates, weekly-built. Excluded from the list if they are visual-bound,
    DAX-referenced, M-expression-tagged, relationship keys, or referenced by
    another measure. Note the returned 'caveat' (RLS/sort-by undetectable)."""
    try:
        out = _svc().unused_fields_rows(None)
        rows = list(out.get("rows") or [])
        out["rows"] = rows[: max(1, min(int(limit), 500))]
        out["rowsShown"] = len(out["rows"])
        out["rowsTotal"] = len(rows)
        return {"success": True, "data": out}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def get_field_usage(field_key: str) -> Dict[str, Any]:
    """Which reports use one field/metric, given its fieldKey
    (format 'table.column' or 'table.measure' — see the fieldKey values in
    get_unused_fields or the Impact Explorer). Weekly-built index."""
    try:
        out = _svc().field_usage_detail(field_key, None)
        if out is None:
            return _not_found(f"Field {field_key!r}")
        return {"success": True, "data": out}
    except Exception as exc:
        return _error(exc)


# ---------------------------------------------------------------- entry


def main() -> None:
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
