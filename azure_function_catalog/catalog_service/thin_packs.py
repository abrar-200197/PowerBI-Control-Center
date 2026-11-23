"""
Precompute thin UI JSON packs from full catalog artifacts.

Browser and Home never need 300MB workspace_catalog / 40MB impact_index.
Extract writes these next to latest/ and publishes to SharePoint.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Match Report Catalog / ops usage window (Activity Events ViewReport)
USAGE_LOOKBACK_DAYS = int(os.getenv("USAGE_LOOKBACK_DAYS", "60"))

# Microsoft Power BI platform usage reports — not business content.
# Exact names only (case-insensitive), per product decision.
_EXCLUDED_REPORT_NAMES = frozenset({
    "usage metrics report",
    "report usage metrics report",
    "dashboard usage metrics report",
})


def is_excluded_report_name(name: Optional[str]) -> bool:
    """
    True for platform reports we never show in Catalog / Home / Decommission / Impact.
    Currently:
      - Usage Metrics Report
      - Report Usage Metrics Report
      - Dashboard Usage Metrics Report
    Also skips published app shells ([App] …).
    """
    n = (name or "").strip()
    if not n:
        return False
    if n.startswith("[App]"):
        return True
    return n.casefold() in _EXCLUDED_REPORT_NAMES


def _parse_days(iso_ts: Optional[str], now: datetime) -> Optional[int]:
    if not iso_ts:
        return None
    try:
        s = str(iso_ts)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (now - dt).days)
    except Exception:
        return None


def _is_live(report: Dict[str, Any], ds: Dict[str, Any]) -> bool:
    rtype = str(report.get("refresh_type") or ds.get("refresh_type") or "").lower()
    status = str(report.get("last_refresh_status") or ds.get("last_refresh_status") or "").lower()
    schedule = str(report.get("refresh_schedule") or ds.get("refresh_schedule") or "").lower()
    note = str(report.get("refresh_note") or ds.get("refresh_note") or "").lower()
    return (
        rtype in ("directquery", "live", "push", "streaming")
        or "directquery" in status or "live" in status
        or "directquery" in schedule or "live" in schedule
        or "live connection" in note
    )


def _is_inactive(report: Dict[str, Any], ds: Dict[str, Any], inactive_days: int, now: datetime) -> bool:
    if _is_live(report, ds):
        return False
    days = report.get("days_since_refresh")
    if days is None:
        days = ds.get("days_since_refresh")
    if days is None:
        days = _parse_days(report.get("last_refreshed") or ds.get("last_refreshed"), now)
    if days is None:
        return False
    try:
        return int(days) >= inactive_days
    except Exception:
        return False


def _is_orphaned(report: Dict[str, Any]) -> bool:
    creator = (report.get("createdBy") or report.get("created_by") or "").strip()
    modifier = (report.get("modifiedBy") or report.get("modified_by") or "").strip()
    has_owner = any(
        k in report
        for k in ("createdBy", "created_by", "modifiedBy", "modified_by", "configuredBy", "dataset_owner")
    )
    if not has_owner:
        return False
    owner = (report.get("configuredBy") or report.get("dataset_owner") or "").strip()
    return not creator and not modifier and not owner


def _is_zero_views(report: Dict[str, Any]) -> bool:
    """
    True when usage ops attached an explicit view_count and it is 0.
    None/missing view_count means usage unknown — do NOT count as zero-views.
    """
    vc = report.get("view_count")
    if vc is None and report.get("views") is not None:
        vc = report.get("views")
    if vc is None:
        return False
    try:
        return int(vc) == 0
    except Exception:
        return False


def _is_refresh_failed(report: Dict[str, Any], ds: Optional[Dict[str, Any]] = None) -> bool:
    """
    True when last refresh status is Failed (report or dataset ops).
    Matches Report Catalog badge normalization (failed / failure).
    """
    ds = ds or {}
    status = str(
        report.get("last_refresh_status")
        or ds.get("last_refresh_status")
        or report.get("refreshStatus")
        or ""
    ).strip().lower()
    if not status:
        return False
    return status in ("failed", "failure") or status.startswith("fail")


def _is_refresh_completed(report: Dict[str, Any], ds: Optional[Dict[str, Any]] = None) -> bool:
    """
    True only when last refresh status indicates a successful completion
    (e.g. Completed / Success / Succeeded). Excludes Failed, Error,
    Unverified, No History, Access Denied, Not Refreshable, etc.
    """
    ds = ds or {}
    status = str(
        report.get("last_refresh_status")
        or ds.get("last_refresh_status")
        or ""
    ).strip().lower()
    return status in ("completed", "success", "succeeded")


def _long_running_duration(report: Dict[str, Any], ds: Dict[str, Any]) -> Optional[float]:
    """Dataset-level refresh duration (seconds), preferring report-level value."""
    dur = report.get("refresh_duration_seconds")
    if dur is None:
        dur = ds.get("refresh_duration_seconds")
    if dur is None:
        return None
    try:
        dur = float(dur)
    except Exception:
        return None
    return dur if dur >= 0 else None


def _detail_row(
    r: Dict[str, Any],
    wid: str,
    wname: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    row = {
        "reportId": r.get("id"),
        "reportName": r.get("name") or "",
        "workspaceId": wid,
        "workspaceName": wname,
        "datasetId": r.get("datasetId") or "",
    }
    if extra:
        row.update(extra)
    return row


def build_ui_home_index(catalog: Dict[str, Any], inactive_days: int = 30) -> Dict[str, Any]:
    """
    Per-workspace counts + Home KPI detail lists (report tabs).

    Counts alone are ~200 KB; detail lists stay small (id/name/ws only) so Home
    never needs the full ~300MB catalog for KPI drill-downs.
    """
    datasets_map = catalog.get("datasets") or {}
    now = datetime.now(timezone.utc)
    workspaces: List[Dict[str, Any]] = []
    reports_rows: List[Dict[str, Any]] = []
    inactive_rows: List[Dict[str, Any]] = []
    orphaned_rows: List[Dict[str, Any]] = []
    zero_views_rows: List[Dict[str, Any]] = []
    failed_refresh_rows: List[Dict[str, Any]] = []
    long_running_by_dataset: Dict[str, Dict[str, Any]] = {}
    total_r = total_i = total_o = total_a = total_zv = total_fr = 0
    for ws in catalog.get("workspaces") or []:
        wid = ws.get("id")
        if not wid:
            continue
        wname = ws.get("name") or ""
        rc = ic = oc = zc = fc = 0
        for r in ws.get("reports") or []:
            if is_excluded_report_name(r.get("name")):
                continue
            ds = datasets_map.get(r.get("datasetId") or "") or {}
            rc += 1
            reports_rows.append(_detail_row(r, wid, wname))
            if _is_inactive(r, ds, inactive_days, now):
                ic += 1
                days = r.get("days_since_refresh")
                if days is None:
                    days = ds.get("days_since_refresh")
                if days is None:
                    days = _parse_days(r.get("last_refreshed") or ds.get("last_refreshed"), now)
                inactive_rows.append(_detail_row(r, wid, wname, {
                    "lastRefreshed": r.get("last_refreshed") or ds.get("last_refreshed"),
                    "daysSinceRefresh": days,
                    "refreshStatus": r.get("last_refresh_status") or ds.get("last_refresh_status"),
                }))
            if _is_orphaned(r):
                oc += 1
                orphaned_rows.append(_detail_row(r, wid, wname))
            if _is_zero_views(r):
                zc += 1
                zero_views_rows.append(_detail_row(r, wid, wname, {
                    "viewCount": int(r.get("view_count") or r.get("views") or 0),
                    "lastViewed": r.get("last_viewed"),
                }))
            if _is_refresh_failed(r, ds):
                fc += 1
                failed_refresh_rows.append(_detail_row(r, wid, wname, {
                    "lastRefreshed": r.get("last_refreshed") or ds.get("last_refreshed"),
                    "refreshStatus": r.get("last_refresh_status") or ds.get("last_refresh_status") or "Failed",
                    "refreshNote": r.get("refresh_note") or ds.get("refresh_note") or "",
                }))
            ds_id = r.get("datasetId") or ""
            if ds_id and not _is_live(r, ds) and _is_refresh_completed(r, ds):
                dur = _long_running_duration(r, ds)
                if dur is not None:
                    existing = long_running_by_dataset.get(ds_id)
                    if existing is None or dur > (existing.get("durationSeconds") or 0):
                        long_running_by_dataset[ds_id] = _detail_row(r, wid, wname, {
                            "durationSeconds": dur,
                            "lastRefreshed": r.get("last_refreshed") or ds.get("last_refreshed"),
                            "refreshStartTime": r.get("refresh_start_time") or ds.get("refresh_start_time"),
                            "refreshEndTime": r.get("refresh_end_time") or ds.get("refresh_end_time"),
                            "refreshStatus": r.get("last_refresh_status") or ds.get("last_refresh_status"),
                        })
        ac = max(0, rc - ic)
        total_r += rc
        total_i += ic
        total_o += oc
        total_a += ac
        total_zv += zc
        total_fr += fc
        workspaces.append({
            "id": wid,
            "name": wname,
            "reportCount": rc,
            "inactiveCount": ic,
            "activeCount": ac,
            "orphanedCount": oc,
            "zeroViewsCount": zc,
            "failedRefreshCount": fc,
        })
    workspaces.sort(key=lambda w: (w.get("name") or "").lower())

    def _sort_ws_name(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows.sort(key=lambda r: (
            (r.get("workspaceName") or "").lower(),
            (r.get("reportName") or "").lower(),
        ))
        return rows

    def _sort_inactive(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        def key(row):
            try:
                d = -int(row.get("daysSinceRefresh"))
            except Exception:
                d = 0
            return (d, (row.get("reportName") or "").lower())
        rows.sort(key=key)
        return rows

    # Cap at top 100 *per workspace* (not top 100 overall) so the Home UI can
    # offer a workspace filter: "no workspace selected" shows the combined
    # top 100 (which can never draw more than 100 rows from any one
    # workspace anyway), while "workspace selected" shows that workspace's
    # own top 100 — both cases are satisfied by this same flattened list.
    _long_running_by_ws: Dict[str, List[Dict[str, Any]]] = {}
    for row in long_running_by_dataset.values():
        _long_running_by_ws.setdefault(row.get("workspaceId") or "", []).append(row)
    long_running_rows: List[Dict[str, Any]] = []
    for _ws_rows in _long_running_by_ws.values():
        _ws_rows.sort(key=lambda r: -(r.get("durationSeconds") or 0))
        long_running_rows.extend(_ws_rows[:100])
    long_running_rows.sort(key=lambda r: -(r.get("durationSeconds") or 0))

    return {
        "generatedAt": catalog.get("generatedAt") or datetime.now(timezone.utc).isoformat(),
        "opsEnrichedAt": catalog.get("opsEnrichedAt"),
        "inactiveDaysThreshold": inactive_days,
        "usageLookbackDays": USAGE_LOOKBACK_DAYS,
        "workspaceCount": len(workspaces),
        "totalReports": total_r,
        "activeReports": total_a,
        "inactiveReports": total_i,
        "orphanedReports": total_o,
        "zeroViewsReports": total_zv,
        "failedRefreshReports": total_fr,
        "workspaces": workspaces,
        # KPI tab detail lists (ACL-filter on the server)
        "detailLists": {
            "reports": _sort_ws_name(reports_rows),
            "inactive": _sort_inactive(inactive_rows),
            "orphaned": _sort_ws_name(orphaned_rows),
            "zero_views": _sort_ws_name(zero_views_rows),
            "failed_refresh": _sort_ws_name(failed_refresh_rows),
            "long_running": long_running_rows,
        },
    }


def build_ui_impact_tables(impact_index: Dict[str, Any]) -> Dict[str, Any]:
    """Flat table rows without nested datasets/reports — grid only."""
    rows: List[Dict[str, Any]] = []
    for key, entry in (impact_index.get("tables") or {}).items():
        s = entry.get("impactSummary") or {}
        model_names = entry.get("modelTableNames") or []
        rows.append({
            "tableKey": entry.get("tableKey") or key,
            "table": entry.get("table"),
            "server": entry.get("server") or "",
            "database": entry.get("database") or "",
            "schema": entry.get("schema") or "",
            "sourceType": entry.get("sourceType") or "Unknown",
            "modelTableNames": model_names,
            "reportCount": s.get("reportCount", 0),
            "datasetCount": s.get("datasetCount", 0),
            "workspaceCount": s.get("workspaceCount", 0),
            "searchText": " ".join([
                str(entry.get("table") or ""),
                str(entry.get("tableKey") or key),
                str(entry.get("server") or ""),
                str(entry.get("database") or ""),
                str(entry.get("schema") or ""),
                str(entry.get("sourceType") or ""),
                *map(str, model_names),
            ]).lower(),
        })
    rows.sort(key=lambda r: (-(r.get("reportCount") or 0), r.get("tableKey") or ""))
    return {
        "generatedAt": impact_index.get("generatedAt") or datetime.now(timezone.utc).isoformat(),
        "tableCount": len(rows),
        "rows": rows,
    }


def build_ui_impact_reports(impact_index: Dict[str, Any]) -> Dict[str, Any]:
    """
    Invert impact_index: report → all source tables / files / connections.

    Includes every source type present in the index (Sql, Excel, SharePoint,
    ModelTable, etc.). Grid rows stay light; detailsByReportId holds full
    source lists for the drawer (server-only — not sent as the full pack).
    """
    # reportId -> mutable aggregate
    by_report: Dict[str, Dict[str, Any]] = {}

    for key, entry in (impact_index.get("tables") or {}).items():
        if not isinstance(entry, dict):
            continue
        table_key = entry.get("tableKey") or key
        source_type = entry.get("sourceType") or "Unknown"
        server = entry.get("server") or ""
        database = entry.get("database") or ""
        schema = entry.get("schema") or ""
        table_name = entry.get("table") or ""
        model_names_all = entry.get("modelTableNames") or []

        for ds in entry.get("datasets") or []:
            if not isinstance(ds, dict):
                continue
            ds_id = ds.get("datasetId") or ""
            ds_name = ds.get("datasetName") or ""
            model_table = ds.get("modelTableName") or ""
            for rep in ds.get("reports") or []:
                if not isinstance(rep, dict):
                    continue
                if is_excluded_report_name(rep.get("reportName")):
                    continue
                rid = rep.get("reportId")
                if not rid:
                    continue
                if rid not in by_report:
                    by_report[rid] = {
                        "reportId": rid,
                        "reportName": rep.get("reportName") or "",
                        "workspaceId": rep.get("workspaceId") or ds.get("workspaceId") or "",
                        "workspaceName": rep.get("workspaceName") or ds.get("workspaceName") or "",
                        "reportType": rep.get("reportType") or "",
                        "datasetIds": set(),
                        "sourceTypes": set(),
                        # tableKey -> source stub (merge datasets/model tables)
                        "sources": {},
                    }
                agg = by_report[rid]
                # Prefer non-empty names if we saw empties first
                if not agg.get("reportName") and rep.get("reportName"):
                    agg["reportName"] = rep.get("reportName")
                if not agg.get("workspaceName") and (rep.get("workspaceName") or ds.get("workspaceName")):
                    agg["workspaceName"] = rep.get("workspaceName") or ds.get("workspaceName") or ""
                if not agg.get("workspaceId") and (rep.get("workspaceId") or ds.get("workspaceId")):
                    agg["workspaceId"] = rep.get("workspaceId") or ds.get("workspaceId") or ""
                if ds_id:
                    agg["datasetIds"].add(ds_id)
                if source_type:
                    agg["sourceTypes"].add(str(source_type))

                src = agg["sources"].get(table_key)
                if src is None:
                    src = {
                        "tableKey": table_key,
                        "table": table_name,
                        "sourceType": source_type,
                        "server": server,
                        "database": database,
                        "schema": schema,
                        "modelTableNames": set(),
                        "datasets": [],
                    }
                    agg["sources"][table_key] = src
                if model_table:
                    src["modelTableNames"].add(model_table)
                for mn in model_names_all:
                    if mn:
                        src["modelTableNames"].add(mn)
                # Enrich connection fields if missing
                for fld, val in (("server", server), ("database", database), ("schema", schema)):
                    if not src.get(fld) and val:
                        src[fld] = val
                # Track dataset link (unique)
                existing_ds = {d.get("datasetId") for d in src["datasets"] if d.get("datasetId")}
                if ds_id and ds_id not in existing_ds:
                    src["datasets"].append({
                        "datasetId": ds_id,
                        "datasetName": ds_name,
                        "workspaceId": ds.get("workspaceId") or agg.get("workspaceId") or "",
                        "workspaceName": ds.get("workspaceName") or agg.get("workspaceName") or "",
                        "modelTableName": model_table or "",
                    })

    rows: List[Dict[str, Any]] = []
    details: Dict[str, List[Dict[str, Any]]] = {}

    for rid, agg in by_report.items():
        source_list: List[Dict[str, Any]] = []
        for _tk, src in agg["sources"].items():
            model_names = sorted(src["modelTableNames"]) if isinstance(src["modelTableNames"], set) else list(src.get("modelTableNames") or [])
            source_list.append({
                "tableKey": src.get("tableKey"),
                "table": src.get("table") or "",
                "sourceType": src.get("sourceType") or "Unknown",
                "server": src.get("server") or "",
                "database": src.get("database") or "",
                "schema": src.get("schema") or "",
                "modelTableNames": model_names,
                "datasets": src.get("datasets") or [],
            })
        # Prefer physical/sql-like first, then by name
        def _src_sort(s: Dict[str, Any]):
            st = str(s.get("sourceType") or "").lower()
            phys = 0 if st not in ("modeltable", "unknown", "") else 1
            return (phys, (s.get("table") or "").lower(), s.get("tableKey") or "")
        source_list.sort(key=_src_sort)

        source_types = sorted(agg["sourceTypes"])
        ds_ids = sorted(agg["datasetIds"])
        rname = agg.get("reportName") or ""
        wname = agg.get("workspaceName") or ""
        rows.append({
            "reportId": rid,
            "reportName": rname,
            "workspaceId": agg.get("workspaceId") or "",
            "workspaceName": wname,
            "reportType": agg.get("reportType") or "",
            "tableCount": len(source_list),
            "datasetCount": len(ds_ids),
            "sourceTypes": source_types,
            "searchText": " ".join([
                rname,
                wname,
                rid,
                agg.get("workspaceId") or "",
                *source_types,
                *[str(s.get("table") or "") for s in source_list[:40]],
            ]).lower(),
        })
        details[rid] = source_list

    rows.sort(key=lambda r: (
        (r.get("workspaceName") or "").lower(),
        (r.get("reportName") or "").lower(),
    ))

    return {
        "generatedAt": impact_index.get("generatedAt") or datetime.now(timezone.utc).isoformat(),
        "schemaVersion": "1.0",
        "reportCount": len(rows),
        "rows": rows,
        # Server drawer only — strip before any browser pack delivery if ever exposed
        "detailsByReportId": details,
    }


def build_ui_field_index(field_usage_index: Dict[str, Any]) -> Dict[str, Any]:
    """
    Thin grid + drawer pack for the Impact Explorer "Field usage" tab.

    Flat searchable rows (field/table/type/reportCount) plus a server-side
    detailsByField map with the full report list per field for the drawer.
    """
    rows: List[Dict[str, Any]] = []
    details: Dict[str, List[Dict[str, Any]]] = {}
    for key, entry in (field_usage_index.get("fields") or {}).items():
        if not isinstance(entry, dict):
            continue
        table = entry.get("table") or ""
        field = entry.get("field") or ""
        rows.append({
            "fieldKey": key,
            "field": field,
            "table": table,
            "type": entry.get("type") or "column",
            "reportCount": entry.get("reportCount") or 0,
            "workspaceCount": entry.get("workspaceCount") or 0,
            "searchText": f"{field} {table} {key}".lower(),
        })
        details[key] = entry.get("reports") or []
    rows.sort(key=lambda r: (-(r.get("reportCount") or 0), (r.get("field") or "").lower()))
    return {
        "generatedAt": field_usage_index.get("generatedAt") or datetime.now(timezone.utc).isoformat(),
        "schemaVersion": "1.0",
        "fieldCount": len(rows),
        "reportsProcessed": field_usage_index.get("reportsProcessed") or 0,
        "reportsSkippedUnchanged": field_usage_index.get("reportsSkippedUnchanged") or 0,
        "reportsFailed": field_usage_index.get("reportsFailed") or 0,
        "rows": rows,
        "detailsByField": details,
    }


def build_ui_report_directory(catalog: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flat report directory for reverse search: report name → workspace(s).

    One row per report (not [App] shells). Used by Report Catalog
    "Find report…" without loading the full catalog in the browser.
    """
    rows: List[Dict[str, Any]] = []
    for ws in catalog.get("workspaces") or []:
        wid = ws.get("id")
        if not wid:
            continue
        wname = ws.get("name") or ""
        for r in ws.get("reports") or []:
            rname = str(r.get("name") or "")
            if not rname or is_excluded_report_name(rname):
                continue
            rid = r.get("id")
            if not rid:
                continue
            rows.append({
                "reportId": rid,
                "reportName": rname,
                "workspaceId": wid,
                "workspaceName": wname,
                "datasetId": r.get("datasetId") or "",
                "searchText": f"{rname} {wname} {rid} {wid}".lower(),
            })
    rows.sort(key=lambda r: (
        (r.get("reportName") or "").lower(),
        (r.get("workspaceName") or "").lower(),
    ))
    return {
        "generatedAt": catalog.get("generatedAt") or datetime.now(timezone.utc).isoformat(),
        "opsEnrichedAt": catalog.get("opsEnrichedAt"),
        "reportCount": len(rows),
        "rows": rows,
    }


def build_ui_unused_fields_index(unused_index: Dict[str, Any]) -> Dict[str, Any]:
    """
    Thin grid pack for the Impact Explorer "Unused Columns & Measures" tab
    (Level 1 — tenant-wide, safe-to-review-for-removal list).

    Flat searchable rows; no drawer/details map needed since each row already
    carries its full identity (workspace/dataset/table/field).
    """
    rows: List[Dict[str, Any]] = []
    for item in unused_index.get("items") or []:
        if not isinstance(item, dict):
            continue
        rows.append({
            "workspaceId": item.get("workspaceId") or "",
            "workspaceName": item.get("workspaceName") or "",
            "datasetId": item.get("datasetId") or "",
            "datasetName": item.get("datasetName") or "",
            "table": item.get("table") or "",
            "field": item.get("field") or "",
            "type": item.get("type") or "column",
            "isHidden": bool(item.get("isHidden")),
            "searchText": " ".join([
                str(item.get("field") or ""), str(item.get("table") or ""),
                str(item.get("datasetName") or ""), str(item.get("workspaceName") or ""),
            ]).lower(),
        })
    rows.sort(key=lambda r: (
        (r.get("workspaceName") or "").lower(),
        (r.get("datasetName") or "").lower(),
        (r.get("table") or "").lower(),
        (r.get("field") or "").lower(),
    ))
    return {
        "generatedAt": unused_index.get("generatedAt") or datetime.now(timezone.utc).isoformat(),
        "schemaVersion": "1.0",
        "caveat": unused_index.get("caveat") or "",
        "totalColumns": unused_index.get("totalColumns") or 0,
        "totalMeasures": unused_index.get("totalMeasures") or 0,
        "unusedCount": len(rows),
        "excludedViaReportUsage": unused_index.get("excludedViaReportUsage") or 0,
        "excludedViaDaxOrMExpression": unused_index.get("excludedViaDaxOrMExpression") or 0,
        "excludedViaRelationshipKey": unused_index.get("excludedViaRelationshipKey") or 0,
        "rows": rows,
    }


def _clean_person(*vals: Any) -> str:
    for v in vals:
        if v is None:
            continue
        if isinstance(v, dict):
            v = v.get("userPrincipalName") or v.get("emailAddress") or v.get("displayName") or ""
        s = str(v).strip()
        if s and s.lower() not in {"unknown", "n/a", "none", "null", "-", "—"}:
            return s
    return ""


def build_ui_decommission_candidates(
    catalog: Dict[str, Any],
    refresh_stale_days: int = 90,
) -> Dict[str, Any]:
    """
    Tenant-wide precomputed Report Catalog "Decommission list" candidates.

    Mirrors the live per-workspace rules in app.py
    (export_inactive_reports / decommission_list_candidates):
      1) days_since_refresh > refresh_stale_days
      2) no import refresh history (not Live/DirectQuery)
      3) 0 views in the usage lookback window
      4) needs verification (unverified/estimated refresh status)

    Folder path is intentionally omitted — it's resolved live per workspace
    from Power BI folder metadata and isn't part of the batch catalog.
    """
    datasets_map = catalog.get("datasets") or {}
    now = datetime.now(timezone.utc)
    rows: List[Dict[str, Any]] = []
    by_workspace: Dict[str, int] = {}

    for ws in catalog.get("workspaces") or []:
        wid = ws.get("id")
        if not wid:
            continue
        wname = ws.get("name") or ""
        for r in ws.get("reports") or []:
            name = r.get("name") or "Unknown"
            if is_excluded_report_name(name):
                continue
            ds = datasets_map.get(r.get("datasetId") or "") or {}
            live_dq = _is_live(r, ds)

            days = r.get("days_since_refresh")
            if days is None:
                days = ds.get("days_since_refresh")
            last_ref = r.get("last_refreshed") or ds.get("last_refreshed")
            if days is None and last_ref:
                days = _parse_days(last_ref, now)
            try:
                days = int(days) if days is not None and days != "" else None
            except Exception:
                days = None

            refresh_status = r.get("last_refresh_status") or ds.get("last_refresh_status") or ""

            vc = r.get("view_count")
            if vc is None:
                vc = r.get("views")
            views_known = vc is not None
            try:
                views = int(vc) if vc is not None else 0
            except Exception:
                views = 0

            reasons: List[str] = []
            if days is not None and days > refresh_stale_days:
                reasons.append(f"Days since refresh > {refresh_stale_days} ({days}d)")

            no_history = False
            if not live_dq:
                if (not last_ref) and (days is None):
                    no_history = True
                elif str(refresh_status).lower() in {"error", "failed"} and not last_ref:
                    no_history = True
            if no_history:
                reasons.append("No refresh history")

            zero_views = views_known and views == 0
            if zero_views:
                reasons.append(f"0 views in last {USAGE_LOOKBACK_DAYS} days")

            refresh_src = str(
                r.get("refresh_source") or ds.get("refresh_source") or ""
            ).lower().replace("-", "_").replace(" ", "")
            status_l = str(refresh_status or "").lower()
            note_l = str(r.get("refresh_note") or ds.get("refresh_note") or "").lower()
            needs_verify = (
                status_l in {"unverified", "estimated"}
                or refresh_src in {"content_modified", "content_created", "contentcreated", "created"}
                or "verify in power bi" in note_l
                or "verify in pbi" in note_l
            )
            if needs_verify and not live_dq:
                reasons.append("Verify in PBI (unconfirmed refresh)")

            if not reasons:
                continue

            owner = _clean_person(
                ds.get("dataset_owner"),
                r.get("configuredBy"),
                r.get("createdBy"),
                r.get("created_by"),
                r.get("modifiedBy"),
                r.get("modified_by"),
            )
            model_name = (r.get("datasetName") or r.get("dataset_name") or ds.get("name") or "").strip() or "NA"
            last_ref_disp = str(last_ref)[:19].replace("T", " ") if last_ref else "NA"

            rows.append({
                "reportId": r.get("id"),
                "reportName": name,
                "workspaceId": wid,
                "workspaceName": wname,
                "datasetId": r.get("datasetId") or "",
                "modelName": model_name,
                "lastRefreshedOn": last_ref_disp,
                "daysSinceRefresh": days if days is not None else None,
                "contactOwner": owner or "NA",
                "viewCount": views if views_known else None,
                "reasons": reasons,
                "zeroViews": zero_views,
                "needsVerify": bool(needs_verify and not live_dq),
            })
            by_workspace[wid] = by_workspace.get(wid, 0) + 1

    rows.sort(key=lambda r: (
        (r.get("workspaceName") or "").lower(),
        (r.get("reportName") or "").lower(),
    ))

    return {
        "generatedAt": catalog.get("generatedAt") or datetime.now(timezone.utc).isoformat(),
        "opsEnrichedAt": catalog.get("opsEnrichedAt"),
        "schemaVersion": "1.0",
        "refreshStaleDaysThreshold": refresh_stale_days,
        "usageLookbackDays": USAGE_LOOKBACK_DAYS,
        "candidateCount": len(rows),
        "workspaceCounts": by_workspace,
        "rows": rows,
    }


def write_thin_packs(latest_dir: Path) -> Dict[str, Path]:
    """Build ui_*.json into latest_dir from full artifacts. Returns paths written."""
    latest_dir = Path(latest_dir)
    out: Dict[str, Path] = {}
    cat_path = latest_dir / "workspace_catalog.json"
    if cat_path.is_file():
        cat = json.loads(cat_path.read_text(encoding="utf-8"))
        home = build_ui_home_index(cat)
        p = latest_dir / "ui_home_index.json"
        p.write_text(json.dumps(home, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        out["ui_home_index"] = p
        logger.info("Wrote %s (%.1f KB, %s workspaces)", p.name, p.stat().st_size / 1024, home["workspaceCount"])
        directory = build_ui_report_directory(cat)
        p = latest_dir / "ui_report_directory.json"
        p.write_text(json.dumps(directory, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        out["ui_report_directory"] = p
        logger.info(
            "Wrote %s (%.1f KB, %s reports)",
            p.name,
            p.stat().st_size / 1024,
            directory.get("reportCount") or 0,
        )
        try:
            decomm = build_ui_decommission_candidates(cat)
            p = latest_dir / "ui_decommission_candidates.json"
            p.write_text(json.dumps(decomm, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            out["ui_decommission_candidates"] = p
            logger.info(
                "Wrote %s (%.1f KB, %s candidates)",
                p.name, p.stat().st_size / 1024, decomm.get("candidateCount") or 0,
            )
        except Exception as exc:
            logger.warning("decommission candidates thin pack build skipped: %s", exc)
    imp_path = latest_dir / "impact_index.json"
    if imp_path.is_file():
        imp = json.loads(imp_path.read_text(encoding="utf-8"))
        tables = build_ui_impact_tables(imp)
        p = latest_dir / "ui_impact_tables.json"
        p.write_text(json.dumps(tables, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        out["ui_impact_tables"] = p
        logger.info("Wrote %s (%.1f MB, %s rows)", p.name, p.stat().st_size / (1024 * 1024), tables["tableCount"])
        reports_pack = build_ui_impact_reports(imp)
        p = latest_dir / "ui_impact_reports.json"
        p.write_text(json.dumps(reports_pack, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        out["ui_impact_reports"] = p
        logger.info(
            "Wrote %s (%.1f MB, %s reports)",
            p.name,
            p.stat().st_size / (1024 * 1024),
            reports_pack.get("reportCount") or 0,
        )
    fld_path = latest_dir / "field_usage_index.json"
    if fld_path.is_file():
        try:
            fld = json.loads(fld_path.read_text(encoding="utf-8"))
            field_pack = build_ui_field_index(fld)
            p = latest_dir / "ui_field_index.json"
            p.write_text(json.dumps(field_pack, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            out["ui_field_index"] = p
            logger.info(
                "Wrote %s (%.1f KB, %s fields)",
                p.name, p.stat().st_size / 1024, field_pack.get("fieldCount") or 0,
            )
        except Exception as exc:
            logger.warning("field usage thin pack build skipped: %s", exc)
    unused_path = latest_dir / "unused_fields_index.json"
    if unused_path.is_file():
        try:
            unused = json.loads(unused_path.read_text(encoding="utf-8"))
            unused_pack = build_ui_unused_fields_index(unused)
            p = latest_dir / "ui_unused_fields_index.json"
            p.write_text(json.dumps(unused_pack, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            out["ui_unused_fields_index"] = p
            logger.info(
                "Wrote %s (%.1f KB, %s unused fields)",
                p.name, p.stat().st_size / 1024, unused_pack.get("unusedCount") or 0,
            )
        except Exception as exc:
            logger.warning("unused fields thin pack build skipped: %s", exc)
    return out
