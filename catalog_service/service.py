"""
CatalogService — SharePoint source of truth + server-side caches.

Architecture (production):
  SharePoint {folder}/latest/*.json     ← durable source of truth
       ↓ verified download (ranged)
  data/catalog_cache/latest/*.json      ← server disk mirror (not SoT)
       ↓ parse once
  process memory (TTL)                  ← hot path for APIs
       ↓ thin query endpoints
  Browser                               ← only KB–low-MB JSON, never 300MB blobs

Extract jobs write temp JSON, upload to SharePoint, delete temp.
The browser never reads SharePoint and never receives workspace_catalog /
impact_index raw files.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from catalog_service import catalog_config as cfg

logger = logging.getLogger("catalog_service")

_lock = threading.RLock()
# name -> {data, loaded_at, source, sp_size, sp_modified}
_memory: Dict[str, Dict[str, Any]] = {}
# name -> last time we compared disk mirror vs Graph meta
_disk_checked_at: Dict[str, float] = {}


class CatalogService:
    """Thread-safe SharePoint catalog loader + thin query helpers."""

    def __init__(self) -> None:
        try:
            cfg.CATALOG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------ status
    def status(self) -> Dict[str, Any]:
        mode = self._resolve_mode()
        files = {}
        for name in list(cfg.REQUIRED_CATALOG_FILES) + [
            "ui_home_index.json", "ui_impact_tables.json", "ops_summary.json"
        ]:
            meta = _memory.get(name)
            disk = self._disk_path(name)
            files[name] = {
                "inMemory": bool(meta),
                "memoryAgeSec": int(time.time() - meta["loaded_at"]) if meta else None,
                "source": meta.get("source") if meta else None,
                "diskCached": disk.is_file(),
                "diskSizeMb": round(disk.stat().st_size / (1024 * 1024), 2) if disk.is_file() else None,
            }
        return {
            "enabled": cfg.CATALOG_FAST_PATH_ENABLED and mode == "sharepoint",
            "mode": mode,
            "sharepointConfigured": cfg.sharepoint_configured(),
            "sharepointFolder": f"{(cfg.SHAREPOINT_FOLDER_PATH or '').strip('/')}/latest",
            "cacheTtlSec": cfg.CATALOG_CACHE_TTL_SEC,
            "diskCacheDir": str(cfg.CATALOG_CACHE_DIR),
            "diskRevalidateSec": cfg.CATALOG_DISK_REVALIDATE_SEC,
            "fastPath": cfg.CATALOG_FAST_PATH_ENABLED,
            "files": files,
            "generatedAt": self._peek_generated_at(),
            "sourceOfTruth": "sharepoint",
            "browserBlockedFiles": sorted(cfg.BROWSER_BLOCKED_CATALOG_FILES),
            "architecture": "sp_sot_server_disk_mirror_thin_api",
        }

    def is_available(self) -> bool:
        """True when Home can answer without waiting on full catalog if ui pack exists."""
        if not cfg.CATALOG_FAST_PATH_ENABLED:
            return False
        if self._resolve_mode() != "sharepoint":
            return False
        # Prefer thin home pack — cheap availability
        thin = self.get_json("ui_home_index.json")
        if thin and thin.get("workspaces") is not None:
            return True
        cat = self.get_workspace_catalog()
        return bool(cat and cat.get("workspaces") is not None)

    # ------------------------------------------------------------------ loaders
    def get_json(self, name: str, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        name = Path(name).name
        if not name.endswith(".json"):
            return None
        with _lock:
            if not force_refresh:
                mem = _memory.get(name)
                if mem and (time.time() - mem["loaded_at"]) < cfg.CATALOG_CACHE_TTL_SEC:
                    return mem["data"]
            data, source = self._load_catalog_file(name, force_refresh=force_refresh)
            if data is not None:
                _memory[name] = {
                    "data": data,
                    "loaded_at": time.time(),
                    "source": source,
                }
            elif force_refresh:
                _memory.pop(name, None)
            return data

    def get_workspace_catalog(self, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        cat = self.get_json("workspace_catalog.json", force_refresh=force_refresh)
        # Opportunistically materialize thin home pack for next Home hit
        if cat and "ui_home_index.json" not in _memory:
            try:
                self._ensure_thin_home_pack(cat)
            except Exception as exc:
                logger.warning("thin home pack build skipped: %s", exc)
        return cat

    def get_impact_index(self, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        idx = self.get_json("impact_index.json", force_refresh=force_refresh)
        if idx and "ui_impact_tables.json" not in _memory:
            try:
                self._ensure_thin_impact_pack(idx)
            except Exception as exc:
                logger.warning("thin impact pack build skipped: %s", exc)
        return idx

    def _ensure_thin_home_pack(self, cat: Dict[str, Any]) -> None:
        from catalog_service.thin_packs import build_ui_home_index
        home = build_ui_home_index(cat)
        _memory["ui_home_index.json"] = {
            "data": home, "loaded_at": time.time(), "source": "derived-in-process",
        }
        try:
            raw = json.dumps(home, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._write_disk_mirror(
                "ui_home_index.json", raw,
                sp_size=len(raw), sp_modified=home.get("generatedAt") or "",
            )
        except Exception:
            pass

    def _ensure_thin_impact_pack(self, idx: Dict[str, Any]) -> None:
        from catalog_service.thin_packs import build_ui_impact_tables
        tables = build_ui_impact_tables(idx)
        _memory["ui_impact_tables.json"] = {
            "data": tables, "loaded_at": time.time(), "source": "derived-in-process",
        }
        try:
            raw = json.dumps(tables, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._write_disk_mirror(
                "ui_impact_tables.json", raw,
                sp_size=len(raw), sp_modified=tables.get("generatedAt") or "",
            )
        except Exception:
            pass

    def get_summary(self, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        return self.get_json("summary.json", force_refresh=force_refresh)

    def get_sources(self, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        return self.get_json("sources.json", force_refresh=force_refresh)

    def invalidate(self, name: Optional[str] = None) -> None:
        with _lock:
            if name:
                n = Path(name).name
                _memory.pop(n, None)
                _disk_checked_at.pop(n, None)
            else:
                _memory.clear()
                _disk_checked_at.clear()

    # ---------------------------------------------------------- query helpers
    def list_workspaces_for_user(
        self, allowed_workspace_ids: Optional[Set[str]] = None
    ) -> List[Dict[str, Any]]:
        cat = self.get_workspace_catalog()
        if not cat:
            return []
        out: List[Dict[str, Any]] = []
        for ws in cat.get("workspaces") or []:
            wid = ws.get("id")
            if not wid:
                continue
            if allowed_workspace_ids is not None and wid not in allowed_workspace_ids:
                continue
            reports = [
                r for r in (ws.get("reports") or [])
                if not str(r.get("name") or "").startswith("[App]")
            ]
            out.append({
                "id": wid,
                "name": ws.get("name") or "",
                "type": ws.get("type") or ws.get("workspaceType") or "",
                "state": ws.get("state") or "",
                "isOnDedicatedCapacity": ws.get("isOnDedicatedCapacity"),
                "capacityId": ws.get("capacityId"),
                "reportCount": len(reports),
                "datasetCount": len(ws.get("datasets") or []),
                "from_catalog": True,
            })
        out.sort(key=lambda w: (w.get("name") or "").lower())
        return out

    def get_workspace_reports(
        self,
        workspace_id: str,
        allowed_workspace_ids: Optional[Set[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        if allowed_workspace_ids is not None and workspace_id not in allowed_workspace_ids:
            return None
        cat = self.get_workspace_catalog()
        if not cat:
            return None
        ws = next((w for w in (cat.get("workspaces") or []) if w.get("id") == workspace_id), None)
        if not ws:
            return None

        datasets_by_id = dict(cat.get("datasets") or {})
        # include workspace-local datasets
        for d in ws.get("datasets") or []:
            did = d.get("id")
            if did and did not in datasets_by_id:
                datasets_by_id[did] = d

        reports = []
        ops_hits = 0
        for r in ws.get("reports") or []:
            name = r.get("name") or ""
            if name.startswith("[App]"):
                continue
            ds_id = r.get("datasetId") or ""
            ds = datasets_by_id.get(ds_id) or {}
            last_ref = r.get("last_refreshed") if "last_refreshed" in r else ds.get("last_refreshed")
            last_status = r.get("last_refresh_status") if "last_refresh_status" in r else ds.get("last_refresh_status")
            sched = r.get("refresh_schedule") if "refresh_schedule" in r else ds.get("refresh_schedule")
            rtype = r.get("refresh_type") if "refresh_type" in r else ds.get("refresh_type")
            rnote = r.get("refresh_note") if "refresh_note" in r else ds.get("refresh_note")
            days = r.get("days_since_refresh") if "days_since_refresh" in r else ds.get("days_since_refresh")
            has_ops = any(v is not None for v in (last_ref, last_status, days, r.get("view_count")))
            if has_ops:
                ops_hits += 1
            reports.append({
                **r,
                "datasetName": r.get("datasetName") or ds.get("name") or "",
                "dataset_owner": (
                    r.get("dataset_owner")
                    or ds.get("configuredBy")
                    or ds.get("dataset_owner")
                    or ds.get("owner")
                    or ""
                ),
                "last_refreshed": last_ref,
                "last_refresh_status": last_status,
                "refresh_schedule": sched,
                "refresh_type": rtype,
                "refresh_note": rnote,
                "days_since_refresh": days,
                "dataset_workspace_id": r.get("dataset_workspace_id") or ds.get("workspaceId") or workspace_id,
                "view_count": r.get("view_count"),
                "last_viewed": r.get("last_viewed"),
                "from_catalog": True,
                "ops_from_catalog": bool(r.get("ops_from_catalog") or has_ops),
                "refresh_pending": not has_ops,
            })

        return {
            "workspace": {
                "id": ws.get("id"),
                "name": ws.get("name"),
                "reportCount": len(reports),
                "datasetCount": ws.get("datasetCount") or len(ws.get("datasets") or []),
            },
            "workspace_id": workspace_id,
            "workspace_name": ws.get("name"),
            "reports": reports,
            "datasets": datasets_by_id,
            "datasets_map": datasets_by_id,
            "generatedAt": cat.get("generatedAt"),
            "opsEnrichedAt": cat.get("opsEnrichedAt"),
            "ops": cat.get("ops"),
            "opsCoverage": {
                "reportsWithOps": ops_hits,
                "reportsTotal": len(reports),
            },
            "sourceRunId": cat.get("sourceRunId"),
            "source": "sharepoint",
            "catalog_meta": {
                "generatedAt": cat.get("generatedAt"),
                "opsEnrichedAt": cat.get("opsEnrichedAt"),
                "source": "sharepoint",
            },
        }

    def build_home_summary(
        self,
        allowed_workspace_ids: Optional[Set[str]] = None,
        inactive_days: int = 30,
    ) -> Optional[Dict[str, Any]]:
        """
        Fast Home dashboard stats.

        Prefer precomputed ui_home_index.json (tiny) — no need for 300MB catalog.
        Fall back to computing from workspace_catalog when pack is missing
        (e.g. first run before next extract publishes the pack).
        """
        thin = self.get_json("ui_home_index.json")
        if thin and isinstance(thin.get("workspaces"), list):
            return self._home_from_thin_index(thin, allowed_workspace_ids, inactive_days)
        return self._home_from_full_catalog(allowed_workspace_ids, inactive_days)

    def _home_from_thin_index(
        self,
        thin: Dict[str, Any],
        allowed_workspace_ids: Optional[Set[str]],
        inactive_days: int,
    ) -> Dict[str, Any]:
        workspaces_out: List[Dict[str, Any]] = []
        total_reports = total_inactive = total_orphaned = total_active = 0
        for ws in thin.get("workspaces") or []:
            wid = ws.get("id")
            if not wid:
                continue
            if allowed_workspace_ids is not None and wid not in allowed_workspace_ids:
                continue
            rc = int(ws.get("reportCount") or 0)
            ic = int(ws.get("inactiveCount") or 0)
            oc = int(ws.get("orphanedCount") or 0)
            ac = int(ws.get("activeCount") if ws.get("activeCount") is not None else max(0, rc - ic))
            total_reports += rc
            total_inactive += ic
            total_orphaned += oc
            total_active += ac
            workspaces_out.append({
                "id": wid,
                "name": ws.get("name") or "",
                "reportCount": rc,
                "inactiveCount": ic,
                "activeCount": ac,
                "orphanedCount": oc,
                "from_catalog": True,
            })
        workspaces_out.sort(key=lambda w: (w.get("name") or "").lower())
        return {
            "success": True,
            "source": "sharepoint-ui-pack",
            "fallback": False,
            "generatedAt": thin.get("generatedAt"),
            "opsEnrichedAt": thin.get("opsEnrichedAt"),
            "workspaceCount": len(workspaces_out),
            "totalReports": total_reports,
            "activeReports": total_active,
            "inactiveReports": total_inactive,
            "orphanedReports": total_orphaned,
            "inactiveDaysThreshold": inactive_days,
            "workspaces": workspaces_out,
        }

    def _home_from_full_catalog(
        self,
        allowed_workspace_ids: Optional[Set[str]],
        inactive_days: int,
    ) -> Optional[Dict[str, Any]]:
        cat = self.get_workspace_catalog()
        if not cat or not cat.get("workspaces"):
            return None

        datasets_map = cat.get("datasets") or {}
        now = datetime.now(timezone.utc)

        def _parse_days(iso_ts: Optional[str]) -> Optional[int]:
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
                or "directquery" in status
                or "live" in status
                or "directquery" in schedule
                or "live" in schedule
                or "live connection" in note
            )

        def _is_inactive(report: Dict[str, Any], ds: Dict[str, Any]) -> bool:
            if _is_live(report, ds):
                return False
            days = report.get("days_since_refresh")
            if days is None:
                days = ds.get("days_since_refresh")
            if days is None:
                days = _parse_days(report.get("last_refreshed") or ds.get("last_refreshed"))
            if days is None:
                return False
            try:
                return int(days) >= inactive_days
            except Exception:
                return False

        def _is_orphaned(report: Dict[str, Any]) -> bool:
            creator = (report.get("createdBy") or report.get("created_by") or "").strip()
            modifier = (report.get("modifiedBy") or report.get("modified_by") or "").strip()
            has_owner_fields = any(
                k in report
                for k in ("createdBy", "created_by", "modifiedBy", "modified_by", "configuredBy", "dataset_owner")
            )
            if not has_owner_fields:
                return False
            owner = (report.get("configuredBy") or report.get("dataset_owner") or "").strip()
            return not creator and not modifier and not owner

        workspaces_out: List[Dict[str, Any]] = []
        total_reports = total_inactive = total_orphaned = total_active = 0

        for ws in cat.get("workspaces") or []:
            wid = ws.get("id")
            if not wid:
                continue
            if allowed_workspace_ids is not None and wid not in allowed_workspace_ids:
                continue

            ws_reports = ws_inactive = ws_orphaned = 0
            for r in ws.get("reports") or []:
                if str(r.get("name") or "").startswith("[App]"):
                    continue
                ds = datasets_map.get(r.get("datasetId") or "") or {}
                ws_reports += 1
                if _is_inactive(r, ds):
                    ws_inactive += 1
                if _is_orphaned(r):
                    ws_orphaned += 1

            ws_active = max(0, ws_reports - ws_inactive)
            total_reports += ws_reports
            total_inactive += ws_inactive
            total_orphaned += ws_orphaned
            total_active += ws_active
            workspaces_out.append({
                "id": wid,
                "name": ws.get("name") or "",
                "reportCount": ws_reports,
                "inactiveCount": ws_inactive,
                "activeCount": ws_active,
                "orphanedCount": ws_orphaned,
                "from_catalog": True,
            })

        workspaces_out.sort(key=lambda w: (w.get("name") or "").lower())
        return {
            "success": True,
            "source": "sharepoint",
            "fallback": False,
            "generatedAt": cat.get("generatedAt"),
            "opsEnrichedAt": cat.get("opsEnrichedAt"),
            "workspaceCount": len(workspaces_out),
            "totalReports": total_reports,
            "activeReports": total_active,
            "inactiveReports": total_inactive,
            "orphanedReports": total_orphaned,
            "inactiveDaysThreshold": inactive_days,
            "workspaces": workspaces_out,
        }

    # -------------------------------------------------------------- impact thin
    def impact_table_rows(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Thin table list for Impact Explorer grid (no nested datasets/reports).
        Uses ui_impact_tables.json when present; else derives from impact_index.
        """
        pack = self.get_json("ui_impact_tables.json", force_refresh=force_refresh)
        if pack and isinstance(pack.get("rows"), list):
            return pack["rows"]
        return self._derive_impact_rows_from_index(force_refresh=force_refresh)

    def _derive_impact_rows_from_index(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        idx = self.get_impact_index(force_refresh=force_refresh)
        if not idx:
            return []
        rows = []
        for key, entry in (idx.get("tables") or {}).items():
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
        return rows

    def impact_table_detail(self, table_key: str) -> Optional[Dict[str, Any]]:
        """Full impact entry for one table (drawer). Loads index server-side only."""
        if not table_key:
            return None
        idx = self.get_impact_index()
        if not idx:
            return None
        tables = idx.get("tables") or {}
        entry = tables.get(table_key)
        if entry:
            return entry
        # case-insensitive key / table name match
        tk = table_key.lower()
        for k, e in tables.items():
            if str(k).lower() == tk or str(e.get("tableKey") or "").lower() == tk:
                return e
            if str(e.get("table") or "").lower() == tk:
                return e
            for m in e.get("modelTableNames") or []:
                if str(m).lower() == tk:
                    return e
        return None

    def impact_model_details(
        self,
        dataset_id: str,
        workspace_id: str = "",
        focus_table: str = "",
        model_table_name: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Thin payload for Impact Explorer model popup.
        One dataset from workspace_catalog — never ships full catalog to browser.
        """
        if not dataset_id:
            return None
        cat = self.get_workspace_catalog()
        if not cat:
            return None

        dataset = None
        dmap = cat.get("datasets") or {}
        if isinstance(dmap, dict):
            dataset = dmap.get(dataset_id)

        ws_name = ""
        if workspace_id:
            for ws in cat.get("workspaces") or []:
                if ws.get("id") == workspace_id:
                    ws_name = ws.get("name") or ""
                    if dataset is None or not (dataset or {}).get("tables"):
                        for ds in ws.get("datasets") or []:
                            if ds.get("id") == dataset_id:
                                dataset = ds
                                break
                    break

        if dataset is None and isinstance(dmap, dict):
            # last resort: any workspace list entry
            for ws in cat.get("workspaces") or []:
                for ds in ws.get("datasets") or []:
                    if ds.get("id") == dataset_id:
                        dataset = ds
                        ws_name = ws_name or (ws.get("name") or "")
                        if not workspace_id:
                            workspace_id = ws.get("id") or ""
                        break
                if dataset is not None:
                    break

        if not dataset:
            return None

        focus_l = (focus_table or "").strip().lower()
        model_l = (model_table_name or "").strip().lower()
        focus_names = {n for n in (focus_l, model_l) if n}

        # Also match impact alias: physical name may differ from model table name
        tables_out = []
        focused = []
        for t in dataset.get("tables") or []:
            if not isinstance(t, dict):
                continue
            name = t.get("name") or ""
            name_l = name.lower()
            sql_tables = [str(x).lower() for x in (t.get("sqlSourceTables") or [])]
            src_names = []
            for s in t.get("sources") or []:
                if isinstance(s, dict):
                    for fld in ("table", "object_name", "objectName"):
                        if s.get(fld):
                            src_names.append(str(s.get(fld)).lower())
            expr_text = t.get("sourceExpression") or ""
            sql_q = t.get("sqlQuery") or ""
            file_name = t.get("fileName") or ""
            source_url = t.get("sourceUrl") or ""
            server_name = t.get("serverName") or ""
            source_type_label = t.get("sourceTypeLabel") or "Unknown"
            sql_source_tables = list(t.get("sqlSourceTables") or [])

            # Recover display fields from M expression (older snapshots / partial catalog)
            needs_display = expr_text and (
                not sql_q
                or not source_url
                or not file_name
                or not server_name
                or source_type_label in ("", "Unknown")
                or not sql_source_tables
            )
            if needs_display:
                try:
                    from catalog_service.metadata_lib.expression_parser import (
                        classify_source_display,
                    )

                    display = classify_source_display(expr_text, [])
                    sql_q = sql_q or (display.get("sqlQuery") or "")
                    file_name = file_name or (display.get("fileName") or "")
                    source_url = source_url or (display.get("sourceUrl") or "")
                    server_name = server_name or (display.get("serverName") or "")
                    if source_type_label in ("", "Unknown"):
                        source_type_label = display.get("sourceTypeLabel") or source_type_label
                    if not sql_source_tables:
                        sql_source_tables = list(display.get("sqlSourceTables") or [])
                except Exception:
                    pass
            if not source_url and expr_text:
                m = re.search(r'https?://[^\s"\']+', expr_text)
                if m:
                    source_url = m.group(0).rstrip(")',\"")
            if not file_name and expr_text:
                fm = re.search(r"([^\\/'\"]+\.(?:xlsx?|csv|xls))", expr_text, re.I)
                if fm:
                    file_name = fm.group(1)

            # Refresh sql_tables for focus matching after recovery
            sql_tables = [str(x).lower() for x in sql_source_tables] or sql_tables

            row = {
                "name": name,
                "isHidden": bool(t.get("isHidden")),
                "sourceTypeLabel": source_type_label,
                "serverName": server_name,
                "sqlSourceTables": sql_source_tables,
                "sqlQuery": sql_q,
                "fileName": file_name,
                "sourceUrl": source_url,
                "sourceExpression": expr_text,
                "columnCount": t.get("columnCount")
                if t.get("columnCount") is not None
                else len(t.get("columns") or []),
                "measureCount": t.get("measureCount")
                if t.get("measureCount") is not None
                else len(t.get("measures") or []),
                "columns": [
                    {
                        "name": c.get("name") if isinstance(c, dict) else str(c),
                        "dataType": (c.get("dataType") or c.get("type") or "")
                        if isinstance(c, dict)
                        else "",
                        "isHidden": bool(c.get("isHidden")) if isinstance(c, dict) else False,
                    }
                    for c in (t.get("columns") or [])
                ],
                "measures": [
                    {
                        "name": m.get("name") if isinstance(m, dict) else str(m),
                        "expression": (m.get("expression") or "") if isinstance(m, dict) else "",
                    }
                    for m in (t.get("measures") or [])
                ],
            }

            is_focus = False
            if focus_names:
                if name_l in focus_names:
                    is_focus = True
                elif any(fn in name_l or name_l in fn for fn in focus_names if len(fn) > 2):
                    is_focus = True
                elif any(fn in st or st.endswith("." + fn) for st in sql_tables for fn in focus_names):
                    is_focus = True
                elif any(fn in sn or sn in fn for sn in src_names for fn in focus_names if fn):
                    is_focus = True
            row["isFocus"] = is_focus
            tables_out.append(row)
            if is_focus:
                focused.append(row)

        # If focus name given but nothing matched, try modelTableName exact on impact side only
        if focus_names and not focused and model_l:
            for row in tables_out:
                if (row.get("name") or "").lower() == model_l:
                    row["isFocus"] = True
                    focused.append(row)

        all_measures = []
        for row in tables_out:
            for m in row.get("measures") or []:
                all_measures.append({
                    "name": m.get("name"),
                    "table": row.get("name"),
                    "expression": m.get("expression") or "",
                })

        return {
            "datasetId": dataset_id,
            "datasetName": dataset.get("name") or "",
            "workspaceId": workspace_id or dataset.get("workspaceId") or "",
            "workspaceName": ws_name,
            "targetStorageMode": dataset.get("targetStorageMode") or dataset.get("storageMode") or "",
            "focusTable": focus_table or model_table_name or "",
            "focusTables": focused,
            "tables": tables_out,
            "measures": all_measures,
            "tableCount": len(tables_out),
            "measureCount": len(all_measures),
            "columnCount": sum(len(t.get("columns") or []) for t in tables_out),
        }

    def lookup_table(self, table_name: str) -> List[Dict[str, Any]]:
        """Impact lookup by table / model name (case-insensitive)."""
        from catalog_service.metadata_lib.impact_builder import lookup_table_impact

        idx = self.get_impact_index()
        if not idx:
            return []
        return lookup_table_impact(idx, table_name)

    def impact_top(self, n: int = 50) -> List[Dict[str, Any]]:
        return self.top_impact_tables(n=n)

    def top_impact_tables(self, n: int = 15) -> List[Dict[str, Any]]:
        rows = self.impact_table_rows()
        # Strip searchText for API brevity if present
        out = []
        for r in rows[:n]:
            out.append({k: v for k, v in r.items() if k != "searchText"})
        return out

    def workspace_orphan_stats(
        self, workspace_id: str, allowed_workspace_ids: Optional[Set[str]] = None
    ) -> Optional[Dict[str, Any]]:
        pack = self.get_workspace_reports(workspace_id, allowed_workspace_ids)
        if not pack:
            return None
        total = 0
        orphaned = 0
        for r in pack.get("reports") or []:
            total += 1
            creator = (r.get("createdBy") or r.get("created_by") or "").strip()
            modifier = (r.get("modifiedBy") or r.get("modified_by") or "").strip()
            owner = (r.get("configuredBy") or r.get("dataset_owner") or "").strip()
            if not creator and not modifier and not owner:
                if any(k in r for k in ("createdBy", "created_by", "modifiedBy", "modified_by", "dataset_owner", "configuredBy")):
                    orphaned += 1
        return {
            "workspace_id": workspace_id,
            "total_reports": total,
            "orphaned_count": orphaned,
            "orphaned_percentage": round((orphaned / total) * 100) if total else 0,
        }

    # --------------------------------------------------------------- internals
    def _resolve_mode(self) -> str:
        if not cfg.CATALOG_FAST_PATH_ENABLED:
            return "off"
        if cfg.sharepoint_configured():
            return "sharepoint"
        return "off"

    def _peek_generated_at(self) -> Optional[str]:
        for name in (
            "ui_home_index.json", "summary.json", "ops_summary.json",
            "workspace_catalog.json", "ui_impact_tables.json",
        ):
            data = _memory.get(name, {}).get("data") if name in _memory else None
            if isinstance(data, dict):
                return (
                    data.get("generatedAt")
                    or data.get("opsEnrichedAt")
                    or data.get("usage")
                )
        return None

    def _disk_path(self, name: str) -> Path:
        return cfg.CATALOG_CACHE_DIR / Path(name).name

    def _meta_path(self, name: str) -> Path:
        return cfg.CATALOG_CACHE_DIR / f".{Path(name).name}.meta.json"

    def _remote_path(self, name: str) -> str:
        folder = (cfg.SHAREPOINT_FOLDER_PATH or "").strip("/")
        return f"{folder}/latest/{name}" if folder else f"latest/{name}"

    def _load_catalog_file(
        self, name: str, force_refresh: bool = False
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        Load order:
          1) Valid server disk mirror (size matches Graph meta) → parse disk
          2) Else download from SharePoint → write disk mirror → parse
        Disk is a cache only; SharePoint remains source of truth.
        """
        if not cfg.sharepoint_configured():
            # last resort: try disk-only if mirror exists (offline recovery)
            data = self._read_disk_json(name)
            if data is not None:
                return data, "disk-cache-offline"
            return None, "sharepoint-not-configured"

        try:
            cfg.validate_sharepoint_config()
            from catalog_service.metadata_lib.sharepoint_client import SharePointClient
        except Exception as exc:
            logger.warning("SharePoint config/auth: %s", exc)
            data = self._read_disk_json(name)
            if data is not None:
                return data, "disk-cache-offline"
            return None, f"config/auth: {exc}"

        try:
            sp = SharePointClient()
            sp.resolve_site_and_drive()
            remote = self._remote_path(name)
            sp_meta = sp.get_item_meta(remote)
            sp_size = int(sp_meta.get("size") or 0)
            sp_mod = sp_meta.get("lastModifiedDateTime") or ""

            disk = self._disk_path(name)
            meta_file = self._meta_path(name)
            now = time.time()
            checked = _disk_checked_at.get(name, 0)

            use_disk = False
            if (
                not force_refresh
                and disk.is_file()
                and sp_size > 0
                and disk.stat().st_size == sp_size
            ):
                # If we revalidated recently, trust disk without another Graph hit next time
                # (we already hit Graph for meta this call).
                use_disk = True
                # Optional: also verify sidecar meta matches
                try:
                    if meta_file.is_file():
                        side = json.loads(meta_file.read_text(encoding="utf-8"))
                        if int(side.get("size") or 0) != sp_size:
                            use_disk = False
                except Exception:
                    pass

            if use_disk:
                data = self._read_disk_json(name)
                if data is not None:
                    _disk_checked_at[name] = now
                    logger.info(
                        "Catalog %s loaded from disk mirror (%.1f MB, sp_size match)",
                        name, sp_size / (1024 * 1024),
                    )
                    return data, "disk-mirror"

            # Download from SharePoint (source of truth)
            raw = sp.download_file(remote, max_attempts=3, timeout=1800)
            if sp_size and len(raw) != sp_size:
                raise IOError(
                    f"Download size mismatch for {name}: got {len(raw)}, meta {sp_size}"
                )
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("utf-8-sig")
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError(f"{name} root JSON is {type(data).__name__}, expected object")

            # Persist verified mirror + sidecar
            self._write_disk_mirror(name, raw, sp_size=sp_size, sp_modified=sp_mod)
            _disk_checked_at[name] = now
            logger.info(
                "Catalog %s loaded from SharePoint (%.1f MB verified) → disk mirror",
                name, len(raw) / (1024 * 1024),
            )
            return data, "sharepoint"
        except Exception as exc:
            logger.exception("Catalog load failed for %s", name)
            # Stale disk recovery if download failed mid-way
            data = self._read_disk_json(name)
            if data is not None:
                logger.warning("Serving stale disk mirror for %s after SP error: %s", name, exc)
                return data, "disk-cache-stale"
            return None, str(exc)

    def _read_disk_json(self, name: str) -> Optional[Dict[str, Any]]:
        path = self._disk_path(name)
        if not path.is_file():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning("Disk mirror read failed for %s: %s", name, exc)
        return None

    def _write_disk_mirror(
        self, name: str, raw: bytes, *, sp_size: int, sp_modified: str
    ) -> None:
        try:
            cfg.CATALOG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = self._disk_path(name)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            side = {
                "name": name,
                "size": sp_size or len(raw),
                "lastModifiedDateTime": sp_modified,
                "cachedAt": datetime.now(timezone.utc).isoformat(),
            }
            meta_path = self._meta_path(name)
            meta_tmp = meta_path.with_suffix(".tmp")
            meta_tmp.write_text(json.dumps(side), encoding="utf-8")
            os.replace(meta_tmp, meta_path)
        except Exception as exc:
            logger.warning("Could not write disk mirror for %s: %s", name, exc)


# Process-wide singleton
catalog_service = CatalogService()
