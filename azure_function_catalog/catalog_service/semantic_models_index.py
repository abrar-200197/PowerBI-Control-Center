"""
Tenant-wide semantic model (dataset) search index — published to the
dedicated SharePoint "agent" subfolder (separate from the main latest/
catalog publish tree).

Pure derivation from workspace_catalog.json, which is already enriched with
refresh + usage data by catalog_service.ops_snapshot.run_ops_enrichment()
during every --fresh / --ops-only run. No new Power BI API calls are made
here — this module only re-shapes data that already exists.

Isolation: this module is standalone. It is not imported by any other
catalog_service module, and it does not modify catalog_service.service,
thin_packs, or ops_snapshot. A failure anywhere in here must never break the
existing extract run — every public function catches its own exceptions and
returns a falsy result instead of raising.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from catalog_service.thin_packs import is_excluded_report_name
except Exception:  # pragma: no cover - thin_packs is always present in this repo
    def is_excluded_report_name(name: Optional[str]) -> bool:
        return False

logger = logging.getLogger(__name__)

OUTPUT_FILENAME = "semantic_models_index.json"


def _reports_by_dataset(catalog: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """datasetId -> list of report rows (workspaces[].reports[], ops-enriched)."""
    by_dataset: Dict[str, List[Dict[str, Any]]] = {}
    for ws in catalog.get("workspaces") or []:
        for r in ws.get("reports") or []:
            ds_id = r.get("datasetId") or ""
            if not ds_id:
                continue
            by_dataset.setdefault(ds_id, []).append(r)
    return by_dataset


def _rollup_usage(reports: List[Dict[str, Any]]) -> Tuple[int, Optional[str]]:
    """Sum view_count and pick the most recent last_viewed across a dataset's reports."""
    total_views = 0
    last_viewed: Optional[str] = None
    for r in reports:
        vc = r.get("view_count")
        if isinstance(vc, (int, float)):
            total_views += int(vc)
        lv = r.get("last_viewed")
        ts = (lv or {}).get("timestamp") if isinstance(lv, dict) else None
        if ts and (last_viewed is None or ts > last_viewed):
            last_viewed = ts
    return total_views, last_viewed


def build_semantic_models_index(catalog: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flatten catalog['datasets'] (one row per semantic model) into a single
    tenant-wide searchable array, rolling up the reports that use each one.
    """
    datasets_map = catalog.get("datasets") or {}
    reports_by_ds = _reports_by_dataset(catalog)

    models: List[Dict[str, Any]] = []
    skipped = 0

    for ds_id, ds in datasets_map.items():
        if not isinstance(ds, dict):
            continue
        reports = reports_by_ds.get(ds_id, [])
        report_names = [r.get("name") or "" for r in reports]

        # Skip bare duplicate datasets that back Microsoft's stock Usage/
        # Dashboard Metrics Report app when no visible report wraps them
        # (no business value, same "platform noise" rule used elsewhere).
        if not reports and is_excluded_report_name(ds.get("name")):
            skipped += 1
            continue

        view_count, last_viewed = _rollup_usage(reports)
        wname = ds.get("workspaceName") or ""
        dname = ds.get("name") or ""
        search_parts = [wname, dname, *report_names]

        models.append({
            "workspaceId": ds.get("workspaceId") or "",
            "workspaceName": wname,
            "datasetId": ds.get("id") or ds_id,
            "datasetName": dname,
            "datasetOwner": ds.get("dataset_owner") or "Unknown",
            "configuredBy": ds.get("configuredBy"),
            "targetStorageMode": ds.get("targetStorageMode"),
            "tableCount": ds.get("tableCount") or 0,
            "measureCount": ds.get("measureCount") or 0,
            "isRefreshable": ds.get("is_refreshable"),
            "lastRefreshed": ds.get("last_refreshed"),
            "lastRefreshStatus": ds.get("last_refresh_status"),
            "refreshSchedule": ds.get("refresh_schedule") or "Unknown",
            "daysSinceRefresh": ds.get("days_since_refresh"),
            "reportNames": report_names,
            "reportCount": len(report_names),
            "viewCount": view_count,
            "lastViewed": last_viewed,
            "searchText": " | ".join(p for p in search_parts if p).lower(),
        })

    models.sort(key=lambda m: (m.get("datasetName") or "").lower())

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceRunId": catalog.get("sourceRunId"),
        "opsEnrichedAt": catalog.get("opsEnrichedAt"),
        "sourceCatalogStats": catalog.get("stats") or {},
        "modelCount": len(models),
        "skippedUsageMetricsModels": skipped,
        "models": models,
    }


def write_semantic_models_index(latest_dir: Path) -> Optional[Path]:
    """
    Build semantic_models_index.json from latest_dir/workspace_catalog.json
    (already produced by the existing extract step) and write it alongside it.

    Returns the written path, or None if the source catalog is missing or the
    build fails — never raises, so it cannot break the surrounding --fresh /
    --ops-only run.
    """
    latest_dir = Path(latest_dir)
    cat_path = latest_dir / "workspace_catalog.json"
    if not cat_path.is_file():
        logger.warning("semantic_models_index skipped: workspace_catalog.json missing")
        return None
    try:
        catalog = json.loads(cat_path.read_text(encoding="utf-8"))
        index = build_semantic_models_index(catalog)
        out_path = latest_dir / OUTPUT_FILENAME
        out_path.write_text(
            json.dumps(index, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        logger.info(
            "Wrote %s (%.1f KB, %s models, %s skipped)",
            out_path.name,
            out_path.stat().st_size / 1024,
            index.get("modelCount"),
            index.get("skippedUsageMetricsModels"),
        )
        return out_path
    except Exception:
        logger.exception("semantic_models_index build failed (non-fatal)")
        return None


def upload_semantic_models_index(local_path: Path) -> bool:
    """
    Upload the built file to the dedicated SharePoint "agent" subfolder —
    separate from the main latest/ publish tree, so it is never touched by
    clean_sharepoint_latest() / publish_temp_latest() / SHAREPOINT_PUBLISH_FILES.

    Never raises — a failure here must not fail the extract run.
    """
    try:
        from catalog_service import catalog_config as cfg
        from catalog_service.metadata_lib.sharepoint_client import SharePointClient

        cfg.validate_sharepoint_config()
        remote = f"{cfg.SHAREPOINT_AGENT_FOLDER_PATH}/{OUTPUT_FILENAME}"
        sp = SharePointClient()
        sp.resolve_site_and_drive()
        item = sp.upload_file(Path(local_path), remote)
        logger.info("Uploaded %s -> %s", local_path, item.get("webUrl") or remote)
        return True
    except Exception:
        logger.exception("semantic_models_index SharePoint upload failed (non-fatal)")
        return False
