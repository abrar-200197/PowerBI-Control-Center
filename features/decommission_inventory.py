"""
Inventory of decommissioned reports from SharePoint archive tree.

Layout (same as archive upload):
  {SHAREPOINT_DECOMM_FOLDER_PATH}/
    <batch folder>/
      <WorkspaceName>/
        [<PBI Folder>/]
          Report.pbix | Report.rdl

We do not claim live Power BI metadata — only SharePoint file facts:
  workspace, optional folder, report name, type, size, dates, webUrl, batch.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Process cache (Graph walks are chatty)
_CACHE: Dict[str, Any] = {"ts": 0.0, "payload": None}
_CACHE_TTL_SEC = 300  # 5 minutes


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        t = s.replace("Z", "+00:00")
        return datetime.fromisoformat(t)
    except Exception:
        return None


def _fmt_dt(s: Optional[str]) -> Optional[str]:
    dt = _parse_dt(s)
    if not dt:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _strip_ext(name: str) -> Tuple[str, str]:
    p = PurePosixPath(name)
    ext = (p.suffix or "").lower()
    stem = p.stem if ext else name
    return stem, ext.lstrip(".")


def _is_report_file(name: str) -> bool:
    n = (name or "").lower()
    return n.endswith(".pbix") or n.endswith(".rdl")


def _row_from_file(item: Dict[str, Any], base: str) -> Optional[Dict[str, Any]]:
    """
    Map one driveItem under decomm root → inventory row.
    relativePath is full path from drive root (set by list_files_recursive).
    """
    name = item.get("name") or ""
    if not _is_report_file(name):
        return None

    rel = (item.get("relativePath") or "").replace("\\", "/").strip("/")
    base_n = base.strip("/")
    if base_n and rel.startswith(base_n + "/"):
        under = rel[len(base_n) + 1 :]
    elif base_n and rel == base_n:
        return None
    else:
        under = rel

    parts = [p for p in under.split("/") if p]
    # Expect: batch / workspace / [folder…] / file
    if len(parts) < 3:
        # batch/file or workspace missing — skip incomplete paths
        if len(parts) == 2:
            batch, fname = parts
            workspace, folder = "Unknown", None
            if not _is_report_file(fname):
                return None
        else:
            return None
    else:
        batch = parts[0]
        workspace = parts[1]
        fname = parts[-1]
        mid = parts[2:-1]
        folder = " / ".join(mid) if mid else None
        if not _is_report_file(fname):
            return None

    report_name, ext = _strip_ext(fname)
    created = item.get("createdDateTime") or item.get("fileSystemInfo", {}).get("createdDateTime")
    modified = item.get("lastModifiedDateTime")
    # Decommissioned date = upload/create on SharePoint (archive time)
    decomm_raw = created or modified
    size = int(item.get("size") or 0)

    return {
        "reportName": report_name,
        "fileName": fname,
        "fileType": ext.upper() if ext else "FILE",
        "workspaceName": workspace,
        "folderName": folder,
        "batchFolder": batch,
        "decommissionedAt": decomm_raw,
        "decommissionedAtDisplay": _fmt_dt(decomm_raw),
        "lastModifiedAt": modified,
        "lastModifiedAtDisplay": _fmt_dt(modified),
        "sizeBytes": size,
        "sizeDisplay": _size_label(size),
        "webUrl": item.get("webUrl") or "",
        "sharePointPath": rel,
    }


def _size_label(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def build_decommission_inventory(*, force_refresh: bool = False) -> Dict[str, Any]:
    """
    Walk SharePoint Report Decommission Activity tree → flat report rows + workspace groups.
    Cached in-process for _CACHE_TTL_SEC.
    """
    global _CACHE
    now = time.time()
    if (
        not force_refresh
        and _CACHE.get("payload")
        and (now - float(_CACHE.get("ts") or 0)) < _CACHE_TTL_SEC
    ):
        return dict(_CACHE["payload"])

    from catalog_service import catalog_config as cfg
    from catalog_service.metadata_lib.sharepoint_client import SharePointClient

    base = (getattr(cfg, "SHAREPOINT_DECOMM_FOLDER_PATH", None) or "").strip("/")
    if not base:
        return {
            "success": False,
            "error": "SHAREPOINT_DECOMM_FOLDER_PATH is not configured",
            "rows": [],
            "workspaces": [],
        }

    try:
        sp = SharePointClient()
        sp.resolve_site_and_drive()
        # Confirm base exists
        try:
            batches = sp.list_folders(base)
        except Exception as ex:
            return {
                "success": False,
                "error": f"Cannot list decommission root '{base}': {ex}",
                "basePath": base,
                "rows": [],
                "workspaces": [],
            }

        files = sp.list_files_recursive(base, max_depth=10)
        rows: List[Dict[str, Any]] = []
        try:
            from catalog_service.thin_packs import is_excluded_report_name
        except Exception:
            def is_excluded_report_name(name):  # type: ignore
                n = (name or "").strip().casefold()
                return n in {
                    "usage metrics report",
                    "report usage metrics report",
                    "dashboard usage metrics report",
                }
        for it in files:
            row = _row_from_file(it, base)
            if not row:
                continue
            # Hide platform usage metrics archives (not business decomm content)
            if is_excluded_report_name(row.get("reportName")):
                continue
            rows.append(row)

        # Newest first
        def _sort_key(r: Dict[str, Any]):
            return r.get("decommissionedAt") or r.get("lastModifiedAt") or ""

        rows.sort(key=_sort_key, reverse=True)

        # Group by workspace
        by_ws: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            wn = r.get("workspaceName") or "Unknown"
            by_ws.setdefault(wn, []).append(r)

        workspaces = []
        for wn, rs in sorted(by_ws.items(), key=lambda x: x[0].lower()):
            workspaces.append({
                "workspaceName": wn,
                "reportCount": len(rs),
                "reports": rs,
            })

        payload = {
            "success": True,
            "basePath": base,
            "batchFolders": sorted({b.get("name") for b in batches if b.get("name")}),
            "batchCount": len(batches),
            "totalReports": len(rows),
            "workspaceCount": len(workspaces),
            "rows": rows,
            "workspaces": workspaces,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "cacheTtlSec": _CACHE_TTL_SEC,
            "source": "sharepoint",
            "note": (
                "Inventory is derived from SharePoint archive files only "
                "(not live Power BI metadata). Decommissioned date = file upload/create time."
            ),
        }
        _CACHE = {"ts": now, "payload": payload}
        return dict(payload)
    except Exception as ex:
        logger.exception("decommission inventory failed")
        return {
            "success": False,
            "error": str(ex),
            "basePath": base,
            "rows": [],
            "workspaces": [],
        }
