"""
Operational snapshot for Control Center catalog.

Builds:
  - Dataset refresh snapshot (last refresh, status, schedule, type)
  - Incremental 60-day usage (Activity Events) with day-bucket state

Designed for scheduled runs (e.g. every 6 hours):
  - De-dupe datasets across reports
  - Skip usage days already captured
  - Merge + drop buckets older than lookback window
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import msal
import requests

from catalog_service import catalog_config as cfg
from powerbi_connector import (
    resolve_dataset_refresh_info,
    _empty_refresh_info,
    merge_refresh_candidates,
    refresh_info_from_admin_last_refresh,
    refresh_info_from_content_modified,
    days_since_refresh,
)

logger = logging.getLogger("ops_snapshot")

PBI_SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]
USAGE_LOOKBACK_DAYS = int(getattr(cfg, "USAGE_LOOKBACK_DAYS", None) or 60)
REFRESH_WORKERS = int(getattr(cfg, "OPS_REFRESH_WORKERS", None) or 8)
USAGE_DAY_WORKERS = int(getattr(cfg, "OPS_USAGE_DAY_WORKERS", None) or 6)
HTTP_TIMEOUT = int(getattr(cfg, "OPS_HTTP_TIMEOUT_SEC", None) or 30)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _parse_day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


class OpsAuth:
    """Service-principal token for admin + dataset APIs."""

    def __init__(self) -> None:
        self.tenant_id = cfg.TENANT_ID
        self.client_id = cfg.CLIENT_ID
        self.client_secret = cfg.CLIENT_SECRET
        self._token: Optional[str] = None
        self._expires_at = 0.0
        if not all([self.tenant_id, self.client_id, self.client_secret]):
            raise RuntimeError("TENANT_ID / CLIENT_ID / CLIENT_SECRET required for ops snapshot")

    def token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._expires_at - 300:
            return self._token
        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret,
        )
        result = app.acquire_token_for_client(scopes=PBI_SCOPE)
        if "access_token" not in result:
            raise RuntimeError(
                f"Ops auth failed: {result.get('error')} — {result.get('error_description')}"
            )
        self._token = result["access_token"]
        self._expires_at = time.time() + int(result.get("expires_in", 3600))
        return self._token

    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token()}",
            "Content-Type": "application/json",
        }


def collect_dataset_targets(catalog: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Unique datasets from workspace_catalog with preferred workspace id.
    Returns [{datasetId, workspaceId, datasetName}]
    """
    seen: Set[str] = set()
    out: List[Dict[str, str]] = []
    datasets_map = catalog.get("datasets") or {}

    # Prefer datasets map (has workspaceId)
    for ds_id, ds in datasets_map.items():
        if not ds_id or ds_id in seen:
            continue
        seen.add(ds_id)
        out.append({
            "datasetId": ds_id,
            "workspaceId": ds.get("workspaceId") or "",
            "datasetName": ds.get("name") or "",
        })

    # Also harvest from reports in case dataset missing from map
    for ws in catalog.get("workspaces") or []:
        ws_id = ws.get("id") or ""
        for r in ws.get("reports") or []:
            ds_id = r.get("datasetId")
            if not ds_id or ds_id in seen:
                continue
            seen.add(ds_id)
            out.append({
                "datasetId": ds_id,
                "workspaceId": ws_id,
                "datasetName": "",
            })
    return out


def fetch_admin_refreshables_map(
    auth: Optional[OpsAuth] = None,
    *,
    page_size: int = 1000,
    max_pages: int = 50,
) -> Dict[str, Dict[str, Any]]:
    """
    Admin capacities/refreshables → {datasetId: refresh_info}.

    Complements GET .../refreshes (which never returns OneDrive-tab history).
    When admin lastRefresh has a timestamp we merge it as an alternate source
    so the UI can show whichever of scheduled vs admin is latest.
    Failures are non-fatal (empty map) so ops never breaks.
    """
    auth = auth or OpsAuth()
    headers = auth.headers()
    out: Dict[str, Dict[str, Any]] = {}
    base = "https://api.powerbi.com/v1.0/myorg/admin/capacities/refreshables"
    skip = 0
    pages = 0
    try:
        while pages < max_pages:
            pages += 1
            url = f"{base}?$top={int(page_size)}&$skip={int(skip)}"
            resp = requests.get(url, headers=headers, timeout=min(HTTP_TIMEOUT, 60))
            if resp.status_code in (401, 403):
                logger.warning(
                    "Admin refreshables unauthorized (HTTP %s) — SP needs Fabric admin / tenant read",
                    resp.status_code,
                )
                break
            if resp.status_code != 200:
                logger.warning(
                    "Admin refreshables HTTP %s: %s",
                    resp.status_code,
                    (resp.text or "")[:200],
                )
                break
            payload = resp.json() if resp.content else {}
            rows = payload.get("value") or []
            if not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                ds_id = row.get("id") or ""
                if not ds_id:
                    continue
                ws_id = None
                grp = row.get("group") or {}
                if isinstance(grp, dict):
                    ws_id = grp.get("id")
                info = refresh_info_from_admin_last_refresh(
                    row.get("lastRefresh") or {},
                    dataset_workspace_id=ws_id,
                )
                if not info:
                    continue
                # Prefer schedule from admin when present
                sched = row.get("refreshSchedule") or {}
                if isinstance(sched, dict) and sched:
                    enabled = bool(sched.get("enabled"))
                    days = sched.get("days") or []
                    times = sched.get("times") or []
                    if enabled and days and times:
                        info["refresh_schedule"] = (
                            f"Scheduled: {', '.join(days)} at {', '.join(times)}"
                        )
                        info["schedule_days"] = list(days)
                        info["schedule_times"] = list(times)
                    elif enabled:
                        info["refresh_schedule"] = "Enabled (incomplete)"
                    elif not info.get("refresh_schedule") or info.get("refresh_schedule") == "Unknown":
                        info["refresh_schedule"] = "No Schedule"
                owners = row.get("configuredBy") or []
                if isinstance(owners, list) and owners and not info.get("dataset_owner"):
                    info["dataset_owner"] = owners[0]
                info["datasetName"] = row.get("name") or info.get("datasetName")
                info["refresh_source"] = info.get("refresh_source") or "admin"
                out[str(ds_id)] = info
            if len(rows) < page_size:
                break
            skip += len(rows)
        logger.info("Admin refreshables loaded: %s dataset(s) with lastRefresh", len(out))
    except Exception as exc:
        logger.warning("Admin refreshables failed (non-fatal): %s", exc)
    return out


def build_refresh_snapshot(
    targets: List[Dict[str, str]],
    auth: Optional[OpsAuth] = None,
    workers: int = REFRESH_WORKERS,
    catalog: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Fetch refresh info for unique datasets (service principal).

    For each dataset, pick LATEST of:
      1) Scheduled / OnDemand / ViaApi history (GET .../refreshes)
      2) Admin refreshables lastRefresh (bulk)
      3) Content-modified fallback (report/dataset timestamps) when history empty

    OneDrive-tab history is NOT returned by Microsoft REST APIs — we document that
    in refresh_note when still blank after all sources.
    """
    auth = auth or OpsAuth()
    headers = auth.headers()
    results: Dict[str, Any] = {}
    if not targets:
        return {
            "generatedAt": _utc_now().isoformat(),
            "datasetCount": 0,
            "datasets": {},
            "sources": {"scheduled": 0, "admin": 0, "content_modified": 0},
        }

    # Optional: newest report modifiedDateTime per dataset (content-modified fallback)
    report_mod_by_ds: Dict[str, Dict[str, Any]] = {}
    if isinstance(catalog, dict):
        for ws in catalog.get("workspaces") or []:
            for r in ws.get("reports") or []:
                ds_id = r.get("datasetId") or ""
                if not ds_id:
                    continue
                mod = (
                    r.get("modifiedDateTime")
                    or r.get("modified_date_time")
                    or r.get("modifiedDate")
                    or r.get("modified_date")
                )
                if not mod:
                    continue
                prev = report_mod_by_ds.get(ds_id)
                if not prev:
                    report_mod_by_ds[ds_id] = {
                        "modifiedDateTime": mod,
                        "name": r.get("name"),
                    }
                    continue
                # keep newest
                from powerbi_connector import parse_refresh_timestamp
                cur_dt = parse_refresh_timestamp(mod)
                prev_dt = parse_refresh_timestamp(prev.get("modifiedDateTime"))
                if cur_dt and (prev_dt is None or cur_dt > prev_dt):
                    report_mod_by_ds[ds_id] = {
                        "modifiedDateTime": mod,
                        "name": r.get("name"),
                    }

    admin_map = fetch_admin_refreshables_map(auth=auth)

    def _one(t: Dict[str, str]) -> Tuple[str, Dict[str, Any]]:
        ds_id = t["datasetId"]
        ws_id = t.get("workspaceId") or None
        try:
            scheduled = resolve_dataset_refresh_info(
                headers=headers,
                workspace_id=ws_id or "",
                dataset_id=ds_id,
                dataset_workspace_id=ws_id,
                history_top=5,
                timeout=min(HTTP_TIMEOUT, 12),
            )
        except Exception as exc:
            logger.warning("Refresh snapshot failed %s: %s", ds_id[:8], exc)
            scheduled = _empty_refresh_info(
                refresh_schedule="Error",
                last_refresh_status="Error",
                refresh_type="error",
                refresh_note=str(exc),
                dataset_workspace_id=ws_id,
            )

        admin_side = admin_map.get(ds_id) or {}
        # Always offer report-modified content fallback when we have catalog stamps.
        # merge_refresh_candidates keeps TRUE history winners; content_modified only
        # fills gaps and beats weak content_created (dataset createdDate) stamps.
        content_side = None
        report_meta = report_mod_by_ds.get(ds_id)
        sched_src = str(scheduled.get("refresh_source") or "").lower()
        admin_has = bool((admin_side or {}).get("last_refreshed"))
        sched_is_true = sched_src in (
            "scheduled", "ondemand", "on_demand", "viaapi", "via_api", "admin", "history", "api",
        ) or bool(scheduled.get("history_refresh_type"))
        need_content = (
            report_meta
            and not admin_has
            and (
                not scheduled.get("last_refreshed")
                or not sched_is_true  # e.g. content_created from dataset createdDate
            )
        )
        if need_content:
            content_side = refresh_info_from_content_modified(
                None,
                report_meta,
                dataset_workspace_id=ws_id,
            )

        info = merge_refresh_candidates(scheduled, admin_side, content_side or {})
        info["datasetName"] = t.get("datasetName") or info.get("datasetName") or ""
        info["days_since_refresh"] = days_since_refresh(info.get("last_refreshed"))
        return ds_id, info

    logger.info(
        "Ops refresh snapshot: %s unique datasets (workers=%s, admin_hits=%s)",
        len(targets),
        workers,
        len(admin_map),
    )
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_one, t): t["datasetId"] for t in targets}
        for fut in as_completed(futs):
            ds_id, info = fut.result()
            results[ds_id] = info
            done += 1
            if done % 50 == 0 or done == len(targets):
                logger.info("  refresh progress %s/%s", done, len(targets))

    src_counts = {"scheduled": 0, "admin": 0, "content_modified": 0, "other": 0, "none": 0}
    for info in results.values():
        src = str((info or {}).get("refresh_source") or "none")
        if src in src_counts:
            src_counts[src] += 1
        elif (info or {}).get("last_refreshed"):
            src_counts["other"] += 1
        else:
            src_counts["none"] += 1

    return {
        "generatedAt": _utc_now().isoformat(),
        "datasetCount": len(results),
        "datasets": results,
        "sources": src_counts,
        "adminRefreshablesCount": len(admin_map),
    }


# ---------------------------------------------------------------------------
# Incremental usage (Activity Events)
# ---------------------------------------------------------------------------

def _empty_usage_state() -> Dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "lookbackDays": USAGE_LOOKBACK_DAYS,
        "days": {},  # day -> {report_views: {rid: count}, last_viewed: {rid: {timestamp, user}}}
        "updatedAt": None,
    }


def load_usage_state(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return _empty_usage_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get("days"), dict):
            data["days"] = {}
        return data
    except Exception as exc:
        logger.warning("Bad usage state %s: %s — starting fresh", path, exc)
        return _empty_usage_state()


def save_usage_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updatedAt"] = _utc_now().isoformat()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _prune_usage_days(state: Dict[str, Any], lookback: int = USAGE_LOOKBACK_DAYS) -> None:
    cutoff = _utc_now().date() - timedelta(days=lookback - 1)
    days = state.get("days") or {}
    keep = {}
    for day, payload in days.items():
        try:
            d = _parse_day(day).date()
        except Exception:
            continue
        if d >= cutoff:
            keep[day] = payload
    state["days"] = keep
    state["lookbackDays"] = lookback


def _fetch_activity_day(headers: Dict[str, str], day: str) -> Dict[str, Any]:
    """
    Fetch ViewReport events for one UTC day. Returns:
      {report_views: {rid: n}, last_viewed: {rid: {timestamp, user}}}

    Matches the proven PowerBI-Crash-Test Activity Events pattern:
      - $filter=Activity eq 'ViewReport' (server-side filter; much more reliable)
      - start/end as plain ISO without trailing Z (API quirk)
      - full-day window 00:00:00 → 23:59:59
      - pagination via continuationUri (full URL), not token-only query
    """
    # Plain ISO without Z — same format as working crash-test tool
    start = f"{day}T00:00:00"
    end = f"{day}T23:59:59"
    # Filter reduces payload and avoids client-side Activity mismatches
    first_url = (
        "https://api.powerbi.com/v1.0/myorg/admin/activityevents"
        f"?startDateTime='{start}'&endDateTime='{end}'"
        f"&$filter=Activity eq 'ViewReport'"
    )

    report_views: Dict[str, int] = {}
    last_viewed: Dict[str, Dict[str, str]] = {}
    pages = 0
    url: Optional[str] = first_url
    retries_429 = 0

    while url and pages < 500:
        pages += 1
        try:
            resp = requests.get(url, headers=headers, timeout=max(HTTP_TIMEOUT, 60))
        except requests.RequestException as exc:
            logger.warning("Activity day %s request error: %s", day, exc)
            break

        if resp.status_code == 429:
            retries_429 += 1
            if retries_429 > 8:
                logger.warning("Activity day %s too many 429s", day)
                break
            wait = float(resp.headers.get("Retry-After", min(30, 2 * retries_429)))
            time.sleep(wait)
            pages -= 1  # don't count throttle retries as pages
            continue
        retries_429 = 0

        if resp.status_code in (401, 403):
            logger.error(
                "Activity Events forbidden (%s) for %s: %s",
                resp.status_code, day, (resp.text or "")[:240],
            )
            break
        if resp.status_code != 200:
            logger.warning(
                "Activity day %s HTTP %s: %s", day, resp.status_code, (resp.text or "")[:240]
            )
            break

        data = resp.json() or {}
        entities = data.get("activityEventEntities") or []
        for activity in entities:
            # Server already filters ViewReport; keep client check as safety net
            act = (activity.get("Activity") or "").strip()
            if act and act != "ViewReport":
                continue
            rid = activity.get("ReportId") or activity.get("ArtifactId")
            if not rid:
                continue
            rid = str(rid)
            report_views[rid] = report_views.get(rid, 0) + 1
            ts = activity.get("CreationTime") or ""
            user = (
                activity.get("UserId")
                or activity.get("UserKey")
                or activity.get("UserEmail")
                or "Unknown"
            )
            prev = last_viewed.get(rid)
            if not prev or (ts and ts > (prev.get("timestamp") or "")):
                last_viewed[rid] = {"timestamp": ts, "user": str(user)}

        # Prefer full continuationUri (working crash-test path); fall back to token
        cont_uri = data.get("continuationUri")
        cont_tok = data.get("continuationToken")
        if cont_uri:
            url = cont_uri
        elif cont_tok:
            # Some tenants only return the token — quote form per API docs
            tok = str(cont_tok).strip().strip("'")
            url = (
                "https://api.powerbi.com/v1.0/myorg/admin/activityevents"
                f"?continuationToken='{tok}'"
            )
        else:
            url = None

    logger.info(
        "Activity day %s: pages=%s view_events=%s unique_reports=%s",
        day, pages, sum(report_views.values()), len(report_views),
    )
    return {
        "report_views": report_views,
        "last_viewed": last_viewed,
        "pages": pages,
        "eventCount": sum(report_views.values()),
    }


def build_usage_snapshot_incremental(
    state_path: Path,
    auth: Optional[OpsAuth] = None,
    lookback_days: int = USAGE_LOOKBACK_DAYS,
    force_full: bool = False,
    day_workers: int = USAGE_DAY_WORKERS,
) -> Dict[str, Any]:
    """
    Incremental Activity Events usage.
    - Loads prior day buckets from state_path
    - Fetches only missing days in the lookback window (and always re-fetches today)
    - Aggregates report_views + last_viewed over the window
    """
    auth = auth or OpsAuth()
    headers = auth.headers()
    state = _empty_usage_state() if force_full else load_usage_state(state_path)
    state["lookbackDays"] = lookback_days
    _prune_usage_days(state, lookback_days)

    today = _utc_now().date()
    needed_days = [
        (today - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(lookback_days)
    ]
    # Always refresh "today"; skip other days already present unless force_full
    to_fetch = []
    for day in needed_days:
        if force_full or day == needed_days[0] or day not in (state.get("days") or {}):
            to_fetch.append(day)

    logger.info(
        "Ops usage: lookback=%s existing_days=%s fetch=%s force_full=%s",
        lookback_days,
        len(state.get("days") or {}),
        len(to_fetch),
        force_full,
    )

    if to_fetch:
        with ThreadPoolExecutor(max_workers=max(1, day_workers)) as ex:
            futs = {ex.submit(_fetch_activity_day, headers, d): d for d in to_fetch}
            for fut in as_completed(futs):
                day = futs[fut]
                try:
                    payload = fut.result()
                    state.setdefault("days", {})[day] = {
                        "report_views": payload.get("report_views") or {},
                        "last_viewed": payload.get("last_viewed") or {},
                        "fetchedAt": _utc_now().isoformat(),
                        "pages": payload.get("pages"),
                    }
                    logger.info(
                        "  usage day %s views_reports=%s",
                        day,
                        len(payload.get("report_views") or {}),
                    )
                except Exception as exc:
                    logger.warning("Usage day %s failed: %s", day, exc)

    _prune_usage_days(state, lookback_days)
    save_usage_state(state_path, state)

    # Aggregate
    total_views: Dict[str, int] = {}
    last_viewed: Dict[str, Dict[str, str]] = {}
    for day in needed_days:
        bucket = (state.get("days") or {}).get(day) or {}
        for rid, cnt in (bucket.get("report_views") or {}).items():
            total_views[rid] = total_views.get(rid, 0) + int(cnt or 0)
        for rid, lv in (bucket.get("last_viewed") or {}).items():
            ts = (lv or {}).get("timestamp") or ""
            prev = last_viewed.get(rid)
            if not prev or ts > (prev.get("timestamp") or ""):
                last_viewed[rid] = {
                    "timestamp": ts,
                    "user": (lv or {}).get("user") or "Unknown",
                }

    return {
        "generatedAt": _utc_now().isoformat(),
        "lookbackDays": lookback_days,
        "daysFetchedThisRun": to_fetch,
        "daysRetained": list((state.get("days") or {}).keys()),
        "reportCount": len(total_views),
        "report_views": total_views,
        "last_viewed": last_viewed,
    }


def _days_since(iso_ts: Optional[str], as_of: Optional[datetime] = None) -> Optional[int]:
    if not iso_ts:
        return None
    try:
        s = iso_ts
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = as_of or _utc_now()
        return max(0, (now - dt).days)
    except Exception:
        return None


def enrich_workspace_catalog(
    catalog: Dict[str, Any],
    refresh_snapshot: Optional[Dict[str, Any]] = None,
    usage_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Merge ops fields into workspace_catalog reports (+ dataset map).
    Mutates a deep-enough copy structure for JSON dump.
    """
    as_of = _utc_now()
    refresh_map = (refresh_snapshot or {}).get("datasets") or {}
    views = (usage_snapshot or {}).get("report_views") or {}
    last_viewed = (usage_snapshot or {}).get("last_viewed") or {}

    # Enrich datasets map
    datasets = catalog.get("datasets") or {}
    for ds_id, ds in list(datasets.items()):
        info = refresh_map.get(ds_id) or {}
        if info:
            ds["last_refreshed"] = info.get("last_refreshed")
            ds["last_refresh_status"] = info.get("last_refresh_status")
            ds["refresh_schedule"] = info.get("refresh_schedule")
            ds["refresh_type"] = info.get("refresh_type")
            ds["refresh_note"] = info.get("refresh_note")
            ds["refresh_source"] = info.get("refresh_source")
            ds["history_refresh_type"] = info.get("history_refresh_type")
            ds["days_since_refresh"] = _days_since(info.get("last_refreshed"), as_of)
            ds["is_refreshable"] = info.get("is_refreshable")
            ds["dataset_owner"] = info.get("dataset_owner")

    # Enrich reports under each workspace
    for ws in catalog.get("workspaces") or []:
        for r in ws.get("reports") or []:
            ds_id = r.get("datasetId") or ""
            info = refresh_map.get(ds_id) or {}
            rid = r.get("id") or ""
            r["last_refreshed"] = info.get("last_refreshed")
            r["last_refresh_status"] = info.get("last_refresh_status")
            r["refresh_schedule"] = info.get("refresh_schedule")
            r["refresh_type"] = info.get("refresh_type")
            r["refresh_note"] = info.get("refresh_note")
            r["refresh_source"] = info.get("refresh_source")
            r["history_refresh_type"] = info.get("history_refresh_type")
            r["days_since_refresh"] = _days_since(info.get("last_refreshed"), as_of)
            r["dataset_owner"] = info.get("dataset_owner")
            r["dataset_workspace_id"] = info.get("dataset_workspace_id")
            if ds_id and ds_id in datasets:
                r["datasetName"] = r.get("datasetName") or datasets[ds_id].get("name")
                if not r.get("dataset_workspace_id"):
                    r["dataset_workspace_id"] = datasets[ds_id].get("workspaceId")
            # usage
            if rid in views:
                r["view_count"] = int(views.get(rid) or 0)
            else:
                r["view_count"] = 0 if usage_snapshot else None
            if rid in last_viewed:
                r["last_viewed"] = last_viewed[rid]
            elif usage_snapshot is not None:
                r["last_viewed"] = None
            r["ops_from_catalog"] = True
            r["refresh_pending"] = False

    catalog["opsEnrichedAt"] = as_of.isoformat()
    catalog["ops"] = {
        "refreshGeneratedAt": (refresh_snapshot or {}).get("generatedAt"),
        "usageGeneratedAt": (usage_snapshot or {}).get("generatedAt"),
        "usageLookbackDays": (usage_snapshot or {}).get("lookbackDays"),
        "refreshDatasetCount": (refresh_snapshot or {}).get("datasetCount"),
        "usageReportCount": (usage_snapshot or {}).get("reportCount"),
    }
    return catalog


def run_ops_enrichment(
    catalog: Dict[str, Any],
    *,
    out_dir: Path,
    skip_refresh: bool = False,
    skip_usage: bool = False,
    force_full_usage: bool = False,
    auth: Optional[OpsAuth] = None,
) -> Dict[str, Path]:
    """
    Build refresh + usage snapshots, enrich catalog, write JSON artifacts.
    out_dir should be the run folder or latest folder parent; writes into out_dir and returns paths.
    """
    auth = auth or OpsAuth()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "latest" if (out_dir / "latest").exists() or out_dir.name != "latest" else out_dir
    # Normalize: prefer writing to out_dir directly when it IS latest
    write_dir = out_dir
    write_dir.mkdir(parents=True, exist_ok=True)

    paths: Dict[str, Path] = {}
    refresh_snap = None
    usage_snap = None

    if not skip_refresh:
        targets = collect_dataset_targets(catalog)
        refresh_snap = build_refresh_snapshot(targets, auth=auth, catalog=catalog)
        p = write_dir / "refresh_snapshot.json"
        p.write_text(json.dumps(refresh_snap, ensure_ascii=False, indent=2), encoding="utf-8")
        paths["refresh_snapshot"] = p
        logger.info(
            "Wrote %s (%s datasets, sources=%s)",
            p,
            refresh_snap.get("datasetCount"),
            refresh_snap.get("sources"),
        )

    if not skip_usage:
        # Keep usage incremental state inside the temp write_dir (not a permanent
        # local catalog path). run_catalog_extract seeds this from SharePoint when present.
        state_path = write_dir / "usage_state.json"
        try:
            usage_snap = build_usage_snapshot_incremental(
                state_path,
                auth=auth,
                force_full=force_full_usage,
            )
            # embed day buckets into snapshot so next ops-only run can reload from SharePoint
            if state_path.is_file():
                try:
                    usage_snap["usageState"] = json.loads(state_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            p = write_dir / "usage_snapshot.json"
            p.write_text(json.dumps(usage_snap, ensure_ascii=False, indent=2), encoding="utf-8")
            paths["usage_snapshot"] = p
            logger.info(
                "Wrote %s (reports=%s days_fetched=%s)",
                p,
                usage_snap.get("reportCount"),
                len(usage_snap.get("daysFetchedThisRun") or []),
            )
        except Exception as exc:
            logger.exception("Usage snapshot failed (continuing without views): %s", exc)
            usage_snap = None

    enriched = enrich_workspace_catalog(catalog, refresh_snap, usage_snap)
    p_cat = write_dir / "workspace_catalog.json"
    p_cat.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["workspace_catalog"] = p_cat

    # Compact ops summary for UI banner
    summary_ops = {
        "generatedAt": _utc_now().isoformat(),
        "refresh": (refresh_snap or {}).get("generatedAt"),
        "usage": (usage_snap or {}).get("generatedAt"),
        "refreshDatasetCount": (refresh_snap or {}).get("datasetCount"),
        "usageReportCount": (usage_snap or {}).get("reportCount"),
        "usageDaysFetched": (usage_snap or {}).get("daysFetchedThisRun"),
    }
    p_sum = write_dir / "ops_summary.json"
    p_sum.write_text(json.dumps(summary_ops, indent=2), encoding="utf-8")
    paths["ops_summary"] = p_sum
    return paths
