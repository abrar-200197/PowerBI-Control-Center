"""
Tenant-wide "truly unused" column/measure index (Tab 1 — weekly --fresh only).

Answers: "is this column/measure used ANYWHERE — by any report visual, any
DAX measure/calculated-column expression, or any relationship key?" A field
is only listed as unused when NONE of those are true, so this is meant to be
a safe candidate list for actually removing the field from the model (memory
+ refresh-time optimization), as opposed to field_usage_index.py's per-field
report list (which answers "who uses this field" without a verdict) or
unused_fields_report_view.py (which is a per-report, not-for-deletion view).

Inputs (both already produced by the existing pipeline — no new API calls):
  - workspace_catalog.json  -> datasets{}.tables[].columns[]/.measures[],
    datasets{}.relationships[] (schema facts from the weekly --fresh scan).
  - field_usage_index.json  -> fields{} map of table.field -> reports that
    visually bind it (built the same run, see field_usage_index.py).

Exclusion rules (a field is NOT flagged unused if any apply):
  1. Bound to a visual in >=1 report (field_usage_index "fields" map).
  2. Column: referenced as `TableName[ColumnName]` (quoted or unquoted table
     name) inside ANY measure's or calculated column's DAX expression,
     ANYWHERE in the dataset — not just within its own table. (Bug fixed
     2026-10: previously this only trusted the per-column `usedIn` tag,
     which is only populated for same-table references; a column used only
     by a measure/calculated-column defined in a *different* table was
     incorrectly flagged "unused" and unsafe to delete.)
  2b. Column: tagged with "M Expression" in its own `usedIn` (Power Query M
     expressions aren't DAX text and aren't scanned above, so this tag is
     still the only signal available for M-level usage).
  3. Column: used as a relationship key (fromColumn/toColumn) anywhere in
     its dataset's relationships[].
  4. Measure: referenced by name inside another measure's DAX expression,
     or inside any calculated column's expression, anywhere in the dataset.

Known, honestly-documented limitation: the Scanner API this catalog is built
from does not return Row-Level-Security role/filter definitions or
sort-by-column bindings, so a field used ONLY by an RLS filter or as a
sort-by column cannot be detected and excluded. "Unused" here therefore
means "unused by everything this tool can see" — always spot-check in
Tabular Editor / the model itself before deleting anything this flags.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List


def _field_key(table: str, name: str) -> str:
    return f"{table}.{name}".strip(".").lower()


def _relationship_key_columns(relationships: List[Dict[str, Any]]) -> set:
    keys = set()
    for rel in relationships or []:
        if not isinstance(rel, dict):
            continue
        ft, fc = rel.get("fromTable"), rel.get("fromColumn")
        tt, tc = rel.get("toTable"), rel.get("toColumn")
        if ft and fc:
            keys.add(_field_key(ft, fc))
        if tt and tc:
            keys.add(_field_key(tt, tc))
    return keys


def build_unused_fields_index(catalog: Dict[str, Any], field_usage_index: Dict[str, Any]) -> Dict[str, Any]:
    used_fields = field_usage_index.get("fields") or {}

    def _used_by_reports(key: str) -> bool:
        entry = used_fields.get(key)
        return bool(entry and (entry.get("reportCount") or 0) > 0)

    items: List[Dict[str, Any]] = []
    total_columns = total_measures = 0
    excluded_via_dax = excluded_via_relationship = excluded_via_reports = 0

    for ds_id, ds in (catalog.get("datasets") or {}).items():
        if not isinstance(ds, dict):
            continue
        ws_id, ws_name = ds.get("workspaceId") or "", ds.get("workspaceName") or ""
        ds_name = ds.get("name") or ""
        rel_keys = _relationship_key_columns(ds.get("relationships") or [])

        # Collect every measure/calculated-column DAX expression in this
        # dataset, tagged by its own (table, name), so a measure-to-measure or
        # column-to-measure reference anywhere in the model counts as "used" —
        # not just within its own table. Checking against "every expression
        # except the field's own" avoids a measure's own self-text ever
        # counting as a (false) external reference.
        all_expressions: List[tuple] = []  # (ownerTable, ownerName, expressionTextLower)
        for t in ds.get("tables") or []:
            t_name = t.get("name") or ""
            for m in t.get("measures") or []:
                if m.get("name") and m.get("expression"):
                    all_expressions.append((t_name, m.get("name"), str(m.get("expression")).lower()))
            for c in t.get("columns") or []:
                if c.get("name") and c.get("expression"):
                    all_expressions.append((t_name, c.get("name"), str(c.get("expression")).lower()))

        for t in ds.get("tables") or []:
            table_name = t.get("name") or ""

            for c in t.get("columns") or []:
                col_name = c.get("name") or ""
                if not col_name:
                    continue
                total_columns += 1
                key = _field_key(table_name, col_name)
                used_in = c.get("usedIn") or []
                if _used_by_reports(key):
                    excluded_via_reports += 1
                    continue
                # M/Power-Query usage isn't DAX text, so it can't be found by
                # the tenant-wide scan below — the normalize-time tag is the
                # only signal for it.
                if any(u == "M Expression" for u in used_in):
                    excluded_via_dax += 1
                    continue
                # Tenant-wide DAX reference scan: does ANY measure or
                # calculated-column expression in this dataset (own table OR
                # any other table) reference `TableName[ColumnName]`? This
                # mirrors the measure check below and fixes the cross-table
                # blind spot the old same-table-only `usedIn` tag had.
                needle_plain = f"{table_name}[{col_name}]".lower()
                needle_quoted = f"'{table_name}'[{col_name}]".lower()
                referenced_in_dax = any(
                    (needle_plain in expr_text or needle_quoted in expr_text)
                    for owner_table, owner_name, expr_text in all_expressions
                    if not (owner_table == table_name and owner_name == col_name)
                )
                if referenced_in_dax:
                    excluded_via_dax += 1
                    continue
                if key in rel_keys:
                    excluded_via_relationship += 1
                    continue
                items.append({
                    "workspaceId": ws_id, "workspaceName": ws_name,
                    "datasetId": ds_id, "datasetName": ds_name,
                    "table": table_name, "field": col_name, "type": "column",
                    "isHidden": bool(c.get("isHidden")),
                })

            for m in t.get("measures") or []:
                m_name = m.get("name") or ""
                if not m_name:
                    continue
                total_measures += 1
                key = _field_key(table_name, m_name)
                if _used_by_reports(key):
                    excluded_via_reports += 1
                    continue
                needle = f"[{m_name}]".lower()
                referenced_elsewhere = any(
                    needle in expr_text
                    for owner_table, owner_name, expr_text in all_expressions
                    if not (owner_table == table_name and owner_name == m_name)
                )
                if referenced_elsewhere:
                    excluded_via_dax += 1
                    continue
                items.append({
                    "workspaceId": ws_id, "workspaceName": ws_name,
                    "datasetId": ds_id, "datasetName": ds_name,
                    "table": table_name, "field": m_name, "type": "measure",
                    "isHidden": bool(m.get("isHidden")),
                })

    items.sort(key=lambda r: ((r.get("workspaceName") or "").lower(), (r.get("datasetName") or "").lower(),
                               (r.get("table") or "").lower(), (r.get("field") or "").lower()))

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "schemaVersion": "1.0",
        "sourceFieldUsageGeneratedAt": field_usage_index.get("generatedAt"),
        "fieldUsageDataQuality": {
            "reportsProcessed": field_usage_index.get("reportsProcessed") or 0,
            "reportsSkippedUnchanged": field_usage_index.get("reportsSkippedUnchanged") or 0,
            "reportsFailed": field_usage_index.get("reportsFailed") or 0,
        },
        "caveat": (
            "RLS role filters and sort-by-column bindings are not captured by the "
            "Scanner API this catalog is built from, so a field used only by one of "
            "those cannot be detected here. Always verify in the model before deleting."
        ),
        "totalColumns": total_columns,
        "totalMeasures": total_measures,
        "unusedCount": len(items),
        "excludedViaReportUsage": excluded_via_reports,
        "excludedViaDaxOrMExpression": excluded_via_dax,
        "excludedViaRelationshipKey": excluded_via_relationship,
        "items": items,
    }
