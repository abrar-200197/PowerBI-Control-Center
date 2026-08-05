"""
Normalize Admin Scanner workspace payloads into a clean inventory structure.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .expression_parser import (
    SourceRef,
    classify_source_display,
    enrich_from_datasource,
    extract_expression,
    parse_m_expression,
)

logger = logging.getLogger(__name__)


def _ds_connection_map(workspace: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map datasource id -> details from workspace-level datasource list if present."""
    out: Dict[str, Dict[str, Any]] = {}
    for ds in workspace.get("dataSources") or workspace.get("datasources") or []:
        ds_id = ds.get("datasourceId") or ds.get("id")
        if ds_id:
            out[str(ds_id)] = ds
    return out


def _parse_table_sources(
    table: Dict[str, Any],
    dataset_datasources: List[Dict[str, Any]],
) -> tuple:
    """
    Returns (sources_list, expression_text).
    sources = parsed EDW/SQL/etc objects from M expression.
    """
    refs: List[SourceRef] = []
    raw_source = table.get("source") if table.get("source") is not None else table.get("expression")
    expr = extract_expression(raw_source)
    refs.extend(parse_m_expression(expr))

    # Fallback: dataset-level datasources + model table name (weak lineage)
    if not refs and dataset_datasources:
        for dds in dataset_datasources:
            conn = dds.get("connectionDetails") or dds
            if not isinstance(conn, dict) or not any(
                conn.get(k) for k in ("server", "database", "path", "url", "kind")
            ):
                # Skip pure instance-id stubs — they add noise without server/db
                if not dds.get("datasourceType") and not dds.get("connectionDetails"):
                    continue
            ref = enrich_from_datasource(
                source_type=dds.get("datasourceType") or dds.get("type"),
                connection_details=conn if isinstance(conn, dict) else {},
                table_name=table.get("name"),
            )
            if ref:
                refs.append(ref)

    if not refs:
        refs.append(
            SourceRef(
                source_type="ModelTable",
                table=table.get("name"),
                object_name=table.get("name"),
            )
        )

    return [r.to_dict() for r in refs], expr


def normalize_workspaces(raw_workspaces: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Build inventory document:
      workspaces[] → reports, datasets (tables, expressions, datasources), dashboards
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    inventory_workspaces: List[Dict[str, Any]] = []
    stats = {
        "workspaceCount": 0,
        "reportCount": 0,
        "datasetCount": 0,
        "tableCount": 0,
        "dashboardCount": 0,
        "datasourceCount": 0,
    }

    for ws in raw_workspaces:
        ws_id = ws.get("id")
        ws_name = ws.get("name")
        ws_type = ws.get("type")
        ws_state = ws.get("state")

        reports = []
        for r in ws.get("reports") or []:
            # Preserve owner/date fields when Scanner returns them (users + team/DL).
            # Live UI also overlays Groups REST; catalog storage avoids N/A gaps.
            reports.append({
                "id": r.get("id"),
                "name": r.get("name"),
                "datasetId": r.get("datasetId"),
                "reportType": r.get("reportType") or r.get("type"),
                "description": r.get("description"),
                "createdBy": r.get("createdBy") or r.get("createdByUserPrincipalName"),
                "modifiedBy": r.get("modifiedBy") or r.get("modifiedByUserPrincipalName"),
                "createdDateTime": r.get("createdDateTime") or r.get("createdDate"),
                "modifiedDateTime": r.get("modifiedDateTime") or r.get("modifiedDate"),
                "createdById": r.get("createdById"),
                "modifiedById": r.get("modifiedById"),
            })
            stats["reportCount"] += 1

        # Workspace-level datasource instances (when scanner returns them)
        ws_ds_by_id = _ds_connection_map(ws)

        datasets = []
        for d in ws.get("datasets") or []:
            # Prefer resolved datasource objects; fall back to usage IDs + workspace map
            ds_datasources = list(d.get("datasources") or d.get("dataSources") or [])
            if not ds_datasources:
                for usage in d.get("datasourceUsages") or []:
                    inst = usage.get("datasourceInstanceId") or usage.get("datasourceId")
                    if inst and str(inst) in ws_ds_by_id:
                        ds_datasources.append(ws_ds_by_id[str(inst)])
                    elif inst:
                        ds_datasources.append({
                            "datasourceId": inst,
                            "datasourceInstanceId": inst,
                        })

            tables_out = []
            for t in d.get("tables") or []:
                sources, expr_text = _parse_table_sources(t, ds_datasources)
                # Rebuild SourceRef list lightly for display classifier
                display = classify_source_display(
                    expr_text or "",
                    [
                        SourceRef(
                            source_type=s.get("source_type") or s.get("sourceType") or "Unknown",
                            server=s.get("server"),
                            database=s.get("database"),
                            schema=s.get("schema"),
                            table=s.get("table"),
                            object_name=s.get("object_name") or s.get("objectName"),
                        )
                        for s in sources
                    ],
                )
                measure_exprs = "\n".join(
                    str(m.get("expression") or "") for m in (t.get("measures") or [])
                )
                columns = []
                for c in t.get("columns") or []:
                    cname = c.get("name") or ""
                    used_in = []
                    if expr_text and cname and cname in expr_text:
                        used_in.append("M Expression")
                    if measure_exprs and cname and cname in measure_exprs:
                        used_in.append("DAX Measure")
                    # Scanner does not give visual-level column usage; mark present columns as model
                    if not used_in:
                        used_in.append("Model")
                    columns.append({
                        "name": cname,
                        "dataType": c.get("dataType") or c.get("type"),
                        "isHidden": c.get("isHidden"),
                        "usedIn": used_in,
                        "usedInReport": True,  # bound report uses this model; visual-level N/A from Scanner
                    })
                measures = [
                    {
                        "name": m.get("name"),
                        "expression": m.get("expression"),
                        "isHidden": m.get("isHidden"),
                    }
                    for m in (t.get("measures") or [])
                ]
                tables_out.append({
                    "name": t.get("name"),
                    "isHidden": t.get("isHidden"),
                    "description": t.get("description"),
                    "sourceExpression": expr_text or None,
                    "sources": sources,
                    "sourceTypeLabel": display.get("sourceTypeLabel"),
                    "serverName": display.get("serverName"),
                    "sqlSourceTables": display.get("sqlSourceTables") or [],
                    "sqlQuery": display.get("sqlQuery"),
                    "fileName": display.get("fileName"),
                    "sourceUrl": display.get("sourceUrl"),
                    "columnCount": len(columns),
                    "measureCount": len(measures),
                    "columns": columns,
                    "measures": measures,
                })
                stats["tableCount"] += 1

            resolved_ds = []
            for x in ds_datasources:
                conn = x.get("connectionDetails")
                resolved_ds.append({
                    "datasourceType": x.get("datasourceType") or x.get("datasourceType") or x.get("type"),
                    "connectionDetails": conn,
                    "gatewayId": x.get("gatewayId"),
                    "datasourceId": x.get("datasourceId") or x.get("datasourceInstanceId"),
                })

            # Preserve relationships for Semantic Models / documentation UI
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

            ds_entry = {
                "id": d.get("id"),
                "name": d.get("name"),
                "configuredBy": d.get("configuredBy"),
                "createdDate": d.get("createdDate") or d.get("createdDateTime"),
                "targetStorageMode": d.get("targetStorageMode") or d.get("contentProviderType"),
                "tables": tables_out,
                "relationships": relationships,
                "relationshipCount": len(relationships),
                "measureCount": sum(int(t.get("measureCount") or 0) for t in tables_out),
                "datasources": resolved_ds,
            }
            stats["datasourceCount"] += sum(
                1 for x in resolved_ds if x.get("connectionDetails") or x.get("datasourceType")
            )
            datasets.append(ds_entry)
            stats["datasetCount"] += 1

        dashboards = []
        for dash in ws.get("dashboards") or []:
            dashboards.append({
                "id": dash.get("id"),
                "displayName": dash.get("displayName") or dash.get("name"),
            })
            stats["dashboardCount"] += 1

        inventory_workspaces.append({
            "id": ws_id,
            "name": ws_name,
            "type": ws_type,
            "state": ws_state,
            "isOnDedicatedCapacity": ws.get("isOnDedicatedCapacity"),
            "capacityId": ws.get("capacityId"),
            "reports": reports,
            "datasets": datasets,
            "dashboards": dashboards,
        })
        stats["workspaceCount"] += 1

    return {
        "generatedAt": generated_at,
        "schemaVersion": "1.0",
        "stats": stats,
        "workspaces": inventory_workspaces,
    }
