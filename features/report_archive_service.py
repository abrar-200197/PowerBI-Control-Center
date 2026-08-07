"""
Archive a Power BI report (.pbix / .rdl) to SharePoint decommission folder.

Target layout (same BA Retail Analytics site as catalog metadata):
  BA - Retail Offshore GCC Team/Backup - Reports & Archives/
    Report Decommission Activity/<latest dated child>/
      <WorkspaceName>/[<PBI Folder>/]<ReportFile>

Export uses Power BI REST:
  GET /groups/{ws}/reports/{id}/Export  → .pbix or .rdl bytes
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Set

import requests

logger = logging.getLogger(__name__)

# Central Analytics roster — same default as usage exclude (also @ashleyfurniture.com)
_DEFAULT_ARCHIVE_UPNS = (
    "mahmed@ashleyfurnitureindia.com,"
    "pshivanandam@ashleyfurnitureindia.com,"
    "aramalingam@ashleyfurnitureindia.com,"
    "mthanapathi@ashleyfurnitureindia.com,"
    "ksambasivam@ashleyfurnitureindia.com,"
    "danandkumar@ashleyfurnitureindia.com,"
    "jravikumar@ashleyfurnitureindia.com,"
    "kviswanathan@ashleyfurnitureindia.com,"
    "ychandran@ashleyfurnitureindia.com,"
    "nkathiresan@ashleyfurnitureindia.com,"
    "mahmed@ashleyfurniture.com"
)


def _norm_upn(s: str) -> str:
    return (s or "").strip().lower()


def archive_allowed_upns() -> Set[str]:
    """UPNs allowed to see/use Archive Download button."""
    base = {_norm_upn(u) for u in _DEFAULT_ARCHIVE_UPNS.split(",") if u.strip()}
    extra = os.getenv("REPORT_ARCHIVE_ALLOWED_UPNS") or ""
    for u in extra.split(","):
        if u.strip():
            base.add(_norm_upn(u))
    # Also merge usage-exclude defaults if catalog_config available
    try:
        from catalog_service import catalog_config as cfg
        for u in getattr(cfg, "USAGE_EXCLUDE_USER_UPNS", []) or []:
            base.add(_norm_upn(u))
    except Exception:
        pass
    return base


def user_can_archive(email_or_upn: Optional[str]) -> bool:
    if not email_or_upn:
        return False
    return _norm_upn(email_or_upn) in archive_allowed_upns()


def _safe_segment(name: str, fallback: str = "Item") -> str:
    """SharePoint-safe single path segment."""
    s = (name or "").strip() or fallback
    # Illegal in SP: " * : < > ? / \ |
    s = re.sub(r'[\"\*:<>?/\\|]+', " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:120] if s else fallback)


def _guess_ext(content_type: str, content_disp: str, report_name: str) -> str:
    cd = (content_disp or "").lower()
    ct = (content_type or "").lower()
    name_l = (report_name or "").lower()
    if ".rdl" in cd or "rdl" in ct or name_l.endswith(".rdl"):
        return ".rdl"
    if ".pbix" in cd or "pbix" in ct:
        return ".pbix"
    # Paginated often returns application/xml
    if "xml" in ct and "pbix" not in ct:
        return ".rdl"
    return ".pbix"


def export_report_bytes(
    access_token: str,
    workspace_id: str,
    report_id: str,
    timeout: int = 300,
) -> Dict[str, Any]:
    """
    Download report binary via Power BI Export API.
    Returns {ok, content, content_type, content_disposition, error, status_code}.
    """
    url = (
        f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}"
        f"/reports/{report_id}/Export"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/zip, application/octet-stream, */*",
    }
    print(f"   ⬇️ Export GET {url}")
    try:
        # connect timeout short; read can be large for PBIX
        resp = requests.get(url, headers=headers, timeout=(30, timeout))
    except requests.Timeout:
        print("   ❌ Export timed out")
        return {
            "ok": False,
            "error": f"Export timed out after {timeout}s. Report may be too large or API hung.",
            "status_code": 0,
        }
    except requests.RequestException as ex:
        print(f"   ❌ Export request error: {ex}")
        return {"ok": False, "error": f"Export request failed: {ex}", "status_code": 0}

    if resp.status_code != 200:
        detail = (resp.text or "")[:600]
        # Prefer Power BI JSON error message when present
        try:
            j = resp.json()
            err = j.get("error") or j
            if isinstance(err, dict):
                detail = err.get("message") or err.get("code") or detail
                if err.get("pbi.error"):
                    pe = err["pbi.error"]
                    detail = pe.get("message") or pe.get("code") or detail
        except Exception:
            pass
        print(f"   ❌ Export HTTP {resp.status_code}: {detail[:300]}")
        hint = ""
        if resp.status_code in (401, 403):
            hint = " Check Report.Read.All + Dataset.Read.All and workspace access."
        elif resp.status_code == 404:
            hint = " Report not found or Export not supported for this item type."
        elif resp.status_code == 400:
            hint = (
                " Often blocked for live/DirectQuery-only downloads, "
                "sensitivity labels, or reports that cannot be downloaded as PBIX."
            )
        return {
            "ok": False,
            "error": f"Export HTTP {resp.status_code}: {detail}{hint}",
            "status_code": resp.status_code,
        }

    content = resp.content or b""
    if not content:
        print("   ❌ Export empty body")
        return {"ok": False, "error": "Export returned empty body", "status_code": 200}

    print(
        f"   ✅ Export OK bytes={len(content)} "
        f"type={resp.headers.get('Content-Type')} "
        f"disp={(resp.headers.get('Content-Disposition') or '')[:80]}"
    )
    return {
        "ok": True,
        "content": content,
        "content_type": resp.headers.get("Content-Type") or "",
        "content_disposition": resp.headers.get("Content-Disposition") or "",
        "status_code": 200,
    }


def resolve_decomm_latest_folder(sp_client) -> Dict[str, Any]:
    """
    Under SHAREPOINT_DECOMM_FOLDER_PATH, pick the latest *created* child folder.
    """
    from catalog_service import catalog_config as cfg

    base = (getattr(cfg, "SHAREPOINT_DECOMM_FOLDER_PATH", None) or "").strip("/")
    if not base:
        return {"ok": False, "error": "SHAREPOINT_DECOMM_FOLDER_PATH not configured"}

    if sp_client._drive_id is None:
        sp_client.resolve_site_and_drive()

    latest = sp_client.latest_child_folder(base)
    if not latest or not latest.get("name"):
        return {
            "ok": False,
            "error": (
                f"No dated folders under SharePoint path '{base}'. "
                "Create a batch folder first (e.g. 2026-08-06)."
            ),
            "base": base,
        }
    name = latest["name"]
    path = f"{base}/{name}"
    return {
        "ok": True,
        "base": base,
        "batch_folder": name,
        "batch_path": path,
        "createdDateTime": latest.get("createdDateTime"),
        "webUrl": latest.get("webUrl"),
    }


def archive_report_to_sharepoint(
    *,
    access_token: str,
    workspace_id: str,
    workspace_name: str,
    report_id: str,
    report_name: str,
    folder_name: Optional[str] = None,
    folder_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Export report from Power BI and upload under:
      Report Decommission Activity/<latest>/Workspace/[Folder]/Report.ext

    folder_name: PBI workspace folder display name (optional).
    Does not create the dated batch root — uses the newest existing child.
    Creates workspace (and optional PBI folder) under that batch if missing.
    """
    from catalog_service.metadata_lib.sharepoint_client import SharePointClient

    if not access_token:
        return {"success": False, "error": "Not authenticated (missing Power BI token)"}
    if not workspace_id or not report_id:
        return {"success": False, "error": "workspace_id and report_id are required"}

    # 1) Export binary
    print(f"   ⬇️ Starting Power BI Export for report {report_id[:8]}…")
    exp = export_report_bytes(access_token, workspace_id, report_id)
    if not exp.get("ok"):
        err = exp.get("error") or "Export failed"
        print(f"   ❌ Archive aborted at export: {err}")
        return {
            "success": False,
            "error": err,
            "status_code": exp.get("status_code"),
            "stage": "export",
            "hint": (
                "Export can fail for live-connected reports, sensitivity labels, "
                "or missing Report.Read.All + Dataset.Read.All."
            ),
        }

    ext = _guess_ext(
        exp.get("content_type") or "",
        exp.get("content_disposition") or "",
        report_name or "",
    )
    safe_report = _safe_segment(report_name, "Report")
    # Avoid double extension
    if safe_report.lower().endswith((".pbix", ".rdl")):
        file_name = safe_report
    else:
        file_name = f"{safe_report}{ext}"

    # 2) Latest decomm batch folder
    print("   📂 Resolving SharePoint decommission latest batch folder…")
    try:
        sp = SharePointClient()
        batch = resolve_decomm_latest_folder(sp)
    except Exception as ex:
        logger.exception("SharePoint resolve failed")
        print(f"   ❌ SharePoint resolve failed: {ex}")
        return {
            "success": False,
            "error": f"SharePoint error: {ex}",
            "stage": "sharepoint_resolve",
        }

    if not batch.get("ok"):
        err = batch.get("error") or "Could not resolve decommission folder"
        print(f"   ❌ {err}")
        return {
            "success": False,
            "error": err,
            "base": batch.get("base"),
            "stage": "sharepoint_batch",
        }
    print(
        f"   ✅ Batch folder: {batch.get('batch_folder')} "
        f"(created {batch.get('createdDateTime')})"
    )

    batch_path = batch["batch_path"]
    ws_seg = _safe_segment(workspace_name or workspace_id, "Workspace")
    parts = [batch_path, ws_seg]
    pbi_folder = (folder_name or "").strip()
    if pbi_folder and pbi_folder not in ("__ROOT__", "Root Directory (Uncategorized)"):
        parts.append(_safe_segment(pbi_folder, "Folder"))

    remote_folder = "/".join(parts)
    remote_file = f"{remote_folder}/{file_name}"

    # 3) Write temp file + upload (ensure_folder on parents)
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(suffix=ext, prefix="pbi_archive_")
        os.close(fd)
        tmp_path = Path(tmp_name)
        tmp_path.write_bytes(exp["content"])
        size = tmp_path.stat().st_size

        logger.info(
            "Archive upload %s (%.1f KB) -> %s",
            file_name,
            size / 1024.0,
            remote_file,
        )
        item = sp.upload_file(tmp_path, remote_file)
        web_url = (item or {}).get("webUrl") or ""
        return {
            "success": True,
            "fileName": file_name,
            "remotePath": remote_file,
            "batchFolder": batch.get("batch_folder"),
            "batchPath": batch_path,
            "workspaceFolder": ws_seg,
            "pbiFolder": pbi_folder or None,
            "sizeBytes": size,
            "webUrl": web_url,
            "contentType": exp.get("content_type"),
        }
    except Exception as ex:
        logger.exception("Archive upload failed")
        return {"success": False, "error": f"Upload failed: {ex}", "remotePath": remote_file}
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
