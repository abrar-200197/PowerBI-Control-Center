"""
Build reverse indexes for migration impact:
  tableKey → datasets → reports
  datasource/server → datasets → reports
"""

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Tuple


def _report_index(inventory: Dict[str, Any]) -> Dict[str, List[Dict[str, str]]]:
    """datasetId -> list of {reportId, reportName, workspaceId, workspaceName}"""
    by_dataset: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for ws in inventory.get("workspaces") or []:
        ws_id, ws_name = ws.get("id"), ws.get("name")
        for r in ws.get("reports") or []:
            ds_id = r.get("datasetId")
            if not ds_id:
                continue
            by_dataset[ds_id].append({
                "reportId": r.get("id"),
                "reportName": r.get("name"),
                "workspaceId": ws_id,
                "workspaceName": ws_name,
                "reportType": r.get("reportType"),
            })
    return by_dataset


def build_impact_index(inventory: Dict[str, Any]) -> Dict[str, Any]:
    """
    Reverse index: physical/model table → impacted datasets & reports.
    """
    reports_by_dataset = _report_index(inventory)

    # tableKey -> structure we mutate then freeze
    tables: Dict[str, Dict[str, Any]] = {}

    for ws in inventory.get("workspaces") or []:
        ws_id, ws_name = ws.get("id"), ws.get("name")
        for ds in ws.get("datasets") or []:
            ds_id, ds_name = ds.get("id"), ds.get("name")
            bound_reports = reports_by_dataset.get(ds_id, [])

            for table in ds.get("tables") or []:
                model_table = table.get("name")
                sources = table.get("sources") or []
                if not sources:
                    sources = [{
                        "source_type": "ModelTable",
                        "table": model_table,
                        "tableKey": f"ModelTable|dbo.{(model_table or 'unknown').lower()}",
                    }]

                for src in sources:
                    key = src.get("tableKey") or src.get("table_key")
                    if not key:
                        st = src.get("source_type") or src.get("sourceType") or "unknown"
                        tname = (src.get("table") or src.get("object_name") or model_table or "unknown")
                        key = f"{st}|{(src.get('schema') or 'dbo')}.{tname}".lower()

                    if key not in tables:
                        tables[key] = {
                            "tableKey": key,
                            "sourceType": src.get("source_type") or src.get("sourceType"),
                            "server": src.get("server"),
                            "database": src.get("database"),
                            "schema": src.get("schema"),
                            "table": src.get("table") or src.get("object_name") or model_table,
                            "modelTableNames": set(),
                            "datasets": {},  # ds_id -> entry
                        }

                    entry = tables[key]
                    if model_table:
                        entry["modelTableNames"].add(model_table)
                    # Prefer richer connection info if previously unknown
                    for fld in ("server", "database", "schema"):
                        if not entry.get(fld) and src.get(fld):
                            entry[fld] = src.get(fld)

                    if ds_id not in entry["datasets"]:
                        entry["datasets"][ds_id] = {
                            "datasetId": ds_id,
                            "datasetName": ds_name,
                            "workspaceId": ws_id,
                            "workspaceName": ws_name,
                            "modelTableName": model_table,
                            "reports": [],
                        }
                    # merge reports (unique by reportId)
                    existing_rids = {
                        r["reportId"] for r in entry["datasets"][ds_id]["reports"] if r.get("reportId")
                    }
                    for rep in bound_reports:
                        rid = rep.get("reportId")
                        if rid and rid not in existing_rids:
                            entry["datasets"][ds_id]["reports"].append(rep)
                            existing_rids.add(rid)

    # Freeze sets / compute summaries
    out_tables = {}
    for key, entry in tables.items():
        dataset_list = list(entry["datasets"].values())
        report_ids: Set[str] = set()
        workspace_ids: Set[str] = set()
        for d in dataset_list:
            if d.get("workspaceId"):
                workspace_ids.add(d["workspaceId"])
            for r in d.get("reports") or []:
                if r.get("reportId"):
                    report_ids.add(r["reportId"])

        out_tables[key] = {
            "tableKey": key,
            "sourceType": entry.get("sourceType"),
            "server": entry.get("server"),
            "database": entry.get("database"),
            "schema": entry.get("schema"),
            "table": entry.get("table"),
            "modelTableNames": sorted(entry["modelTableNames"]),
            "datasets": dataset_list,
            "impactSummary": {
                "datasetCount": len(dataset_list),
                "reportCount": len(report_ids),
                "workspaceCount": len(workspace_ids),
            },
        }

    # Sort by blast radius (most reports first)
    sorted_tables = dict(
        sorted(
            out_tables.items(),
            key=lambda kv: (-kv[1]["impactSummary"]["reportCount"], kv[0]),
        )
    )

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "schemaVersion": "1.0",
        "tableCount": len(sorted_tables),
        "tables": sorted_tables,
    }


def build_sources_index(inventory: Dict[str, Any]) -> Dict[str, Any]:
    """Connection-centric: server|database → datasets → reports."""
    reports_by_dataset = _report_index(inventory)
    sources: Dict[str, Dict[str, Any]] = {}

    for ws in inventory.get("workspaces") or []:
        for ds in ws.get("datasets") or []:
            ds_id = ds.get("id")
            bound = reports_by_dataset.get(ds_id, [])
            for dsrc in ds.get("datasources") or []:
                conn = dsrc.get("connectionDetails") or {}
                server = conn.get("server") or conn.get("path") or conn.get("url") or ""
                database = conn.get("database") or conn.get("databaseName") or ""
                dtype = dsrc.get("datasourceType") or "Unknown"
                key = f"{dtype}|{str(server).lower()}|{str(database).lower()}"

                if key not in sources:
                    sources[key] = {
                        "sourceKey": key,
                        "datasourceType": dtype,
                        "server": server or None,
                        "database": database or None,
                        "gatewayId": dsrc.get("gatewayId"),
                        "datasets": {},
                    }
                if ds_id not in sources[key]["datasets"]:
                    sources[key]["datasets"][ds_id] = {
                        "datasetId": ds_id,
                        "datasetName": ds.get("name"),
                        "workspaceId": ws.get("id"),
                        "workspaceName": ws.get("name"),
                        "reports": list(bound),
                    }

    out = {}
    for key, entry in sources.items():
        dlist = list(entry["datasets"].values())
        rids = {r["reportId"] for d in dlist for r in d.get("reports") or [] if r.get("reportId")}
        out[key] = {
            **{k: v for k, v in entry.items() if k != "datasets"},
            "datasets": dlist,
            "impactSummary": {
                "datasetCount": len(dlist),
                "reportCount": len(rids),
            },
        }

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "schemaVersion": "1.0",
        "sourceCount": len(out),
        "sources": out,
    }


def lookup_table_impact(impact_index: Dict[str, Any], table_name: str) -> List[Dict[str, Any]]:
    """
    Convenience: find all impact entries whose table / model name matches (case-insensitive).
    """
    needle = table_name.lower().strip()
    hits = []
    for key, entry in (impact_index.get("tables") or {}).items():
        names = [entry.get("table") or ""] + list(entry.get("modelTableNames") or [])
        if any(needle == (n or "").lower() or needle in key.lower() for n in names):
            hits.append(entry)
    return hits
