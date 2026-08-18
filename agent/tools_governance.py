"""
agent/tools_governance.py — the Governance Agent's entire toolset.

Reads ONLY the DuckDB snapshot built by catalog_duckdb.py. Metadata only:
no fact values ever leave here (the snapshot has no RLS -- see design §2).

Every query is parameterized. Identifiers are never interpolated. The
connection is opened read-only so a prompt-injected "DROP TABLE" cannot
execute even if it somehow reached the driver.

The SQL is deliberately ANSI-plain so it runs on both DuckDB (production)
and sqlite3 (tests) -- which is how these queries are verified without a
DuckDB install.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

MAX_ROWS = 200          # tool results are LLM context, not a data export
SNIPPET = 4000


class Conn(Protocol):
    def execute(self, sql: str, params: Any = ...) -> Any: ...


@dataclass
class ToolResult:
    tool: str
    rows: List[Dict[str, Any]]
    truncated: bool = False
    build_id: Optional[str] = None
    as_of_utc: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps({
            "tool": self.tool, "row_count": len(self.rows),
            "truncated": self.truncated, "as_of_utc": self.as_of_utc,
            "rows": self.rows,
        }, ensure_ascii=False, default=str)[:SNIPPET * 4]


def _fetch(conn: Conn, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    cur = conn.execute(sql, params)
    desc = cur.description if cur.description else []
    cols = [d[0] for d in desc]
    out = []
    for r in cur.fetchall()[: MAX_ROWS + 1]:
        out.append(dict(zip(cols, r)))
    return out


def _wrap(conn: Conn, tool: str, sql: str, params: tuple = ()) -> ToolResult:
    rows = _fetch(conn, sql, params)
    truncated = len(rows) > MAX_ROWS
    m = manifest(conn)
    return ToolResult(tool, rows[:MAX_ROWS], truncated,
                      m.get("build_id"), m.get("built_at_utc"))


def manifest(conn: Conn) -> Dict[str, Any]:
    """Freshness + completeness. The agent must surface as_of to the user and
    refuse to answer from a build whose status != 'complete'."""
    try:
        rows = _fetch(conn, "SELECT build_id, built_at_utc, mode, "
                            "schema_version, row_counts, status FROM manifest "
                            "ORDER BY built_at_utc DESC LIMIT 1")
        return rows[0] if rows else {}
    except Exception:
        return {}


def assert_fresh(conn: Conn) -> Dict[str, Any]:
    m = manifest(conn)
    if not m:
        raise RuntimeError("snapshot has no manifest; refusing to answer")
    if m.get("status") != "complete":
        raise RuntimeError(
            f"snapshot build {m.get('build_id')} is '{m.get('status')}'; "
            f"refusing to answer from a partial build"
        )
    return m


# ---------------------------------------------------------------------------
# Tools. Each maps 1:1 to an intent in router.ROUTING_TABLE.
# ---------------------------------------------------------------------------
def find_measure(conn: Conn, name: str,
                 dataset_id: Optional[str] = None) -> ToolResult:
    """MEASURE_DEFINITION — 'how is Net Sales calculated?'
    Also the disambiguation step that pins dataset_id before the Data Agent
    can build valid DAX."""
    sql = (
        "SELECT m.dataset_id, d.name AS dataset_name, d.workspace_name, "
        "       m.table_name, m.measure_name, m.expression, m.description, "
        "       m.format_string, m.is_hidden "
        "FROM measures m LEFT JOIN datasets d ON d.dataset_id = m.dataset_id "
        "WHERE LOWER(m.measure_name) LIKE ? "
    )
    params: tuple = (f"%{(name or '').lower()}%",)
    if dataset_id:
        sql += "AND m.dataset_id = ? "
        params += (dataset_id,)
    sql += "ORDER BY m.is_hidden, LENGTH(m.measure_name) LIMIT 50"
    return _wrap(conn, "find_measure", sql, params)


def model_sources(conn: Conn, dataset_id: str) -> ToolResult:
    """LINEAGE_SOURCES — 'what sources does this model use?'
    Unions declared datasources with the server/SQL parsed out of M
    expressions by the existing expression_parser."""
    sql = (
        "SELECT 'datasource' AS origin, ds.datasource_type AS kind, "
        "       ds.server, ds.database, ds.gateway_id, NULL AS table_name "
        "FROM datasources ds WHERE ds.dataset_id = ? "
        "UNION ALL "
        "SELECT 'table' AS origin, t.source_type_label AS kind, "
        "       t.server_name AS server, NULL AS database, NULL AS gateway_id, "
        "       t.table_name "
        "FROM tables t "
        "WHERE t.dataset_id = ? AND t.server_name IS NOT NULL "
        "ORDER BY origin, kind, server LIMIT 200"
    )
    return _wrap(conn, "model_sources", sql, (dataset_id, dataset_id))


def impact_of(conn: Conn, table_key: str) -> ToolResult:
    """IMPACT_ANALYSIS — 'what breaks if I drop dbo.FactSales?'
    This is impact_builder.py's reverse index, now an indexed lookup instead
    of a 300MB parse on the request thread."""
    sql = (
        "SELECT i.table_key, i.source_type, i.server, i.database, "
        "       i.schema_name, i.physical_table, i.model_table_name, "
        "       i.dataset_id, d.name AS dataset_name, i.workspace_id, "
        "       r.report_id, r.name AS report_name, "
        "       i.report_count, i.dataset_count, i.workspace_count "
        "FROM impact i "
        "LEFT JOIN datasets d ON d.dataset_id = i.dataset_id "
        "LEFT JOIN reports  r ON r.report_id  = i.report_id "
        "WHERE LOWER(i.table_key) LIKE ? OR LOWER(i.physical_table) LIKE ? "
        "ORDER BY i.report_count DESC LIMIT 200"
    )
    k = f"%{(table_key or '').lower()}%"
    return _wrap(conn, "impact_of", sql, (k, k))


def refresh_status(conn: Conn, only_failed: bool = False,
                   dataset_id: Optional[str] = None) -> ToolResult:
    """REFRESH_STATUS — 'which refreshes failed last night?'"""
    sql = (
        "SELECT rf.dataset_id, d.name AS dataset_name, d.workspace_name, "
        "       rf.status, rf.last_refresh_utc, rf.refresh_type, rf.error_code "
        "FROM refresh rf LEFT JOIN datasets d ON d.dataset_id = rf.dataset_id "
        "WHERE 1=1 "
    )
    params: tuple = ()
    if only_failed:
        # bare truth test, not "= 1": DuckDB BOOLEAN won't compare to INTEGER,
        # sqlite has no BOOLEAN. This form is valid on both.
        sql += "AND rf.is_failed "
    if dataset_id:
        sql += "AND rf.dataset_id = ? "
        params += (dataset_id,)
    sql += "ORDER BY rf.last_refresh_utc DESC LIMIT 200"
    return _wrap(conn, "refresh_status", sql, params)


def usage_stats(conn: Conn, workspace_id: Optional[str] = None,
                unused_only: bool = False) -> ToolResult:
    """USAGE_STATS — 'which reports nobody uses?' (decommissioning evidence)"""
    sql = (
        "SELECT r.report_id, r.name AS report_name, r.workspace_name, "
        "       r.dataset_id, u.views, u.distinct_users, u.last_viewed_utc "
        "FROM reports r LEFT JOIN usage u ON u.report_id = r.report_id "
        "WHERE 1=1 "
    )
    params: tuple = ()
    if workspace_id:
        sql += "AND r.workspace_id = ? "
        params += (workspace_id,)
    if unused_only:
        sql += "AND (u.views IS NULL OR u.views = 0) "
    sql += "ORDER BY COALESCE(u.views, 0) DESC LIMIT 200"
    return _wrap(conn, "usage_stats", sql, params)


def find_dataset(conn: Conn, name: str) -> ToolResult:
    """MODEL_INVENTORY / entity resolution — maps a business phrase to a
    concrete dataset_id. Runs BEFORE the Data Agent on every live turn."""
    sql = (
        "SELECT d.dataset_id, d.name, d.workspace_id, d.workspace_name, "
        "       d.configured_by, d.storage_mode, d.table_count, "
        "       d.measure_count, d.has_schema "
        "FROM datasets d WHERE LOWER(d.name) LIKE ? "
        "ORDER BY d.has_schema DESC, d.measure_count DESC LIMIT 50"
    )
    return _wrap(conn, "find_dataset", sql, (f"%{(name or '').lower()}%",))


def model_schema(conn: Conn, dataset_id: str) -> ToolResult:
    """Grounding context for NL->DAX. Feeding this to the generator is what
    fixes most bad DAX -- ungrounded models invent column names."""
    sql = (
        "SELECT t.table_name, c.column_name, c.data_type, c.is_hidden "
        "FROM tables t LEFT JOIN columns c "
        "  ON c.dataset_id = t.dataset_id AND c.table_name = t.table_name "
        "WHERE t.dataset_id = ? AND (NOT t.is_hidden OR t.is_hidden IS NULL) "
        "ORDER BY t.table_name, c.column_name LIMIT 200"
    )
    return _wrap(conn, "model_schema", sql, (dataset_id,))


def relationships(conn: Conn, dataset_id: str) -> ToolResult:
    """Join paths — the other half of DAX grounding."""
    sql = (
        "SELECT from_table, from_column, to_table, to_column, "
        "       cardinality, is_active, cross_filtering "
        "FROM relationships WHERE dataset_id = ? "
        "ORDER BY from_table LIMIT 200"
    )
    return _wrap(conn, "relationships", sql, (dataset_id,))


TOOLS = {
    "find_measure": find_measure,
    "model_sources": model_sources,
    "impact_of": impact_of,
    "refresh_status": refresh_status,
    "usage_stats": usage_stats,
    "find_dataset": find_dataset,
    "model_schema": model_schema,
    "relationships": relationships,
}
