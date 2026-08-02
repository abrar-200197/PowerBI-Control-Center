"""
End-to-end metadata extraction pipeline (local JSON output).
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from catalog_service import catalog_config as config
from .auth import PowerBIAuth
from .impact_builder import build_impact_index, build_sources_index
from .normalizer import normalize_workspaces
from .scanner_client import ScannerClient

logger = logging.getLogger(__name__)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Wrote %s (%.1f KB)", path, path.stat().st_size / 1024)


def _write_latest(output_dir: Path, name: str, data: Any) -> Path:
    path = output_dir / "latest" / name
    _write_json(path, data)
    return path


def run_extraction(
    *,
    modified_since: Optional[datetime] = None,
    workspace_ids: Optional[List[str]] = None,
    exclude_personal: bool = True,
    save_raw: bool = True,
    output_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    """
    Run full extract. Returns map of artifact name → file path.
    """
    config.validate_config()
    out_dir = Path(output_dir or config.OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = _ts()
    run_dir = out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    auth = PowerBIAuth()
    client = ScannerClient(auth)

    # 1. Resolve workspace IDs
    if workspace_ids:
        ids = list(workspace_ids)
        logger.info("Using explicit workspace list: %s", len(ids))
    elif config.WORKSPACE_ALLOWLIST:
        ids = list(config.WORKSPACE_ALLOWLIST)
        logger.info("Using WORKSPACE_ALLOWLIST: %s", len(ids))
    else:
        modified = client.get_modified_workspaces(
            modified_since=modified_since,
            exclude_personal=exclude_personal,
        )
        ids = [w["id"] for w in modified if w.get("id")]

    if not ids:
        logger.warning("No workspaces to scan.")
        empty = {"generatedAt": datetime.now(timezone.utc).isoformat(), "workspaces": [], "stats": {}}
        paths = {
            "inventory": run_dir / "inventory.json",
            "impact_index": run_dir / "impact_index.json",
            "sources": run_dir / "sources.json",
        }
        for p, data in [
            (paths["inventory"], empty),
            (paths["impact_index"], {"tables": {}, "tableCount": 0}),
            (paths["sources"], {"sources": {}, "sourceCount": 0}),
        ]:
            _write_json(p, data)
        return paths

    # 2. Scan
    raw_workspaces = client.scan_workspace_ids(ids)
    logger.info("Raw scan returned %s workspaces", len(raw_workspaces))

    if save_raw:
        _write_json(run_dir / "raw_scan.json", {"workspaces": raw_workspaces})

    # 3. Normalize
    inventory = normalize_workspaces(raw_workspaces)
    inventory["runId"] = run_id
    inventory["scannedWorkspaceIds"] = ids

    # 4. Impact indexes
    impact = build_impact_index(inventory)
    impact["runId"] = run_id
    sources = build_sources_index(inventory)
    sources["runId"] = run_id

    # 5. Persist run + latest
    paths = {
        "inventory": run_dir / "inventory.json",
        "impact_index": run_dir / "impact_index.json",
        "sources": run_dir / "sources.json",
        "summary": run_dir / "summary.json",
    }
    _write_json(paths["inventory"], inventory)
    _write_json(paths["impact_index"], impact)
    _write_json(paths["sources"], sources)

    summary = {
        "runId": run_id,
        "generatedAt": inventory.get("generatedAt"),
        "stats": inventory.get("stats"),
        "impactTableCount": impact.get("tableCount"),
        "sourceCount": sources.get("sourceCount"),
        "topImpactTables": _top_impact(impact, n=25),
        "files": {k: str(v) for k, v in paths.items()},
    }
    _write_json(paths["summary"], summary)

    _write_latest(out_dir, "inventory.json", inventory)
    _write_latest(out_dir, "impact_index.json", impact)
    _write_latest(out_dir, "sources.json", sources)
    _write_latest(out_dir, "summary.json", summary)

    # Browser-friendly workspace → report catalog (used by webapp)
    try:
        catalog = _build_workspace_catalog(inventory)
        cat_path = run_dir / "workspace_catalog.json"
        _write_json(cat_path, catalog)
        _write_latest(out_dir, "workspace_catalog.json", catalog)
        paths["workspace_catalog"] = cat_path
    except Exception as exc:
        logger.warning("Failed to build workspace_catalog: %s", exc)

    # SharePoint publish is owned by run_catalog_extract.py (clean → upload → delete temp).
    # Pipeline only materializes JSON under the caller-provided output_dir (temp).
    if config.SHAREPOINT_UPLOAD_ENABLED:
        logger.info(
            "SHAREPOINT_UPLOAD_ENABLED is set, but pipeline defers publish to "
            "run_catalog_extract.py so temp dirs can be deleted after upload."
        )

    logger.info(
        "Done. workspaces=%s reports=%s datasets=%s tables=%s impactKeys=%s",
        inventory["stats"].get("workspaceCount"),
        inventory["stats"].get("reportCount"),
        inventory["stats"].get("datasetCount"),
        inventory["stats"].get("tableCount"),
        impact.get("tableCount"),
    )
    return paths


def _build_workspace_catalog(inventory: Dict[str, Any]) -> Dict[str, Any]:
    """
    Catalog for Control Center UI / SharePoint.

    Keep full model schema needed by Semantic Models Details:
      tables → columns + measures, dataset relationships, datasources.
    Workspace.datasets stays a light index; full schema is in top-level datasets map.
    """
    datasets_by_id: Dict[str, Any] = {}
    workspaces: List[Dict[str, Any]] = []

    for ws in inventory.get("workspaces") or []:
        ds_list = []
        for d in ws.get("datasets") or []:
            tables = []
            total_measures = 0
            for t in d.get("tables") or []:
                cols = [
                    {
                        "name": c.get("name"),
                        "dataType": c.get("dataType"),
                        "isHidden": c.get("isHidden"),
                        "usedIn": c.get("usedIn") or [],
                        "usedInReport": c.get("usedInReport", True),
                        "expression": c.get("expression"),  # calculated columns
                    }
                    for c in (t.get("columns") or [])
                ]
                measures = []
                for m in t.get("measures") or []:
                    if not isinstance(m, dict):
                        continue
                    measures.append({
                        "name": m.get("name"),
                        "expression": m.get("expression"),
                        "description": m.get("description"),
                        "isHidden": m.get("isHidden"),
                        "formatString": m.get("formatString"),
                    })
                total_measures += len(measures)
                tables.append({
                    "name": t.get("name"),
                    "isHidden": t.get("isHidden"),
                    "description": t.get("description"),
                    "columnCount": t.get("columnCount") or len(cols),
                    "measureCount": t.get("measureCount") or len(measures),
                    "sources": t.get("sources") or [],
                    "sourceExpression": t.get("sourceExpression"),
                    "hasExpression": bool(t.get("sourceExpression") or t.get("hasExpression")),
                    "sourceTypeLabel": t.get("sourceTypeLabel"),
                    "serverName": t.get("serverName"),
                    "sqlSourceTables": t.get("sqlSourceTables") or [],
                    "sqlQuery": t.get("sqlQuery"),
                    "columns": cols,
                    "measures": measures,
                })

            relationships = []
            for rel in d.get("relationships") or []:
                if not isinstance(rel, dict):
                    continue
                relationships.append({
                    "fromTable": rel.get("fromTable") or rel.get("sourceTable") or rel.get("fromTableName"),
                    "fromColumn": rel.get("fromColumn") or rel.get("sourceColumn") or rel.get("fromColumnName"),
                    "toTable": rel.get("toTable") or rel.get("targetTable") or rel.get("toTableName"),
                    "toColumn": rel.get("toColumn") or rel.get("targetColumn") or rel.get("toColumnName"),
                    "cardinality": rel.get("cardinality") or rel.get("crossFilteringBehavior") or rel.get("relationshipType"),
                    "isActive": rel.get("isActive", True),
                    "crossFilteringBehavior": rel.get("crossFilteringBehavior"),
                })

            measure_count = int(d.get("measureCount") or total_measures)
            sd = {
                "id": d.get("id"),
                "name": d.get("name"),
                "configuredBy": d.get("configuredBy"),
                "createdDate": d.get("createdDate") or d.get("createdDateTime"),
                "targetStorageMode": d.get("targetStorageMode"),
                "tableCount": len(tables),
                "measureCount": measure_count,
                "relationshipCount": len(relationships),
                "tables": tables,
                "relationships": relationships,
                "datasources": d.get("datasources") or [],
                "workspaceId": ws.get("id"),
                "workspaceName": ws.get("name"),
            }
            ds_list.append({
                "id": sd["id"],
                "name": sd["name"],
                "tableCount": sd["tableCount"],
                "measureCount": sd["measureCount"],
                "relationshipCount": sd["relationshipCount"],
            })
            if sd.get("id"):
                datasets_by_id[sd["id"]] = sd

        reports = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "datasetId": r.get("datasetId"),
                "reportType": r.get("reportType"),
                "description": r.get("description"),
            }
            for r in (ws.get("reports") or [])
        ]
        workspaces.append({
            "id": ws.get("id"),
            "name": ws.get("name"),
            "type": ws.get("type"),
            "state": ws.get("state"),
            "isOnDedicatedCapacity": ws.get("isOnDedicatedCapacity"),
            "capacityId": ws.get("capacityId"),
            "reportCount": len(reports),
            "datasetCount": len(ds_list),
            "dashboardCount": len(ws.get("dashboards") or []),
            "reports": reports,
            "datasets": ds_list,
            "dashboards": [
                {"id": x.get("id"), "displayName": x.get("displayName")}
                for x in (ws.get("dashboards") or [])
            ],
        })

    workspaces.sort(key=lambda w: (-w["reportCount"], (w.get("name") or "").lower()))
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceRunId": inventory.get("runId"),
        "stats": inventory.get("stats") or {},
        "workspaceCount": len(workspaces),
        "workspaces": workspaces,
        "datasets": datasets_by_id,
    }


def _top_impact(impact: Dict[str, Any], n: int = 25) -> List[Dict[str, Any]]:
    rows = []
    for key, entry in (impact.get("tables") or {}).items():
        s = entry.get("impactSummary") or {}
        rows.append({
            "tableKey": key,
            "table": entry.get("table"),
            "server": entry.get("server"),
            "database": entry.get("database"),
            "reportCount": s.get("reportCount", 0),
            "datasetCount": s.get("datasetCount", 0),
            "workspaceCount": s.get("workspaceCount", 0),
        })
    rows.sort(key=lambda r: (-r["reportCount"], r["tableKey"]))
    return rows[:n]
