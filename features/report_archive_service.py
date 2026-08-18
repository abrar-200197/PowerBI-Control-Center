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

# Central Analytics team (Download / archive button).
# SSO may present either @ashleyfurnitureindia.com or @ashleyfurniture.com —
# we allow both domains for every alias below.
_ARCHIVE_LOCAL_PARTS = (
    "mahmed",
    "pshivanandam",
    "aramalingam",
    "mthanapathi",
    "ksambasivam",
    "danandkumar",
    "jravikumar",
    "kviswanathan",
    "ychandran",
    "nkathiresan",
)
_ARCHIVE_DOMAINS = (
    "ashleyfurnitureindia.com",
    "ashleyfurniture.com",
)


def _norm_upn(s: str) -> str:
    return (s or "").strip().lower()


def _expand_aliases(local: str) -> Set[str]:
    local = _norm_upn(local).split("@")[0]
    if not local:
        return set()
    return {f"{local}@{d}" for d in _ARCHIVE_DOMAINS}


def archive_allowed_upns() -> Set[str]:
    """UPNs allowed to see/use Archive Download button (both company domains)."""
    base: Set[str] = set()
    for local in _ARCHIVE_LOCAL_PARTS:
        base |= _expand_aliases(local)

    # Env override / extras: comma-separated full UPNs or local parts
    extra = os.getenv("REPORT_ARCHIVE_ALLOWED_UPNS") or ""
    for u in extra.split(","):
        u = u.strip()
        if not u:
            continue
        if "@" in u:
            base.add(_norm_upn(u))
            # also twin domain for same local part
            base |= _expand_aliases(u)
        else:
            base |= _expand_aliases(u)

    # Merge usage-exclude roster (covers both domains if listed there)
    try:
        from catalog_service import catalog_config as cfg
        for u in getattr(cfg, "USAGE_EXCLUDE_USER_UPNS", []) or []:
            nu = _norm_upn(u)
            if not nu:
                continue
            base.add(nu)
            base |= _expand_aliases(nu)
    except Exception:
        pass
    return base


def user_can_archive(email_or_upn: Optional[str]) -> bool:
    """
    True if signed-in UPN is on the Central Analytics archive allow-list.
    Matches case-insensitively; accepts either ashleyfurniture.com or
    ashleyfurnitureindia.com for the same local part.
    """
    if not email_or_upn:
        return False
    upn = _norm_upn(email_or_upn)
    allowed = archive_allowed_upns()
    if upn in allowed:
        return True
    # Local-part fallback (handles rare tenant suffix variants)
    local = upn.split("@")[0] if "@" in upn else upn
    if local and any(a.split("@")[0] == local for a in allowed):
        # Only trust local-part match for known Ashley domains
        if upn.endswith("@ashleyfurniture.com") or upn.endswith("@ashleyfurnitureindia.com"):
            return True
    return False


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


def _parse_export_error_body(resp: requests.Response) -> str:
    """Best-effort parse of Power BI Export error payload."""
    detail = (getattr(resp, "text", None) or "")[:800]
    try:
        j = resp.json()
    except Exception:
        return detail or f"HTTP {getattr(resp, 'status_code', '?')}"
    if not isinstance(j, dict):
        return detail
    # Shapes: {Message}, {error:{message,code,pbi.error}}, {error:{message}}
    if j.get("Message"):
        detail = str(j.get("Message"))
    err = j.get("error") or j
    if isinstance(err, dict):
        detail = (
            err.get("message")
            or err.get("Message")
            or err.get("code")
            or detail
        )
        pe = err.get("pbi.error") or err.get("pbiError")
        if isinstance(pe, dict):
            detail = pe.get("message") or pe.get("code") or detail
            details = pe.get("details")
            if isinstance(details, list) and details:
                bits = []
                for d in details[:3]:
                    if isinstance(d, dict):
                        bits.append(
                            str(d.get("message") or d.get("detail") or d.get("code") or d)
                        )
                    else:
                        bits.append(str(d))
                if bits:
                    detail = f"{detail} ({'; '.join(bits)})"
    return str(detail or "")[:600]


def _export_once(
    access_token: str,
    url: str,
    *,
    timeout: int,
    attempt: int,
    max_attempts: int,
) -> Dict[str, Any]:
    """Single streaming Export attempt."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/zip, application/octet-stream, application/xml, */*",
        # Avoid intermediate caches on App Service / proxies
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    print(f"   ⬇️ Export GET attempt {attempt}/{max_attempts}: {url}")
    try:
        # Stream large PBIX — buffering whole body can OOM / break on App Service
        with requests.get(
            url,
            headers=headers,
            timeout=(60, timeout),
            stream=True,
        ) as resp:
            status = resp.status_code
            if status != 200:
                # Consume a little of the body for the error message
                try:
                    # Prefer decoded text for JSON errors (small)
                    _ = resp.content  # materialize small error body
                except Exception:
                    pass
                detail = _parse_export_error_body(resp)
                print(f"   ❌ Export HTTP {status}: {detail[:300]}")
                return {
                    "ok": False,
                    "error": f"Export HTTP {status}: {detail}",
                    "status_code": status,
                    "retryable": status in (408, 429, 500, 502, 503, 504),
                }

            chunks: list[bytes] = []
            total = 0
            # 1 MB chunks; no hard cap — large models can exceed 1GB
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total and total % (25 * 1024 * 1024) < 1024 * 1024:
                    print(f"      … downloaded {total / (1024 * 1024):.1f} MB")

            content = b"".join(chunks)
            if not content:
                print("   ❌ Export empty body")
                return {
                    "ok": False,
                    "error": "Export returned empty body",
                    "status_code": 200,
                    "retryable": True,
                }

            ctype = resp.headers.get("Content-Type") or ""
            cdisp = resp.headers.get("Content-Disposition") or ""
            print(
                f"   ✅ Export OK bytes={len(content)} "
                f"type={ctype} disp={cdisp[:80]}"
            )
            return {
                "ok": True,
                "content": content,
                "content_type": ctype,
                "content_disposition": cdisp,
                "status_code": 200,
            }
    except requests.Timeout:
        print(f"   ❌ Export timed out after {timeout}s")
        return {
            "ok": False,
            "error": (
                f"Export timed out after {timeout}s. "
                "Large reports can take several minutes — retry or download from Power BI Service."
            ),
            "status_code": 0,
            "retryable": True,
        }
    except requests.RequestException as ex:
        print(f"   ❌ Export request error: {ex}")
        return {
            "ok": False,
            "error": f"Export request failed: {ex}",
            "status_code": 0,
            "retryable": True,
        }


def export_report_bytes(
    access_token: str,
    workspace_id: str,
    report_id: str,
    timeout: int = 900,
    max_attempts: int = 3,
    fallback_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Download report binary via Power BI Export API with retries + streaming.

    Power BI Service UI download uses a different pipeline than REST Export;
    REST occasionally returns generic HTTP 500 for large / flaky models.
    We retry transient failures and optionally try a second token (SP vs user).

    Returns {ok, content, content_type, content_disposition, error, status_code}.
    """
    import time as _time

    # Allow env override for very large reports on slow networks
    try:
        timeout = int(os.getenv("PBI_EXPORT_TIMEOUT_SEC") or timeout)
    except ValueError:
        pass
    try:
        max_attempts = int(os.getenv("PBI_EXPORT_MAX_ATTEMPTS") or max_attempts)
    except ValueError:
        pass
    max_attempts = max(1, min(max_attempts, 5))
    timeout = max(120, timeout)

    url = (
        f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}"
        f"/reports/{report_id}/Export"
    )

    tokens: list[tuple[str, str]] = []
    if access_token:
        tokens.append(("primary", access_token))
    if fallback_token and fallback_token != access_token:
        tokens.append(("fallback", fallback_token))

    if not tokens:
        return {"ok": False, "error": "No access token for Export", "status_code": 0}

    last: Dict[str, Any] = {"ok": False, "error": "Export failed", "status_code": 0}

    for token_label, token in tokens:
        for attempt in range(1, max_attempts + 1):
            result = _export_once(
                token,
                url,
                timeout=timeout,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            if result.get("ok"):
                if token_label != "primary" or attempt > 1:
                    result["via"] = f"{token_label}/attempt{attempt}"
                return result

            last = result
            status = int(result.get("status_code") or 0)
            retryable = bool(result.get("retryable"))

            # Auth failures: try fallback token immediately (don't burn retries)
            if status in (401, 403):
                print(f"   ⚠️ Export auth failed with {token_label} token (HTTP {status})")
                break

            # Permanent client errors (except flaky 400s on some large models)
            if status == 404:
                break
            if status == 400 and attempt >= 2:
                break

            if not retryable and status not in (0, 400, 500):
                break

            if attempt < max_attempts:
                # Backoff: 2s, 5s, 10s…
                delay = min(30, 2 * (2 ** (attempt - 1))) + (attempt * 0.5)
                print(f"   🔁 Retrying export in {delay:.1f}s (token={token_label})…")
                _time.sleep(delay)

    # Enrich final error with actionable hints
    status = int(last.get("status_code") or 0)
    detail = last.get("error") or "Export failed"
    hint = ""
    if status in (401, 403):
        hint = (
            " Check Report.Read.All + Dataset.Read.All (delegated) and workspace Member/"
            "Contributor access. Download in Service uses your interactive session."
        )
    elif status == 404:
        hint = " Report not found or Export not supported for this item type."
    elif status == 400:
        hint = (
            " Often blocked for live/DirectQuery-only, CDM, sensitivity labels, "
            "or reports that cannot be downloaded as PBIX via REST."
        )
    elif status == 500 or status == 0:
        hint = (
            " Power BI REST Export returned a transient/server error (common on large "
            "PBIX). Retries exhausted. Try again later, or download from Power BI Service "
            "File → Download this file (uses a different pipeline)."
        )
    last["error"] = f"{detail}{hint}"
    last["hint"] = hint.strip() if hint else last.get("hint")
    return last


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
    fallback_token: Optional[str] = None,
    export_timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Export report from Power BI and upload under:
      Report Decommission Activity/<latest>/Workspace/[Folder]/Report.ext

    folder_name: PBI workspace folder display name (optional).
    Does not create the dated batch root — uses the newest existing child.
    Creates workspace (and optional PBI folder) under that batch if missing.

    fallback_token: optional second Power BI token (e.g. service principal when
    primary is user-delegated) used if Export fails with auth/transient errors.
    """
    from catalog_service.metadata_lib.sharepoint_client import SharePointClient

    if not access_token:
        return {"success": False, "error": "Not authenticated (missing Power BI token)"}
    if not workspace_id or not report_id:
        return {"success": False, "error": "workspace_id and report_id are required"}

    # 1) Export binary (streamed + retries; dual-token when provided)
    print(f"   ⬇️ Starting Power BI Export for report {report_id[:8]}…")
    timeout = 900
    if export_timeout:
        try:
            timeout = int(export_timeout)
        except (TypeError, ValueError):
            pass
    exp = export_report_bytes(
        access_token,
        workspace_id,
        report_id,
        timeout=timeout,
        fallback_token=fallback_token,
    )
    if not exp.get("ok"):
        err = exp.get("error") or "Export failed"
        print(f"   ❌ Archive aborted at export: {err}")
        return {
            "success": False,
            "error": err,
            "status_code": exp.get("status_code"),
            "stage": "export",
            "hint": exp.get("hint") or (
                "Export can fail for live-connected reports, sensitivity labels, "
                "large PBIX (REST 500), or missing Report.Read.All + Dataset.Read.All. "
                "Service UI download uses a different pipeline and may still work."
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
