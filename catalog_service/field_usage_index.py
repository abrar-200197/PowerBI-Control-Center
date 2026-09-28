"""
Cross-report field / metric usage index (weekly --fresh only).

Answers "which reports/visuals use column or measure X" so authors can reuse
an existing metric instead of duplicating it. Built ONLY during the weekly
--fresh catalog rebuild (never the 6h --ops-only job) because it requires one
Report Definition Export call per report via VisualMetadataExtractor — the
same fast path already used by Visual Lineage. Playwright is intentionally
NOT used here (too slow/flaky for a batch of hundreds of reports); reports
that fail the Export path are simply skipped for this run and retried next
week, or (if a prior cache entry exists) keep showing their last known usage.

Incremental: a report is re-extracted only when its modifiedDateTime differs
from the previous run's cached value for that report (perReportCache), so an
unchanged catalog stays fast.

Concurrency + checkpointing (see docs/field usage runtime fix):
- Reports needing extraction are processed concurrently via a thread pool
  (Export API calls are blocking I/O, so this is a pure throughput win).
- Every CHECKPOINT_EVERY completed reports (and once at the end), the
  in-progress index is handed to an optional `on_checkpoint` callback so the
  caller can persist/publish partial progress. If the process is killed
  mid-run, next week's run resumes from that checkpoint instead of from
  scratch.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from catalog_service.thin_packs import is_excluded_report_name

logger = logging.getLogger(__name__)

DEFAULT_MAX_WORKERS = int(os.getenv("FIELD_USAGE_MAX_WORKERS", "10"))
DEFAULT_CHECKPOINT_EVERY = int(os.getenv("FIELD_USAGE_CHECKPOINT_EVERY", "150"))


def _report_modified_key(report: Dict[str, Any]) -> str:
    return str(
        report.get("modifiedDateTime")
        or report.get("modifiedOn")
        or report.get("modified_by_time")
        or ""
    )


def _extract_fields_for_report(extractor, workspace_id: str, report_id: str) -> Optional[Dict[str, Any]]:
    """Synchronously run the async Export API extraction for one report."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(
            extractor.extract_visuals(workspace_id, report_id, detect_render_errors=False)
        )
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _aggregate_report_fields(visual_result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """From extract_visuals() output, build {fieldKey: {table, field, type, visualCount}}."""
    agg: Dict[str, Dict[str, Any]] = {}
    for page in visual_result.get("pages") or []:
        for visual in page.get("visuals") or []:
            for f in visual.get("fields") or []:
                if isinstance(f, str):
                    name, table, ftype = f, "", "unknown"
                elif isinstance(f, dict):
                    name = f.get("name") or f.get("displayName") or ""
                    table = f.get("table") or ""
                    ftype = f.get("type") or "unknown"
                else:
                    continue
                if not name:
                    continue
                key = f"{table}.{name}".strip(".").lower()
                bucket = agg.setdefault(key, {
                    "table": table, "field": name,
                    "type": "measure" if str(ftype).lower() in ("measure", "aggregation") else "column",
                    "visualCount": 0,
                })
                bucket["visualCount"] += 1
    return agg


def _empty_index() -> Dict[str, Any]:
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "schemaVersion": "1.0",
        "reportsProcessed": 0, "reportsSkippedUnchanged": 0, "reportsFailed": 0,
        "fieldCount": 0, "fields": {}, "perReportCache": {},
    }


def _assemble_index(
    fields_map: Dict[str, Dict[str, Any]],
    per_report_cache: Dict[str, Any],
    processed: int,
    skipped: int,
    failed: int,
) -> Dict[str, Any]:
    out_fields: Dict[str, Any] = {}
    for key, bucket in fields_map.items():
        reports_list = list(bucket["reports"].values())
        reports_list.sort(key=lambda r: (r.get("workspaceName") or "", r.get("reportName") or ""))
        out_fields[key] = {
            "table": bucket["table"], "field": bucket["field"], "type": bucket["type"],
            "reportCount": len(reports_list),
            "workspaceCount": len({r.get("workspaceId") for r in reports_list if r.get("workspaceId")}),
            "reports": reports_list,
        }

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "schemaVersion": "1.0",
        "reportsProcessed": processed,
        "reportsSkippedUnchanged": skipped,
        "reportsFailed": failed,
        "fieldCount": len(out_fields),
        "fields": out_fields,
        "perReportCache": per_report_cache,
    }


def build_field_usage_index(
    catalog: Dict[str, Any],
    prior_index: Optional[Dict[str, Any]] = None,
    max_reports: Optional[int] = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
    on_checkpoint: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    Iterate every report; extract field bindings concurrently; build field ->
    reports usage map.

    Reports whose cached fields are still valid (unchanged modifiedDateTime)
    are resolved instantly, no thread needed. Reports needing a fresh Export
    API pull are dispatched to a bounded thread pool (`max_workers`) since the
    extraction is blocking network I/O — this is the main runtime fix versus
    the old fully-sequential loop.

    Fault tolerance: every `checkpoint_every` completed extractions (and once
    more at the very end, and on any exception/interrupt), the partial index
    assembled so far is passed to `on_checkpoint` if provided, so the caller
    can persist/publish it. That means a killed/interrupted run still leaves
    behind real, resumable progress instead of losing everything.
    """
    client_id, client_secret, tenant_id = os.getenv("CLIENT_ID"), os.getenv("CLIENT_SECRET"), os.getenv("TENANT_ID")
    if not (client_id and client_secret and tenant_id):
        logger.warning("field_usage_index: CLIENT_ID/CLIENT_SECRET/TENANT_ID not set — skipping build")
        return prior_index or _empty_index()

    from visual_metadata_extractor import VisualMetadataExtractor
    extractor = VisualMetadataExtractor(client_id, client_secret, tenant_id)

    prior_reports = (prior_index or {}).get("perReportCache") or {}
    per_report_cache: Dict[str, Any] = {}
    fields_map: Dict[str, Dict[str, Any]] = {}
    processed = skipped = failed = n = 0
    lock = threading.Lock()

    def _merge_report_fields(rid: str, rname: str, wid: str, wname: str,
                              mod_key: str, report_fields: Dict[str, Dict[str, Any]]) -> None:
        per_report_cache[rid] = {"modifiedDateTime": mod_key, "fields": report_fields}
        for key, info in report_fields.items():
            bucket = fields_map.setdefault(key, {
                "table": info.get("table") or "", "field": info.get("field") or "",
                "type": info.get("type") or "column", "reports": {},
            })
            bucket["reports"][rid] = {
                "reportId": rid, "reportName": rname,
                "workspaceId": wid, "workspaceName": wname,
                "visualCount": info.get("visualCount") or 0,
            }

    def _maybe_checkpoint(completed_count: int) -> None:
        if not on_checkpoint:
            return
        if checkpoint_every <= 0 or completed_count % checkpoint_every != 0:
            return
        try:
            snapshot = _assemble_index(fields_map, per_report_cache, processed, skipped, failed)
            on_checkpoint(snapshot)
            logger.info(
                "field_usage_index: checkpoint at %d completed (processed=%d skipped=%d failed=%d)",
                completed_count, processed, skipped, failed,
            )
        except Exception as exc:
            logger.warning("field_usage_index: checkpoint callback failed (non-fatal): %s", exc)

    # Pass 1: resolve cache hits inline, collect work items needing extraction.
    to_extract = []  # list of (rid, rname, wid, wname, mod_key)
    for ws in catalog.get("workspaces") or []:
        wid, wname = ws.get("id"), ws.get("name") or ""
        if not wid:
            continue
        for report in ws.get("reports") or []:
            rname = report.get("name") or ""
            if is_excluded_report_name(rname):
                continue
            rid = report.get("id")
            if not rid:
                continue
            if max_reports is not None and n >= max_reports:
                break
            n += 1

            mod_key = _report_modified_key(report)
            prior = prior_reports.get(rid)

            if prior and mod_key and prior.get("modifiedDateTime") == mod_key and isinstance(prior.get("fields"), dict):
                skipped += 1
                _merge_report_fields(rid, rname, wid, wname, mod_key, prior["fields"])
            else:
                to_extract.append((rid, rname, wid, wname, mod_key, prior))

    completed = 0
    if to_extract:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            future_map = {
                pool.submit(_extract_fields_for_report, extractor, wid, rid): (rid, rname, wid, wname, mod_key, prior)
                for (rid, rname, wid, wname, mod_key, prior) in to_extract
            }
            try:
                for future in as_completed(future_map):
                    rid, rname, wid, wname, mod_key, prior = future_map[future]
                    report_fields: Optional[Dict[str, Dict[str, Any]]] = None
                    try:
                        result = future.result()
                        if result and result.get("success"):
                            report_fields = _aggregate_report_fields(result)
                            with lock:
                                processed += 1
                        else:
                            with lock:
                                failed += 1
                            logger.info(
                                "field_usage_index: extraction failed for %s (%s): %s",
                                rid, rname, (result or {}).get("error"),
                            )
                    except Exception as exc:
                        with lock:
                            failed += 1
                        logger.warning("field_usage_index: exception for %s (%s): %s", rid, rname, exc)

                    if report_fields is None:
                        if prior:
                            report_fields = prior.get("fields") or {}
                        else:
                            completed += 1
                            _maybe_checkpoint(completed)
                            continue

                    with lock:
                        _merge_report_fields(rid, rname, wid, wname, mod_key, report_fields)
                    completed += 1
                    _maybe_checkpoint(completed)
            except BaseException:
                # Interrupted (Ctrl+C, kill, etc.) — save whatever we have so far
                # before propagating, so the run isn't a total loss.
                logger.warning("field_usage_index: interrupted — saving partial checkpoint before exit")
                _maybe_checkpoint(checkpoint_every if checkpoint_every > 0 else 1)
                raise

    final_index = _assemble_index(fields_map, per_report_cache, processed, skipped, failed)
    if on_checkpoint:
        try:
            on_checkpoint(final_index)
        except Exception as exc:
            logger.warning("field_usage_index: final checkpoint callback failed (non-fatal): %s", exc)
    return final_index
