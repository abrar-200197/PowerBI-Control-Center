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
    total_r = total_i = total_o = total_a = total_zv = 0
    for ws in catalog.get("workspaces") or []:
        wid = ws.get("id")
        if not wid:
            continue
        wname = ws.get("name") or ""
        rc = ic = oc = zc = 0
        for r in ws.get("reports") or []:
            if str(r.get("name") or "").startswith("[App]"):
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
        ac = max(0, rc - ic)
        total_r += rc
        total_i += ic
        total_o += oc
        total_a += ac
        total_zv += zc
        workspaces.append({
            "id": wid,
            "name": wname,
            "reportCount": rc,
            "inactiveCount": ic,
            "activeCount": ac,
            "orphanedCount": oc,
            "zeroViewsCount": zc,
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
        "workspaces": workspaces,
        # KPI tab detail lists (ACL-filter on the server)
        "detailLists": {
            "reports": _sort_ws_name(reports_rows),
            "inactive": _sort_inactive(inactive_rows),
            "orphaned": _sort_ws_name(orphaned_rows),
            "zero_views": _sort_ws_name(zero_views_rows),
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
            if not rname or rname.startswith("[App]"):
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
    return out
