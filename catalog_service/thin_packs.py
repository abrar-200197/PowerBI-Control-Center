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


def build_ui_home_index(catalog: Dict[str, Any], inactive_days: int = 30) -> Dict[str, Any]:
    """Per-workspace counts only — typically < 200 KB for hundreds of workspaces."""
    datasets_map = catalog.get("datasets") or {}
    now = datetime.now(timezone.utc)
    workspaces: List[Dict[str, Any]] = []
    total_r = total_i = total_o = total_a = total_zv = 0
    for ws in catalog.get("workspaces") or []:
        wid = ws.get("id")
        if not wid:
            continue
        rc = ic = oc = zc = 0
        for r in ws.get("reports") or []:
            if str(r.get("name") or "").startswith("[App]"):
                continue
            ds = datasets_map.get(r.get("datasetId") or "") or {}
            rc += 1
            if _is_inactive(r, ds, inactive_days, now):
                ic += 1
            if _is_orphaned(r):
                oc += 1
            if _is_zero_views(r):
                zc += 1
        ac = max(0, rc - ic)
        total_r += rc
        total_i += ic
        total_o += oc
        total_a += ac
        total_zv += zc
        workspaces.append({
            "id": wid,
            "name": ws.get("name") or "",
            "reportCount": rc,
            "inactiveCount": ic,
            "activeCount": ac,
            "orphanedCount": oc,
            "zeroViewsCount": zc,
        })
    workspaces.sort(key=lambda w: (w.get("name") or "").lower())
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
    imp_path = latest_dir / "impact_index.json"
    if imp_path.is_file():
        imp = json.loads(imp_path.read_text(encoding="utf-8"))
        tables = build_ui_impact_tables(imp)
        p = latest_dir / "ui_impact_tables.json"
        p.write_text(json.dumps(tables, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        out["ui_impact_tables"] = p
        logger.info("Wrote %s (%.1f MB, %s rows)", p.name, p.stat().st_size / (1024 * 1024), tables["tableCount"])
    return out
