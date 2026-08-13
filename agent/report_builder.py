"""
agent/report_builder.py — generate a downloadable Power BI report.

Why this instead of clone+rebind in the service:
  * No workspace write permission needed. Nothing is created in your tenant.
  * No approval gate, no allowlist, no blast radius. The worst case is a file
    the user deletes.
  * The user opens it in Power BI Desktop, adjusts it, and publishes it
    themselves -- which is where report review belongs anyway.

Output is a PBIP project (Power BI Project format), zipped:

    Store Performance.pbip                  <- open THIS in Desktop
    Store Performance.Report/
        definition.pbir                     <- live connection to the model
        report.json                         <- pages + visuals
        .platform

PBIP is plain text JSON, so it is diffable and git-friendly -- unlike .pbix,
which is an opaque binary. It is also generated rather than hand-assembled
binary, so there is no risk of producing a corrupt file.

The report LIVE CONNECTS to the published semantic model. It contains no data,
so RLS still applies when the user opens it: they see exactly what they are
entitled to see. That is why generating a report file is safe even though the
snapshot it was planned from has no RLS.

Visuals are grounded in the snapshot: real table names, real column names, real
measure names. A visual referencing a field that does not exist would render as
a broken placeholder, so the builder only emits fields it found in the catalog.
"""
from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Canvas geometry (16:9 at the standard 1280x720 report size)
CANVAS_W, CANVAS_H = 1280, 720
PAD = 12


def _guid() -> str:
    return uuid.uuid4().hex


def _safe_name(s: str) -> str:
    """Windows-safe file/folder name."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s or "").strip().rstrip(".")
    return (s or "Report")[:60]


# ---------------------------------------------------------------------------
# Field references
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    table: str
    name: str
    is_measure: bool = False

    @property
    def query_ref(self) -> str:
        return f"{self.table}.{self.name}"


def _select_entry(f: Field, src: str) -> Dict[str, Any]:
    """One entry in a prototypeQuery Select list."""
    kind = "Measure" if f.is_measure else "Column"
    return {
        kind: {"Expression": {"SourceRef": {"Source": src}}, "Property": f.name},
        "Name": f.query_ref,
    }


def _prototype_query(fields: List[Field]) -> Dict[str, Any]:
    """Build From/Select. One alias per distinct table, in first-seen order."""
    aliases: Dict[str, str] = {}
    frm: List[Dict[str, Any]] = []
    for f in fields:
        if f.table not in aliases:
            alias = f"e{len(aliases)}"
            aliases[f.table] = alias
            frm.append({"Name": alias, "Entity": f.table, "Type": 0})
    return {
        "Version": 2,
        "From": frm,
        "Select": [_select_entry(f, aliases[f.table]) for f in fields],
    }


# ---------------------------------------------------------------------------
# Visuals
# ---------------------------------------------------------------------------
def _visual(visual_type: str, x: int, y: int, w: int, h: int, z: int,
            projections: Dict[str, List[Field]],
            title: Optional[str] = None,
            sort: Optional[Tuple[Field, str]] = None,
            top_n: Optional[int] = None) -> Dict[str, Any]:
    """One visualContainer. `config` is a STRINGIFIED json blob -- that is the
    format Power BI expects, not a nested object."""
    ordered: List[Field] = []
    for role_fields in projections.values():
        for f in role_fields:
            if f not in ordered:
                ordered.append(f)

    single: Dict[str, Any] = {
        "visualType": visual_type,
        "projections": {role: [{"queryRef": f.query_ref} for f in fs]
                        for role, fs in projections.items() if fs},
        "prototypeQuery": _prototype_query(ordered),
        "drillFilterOtherVisuals": True,
    }

    if sort:
        sf, direction = sort
        single["prototypeQuery"]["OrderBy"] = [{
            "Direction": 2 if direction.lower() == "desc" else 1,
            "Expression": ({"Measure": {
                "Expression": {"SourceRef": {"Source": "e0"}},
                "Property": sf.name}} if sf.is_measure else
                {"Column": {
                    "Expression": {"SourceRef": {"Source": "e0"}},
                    "Property": sf.name}}),
        }]
    if top_n:
        single["prototypeQuery"]["Top"] = top_n

    if title:
        single["vcObjects"] = {
            "title": [{"properties": {
                "text": {"expr": {"Literal": {"Value": f"'{title}'"}}},
                "show": {"expr": {"Literal": {"Value": "true"}}},
            }}]
        }

    cfg = {
        "name": _guid(),
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": z,
                                           "width": w, "height": h}}],
        "singleVisual": single,
    }
    return {"x": x, "y": y, "z": z, "width": w, "height": h,
            "config": json.dumps(cfg)}


def _textbox(x: int, y: int, w: int, h: int, z: int, text: str) -> Dict[str, Any]:
    cfg = {
        "name": _guid(),
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": z,
                                           "width": w, "height": h}}],
        "singleVisual": {
            "visualType": "textbox",
            "objects": {"general": [{"properties": {"paragraphs": [{
                "textRuns": [{"value": text,
                              "textStyle": {"fontSize": "14pt",
                                            "fontWeight": "bold"}}]}]}}]},
            "drillFilterOtherVisuals": True,
        },
    }
    return {"x": x, "y": y, "z": z, "width": w, "height": h,
            "config": json.dumps(cfg)}


# ---------------------------------------------------------------------------
# Page layout
# ---------------------------------------------------------------------------
def _build_page(title: str, measures: List[Field],
                dim: Optional[Field], date_col: Optional[Field],
                page_index: int) -> Dict[str, Any]:
    """Header + KPI cards + bar chart + trend line + detail table."""
    vis: List[Dict[str, Any]] = []
    z = 0
    vis.append(_textbox(PAD, PAD, CANVAS_W - 2 * PAD, 40, z, title))
    z += 1

    y = 62
    kpis = measures[:4]
    if kpis:
        cw = (CANVAS_W - 2 * PAD - (len(kpis) - 1) * PAD) // len(kpis)
        for i, m in enumerate(kpis):
            vis.append(_visual("card", PAD + i * (cw + PAD), y, cw, 96, z,
                               {"Values": [m]}, title=m.name))
            z += 1
        y += 96 + PAD

    half = (CANVAS_W - 2 * PAD - PAD) // 2
    body_h = 240

    if dim and measures:
        vis.append(_visual(
            "clusteredBarChart", PAD, y, half, body_h, z,
            {"Category": [dim], "Y": [measures[0]]},
            title=f"{measures[0].name} by {dim.name}",
            sort=(measures[0], "desc"), top_n=10))
        z += 1

    if date_col and measures:
        vis.append(_visual(
            "lineChart", PAD + half + PAD, y, half, body_h, z,
            {"Category": [date_col], "Y": [measures[0]]},
            title=f"{measures[0].name} over time"))
        z += 1
    elif dim and len(measures) > 1:
        vis.append(_visual(
            "donutChart", PAD + half + PAD, y, half, body_h, z,
            {"Category": [dim], "Y": [measures[1]]},
            title=f"{measures[1].name} by {dim.name}"))
        z += 1

    y += body_h + PAD
    table_fields = ([dim] if dim else []) + measures[:4]
    if table_fields:
        vis.append(_visual("tableEx", PAD, y, CANVAS_W - 2 * PAD,
                           CANVAS_H - y - PAD, z,
                           {"Values": table_fields}, title="Detail"))

    return {
        "name": f"ReportSection{page_index}",
        "displayName": title[:50],
        "filters": "[]",
        "ordinal": page_index,
        "visualContainers": vis,
        "config": json.dumps({}),
        "width": CANVAS_W,
        "height": CANVAS_H,
        "displayOption": 1,
    }


# ---------------------------------------------------------------------------
# PBIP assembly
# ---------------------------------------------------------------------------
def _connection_string(workspace: str, dataset: str) -> str:
    return (f'Data Source="powerbi://api.powerbi.com/v1.0/myorg/{workspace}";'
            f'Initial Catalog={dataset};Integrated Security=ClaimsToken')


def build_pbip(report_name: str, workspace_name: str, dataset_name: str,
               dataset_id: str, pages: List[Dict[str, Any]]) -> bytes:
    """Return the bytes of a .zip containing a PBIP project."""
    safe = _safe_name(report_name)
    folder = f"{safe}.Report"

    report_json = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/1.0.0/schema.json",
        "config": json.dumps({
            "version": "5.43",
            "themeCollection": {"baseTheme": {"name": "CY24SU02",
                                              "reportVersionAtImport": "5.43",
                                              "type": "SharedResources"}},
            "activeSectionIndex": 0,
            "defaultDrillFilterOtherVisuals": True,
            "settings": {"useStylableVisualContainerHeader": True,
                         "useNewFilterPaneExperience": True},
        }),
        "layoutOptimization": 0,
        "resourcePackages": [{
            "resourcePackage": {
                "disabled": False,
                "items": [{"name": "CY24SU02", "path": "BaseThemes/CY24SU02.json",
                           "type": 202}],
                "name": "SharedResources",
                "type": 2,
            }
        }],
        "sections": pages,
    }

    pbir = {
        "version": "1.0",
        "datasetReference": {
            "byConnection": {
                "connectionString": _connection_string(workspace_name, dataset_name),
                "pbiServiceModelId": None,
                "pbiModelVirtualServerName": "sobe_wowvirtualserver",
                "pbiModelDatabaseName": dataset_id,
                "name": "EntityDataSource",
                "connectionType": "pbiServiceXmlaStyleLive",
            }
        },
    }

    pbip = {
        "version": "1.0",
        "artifacts": [{"report": {"path": folder}}],
        "settings": {"enableAutoRecovery": True},
    }

    platform = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": safe},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{safe}.pbip", json.dumps(pbip, indent=2))
        z.writestr(f"{folder}/definition.pbir", json.dumps(pbir, indent=2))
        z.writestr(f"{folder}/report.json", json.dumps(report_json, indent=2))
        z.writestr(f"{folder}/.platform", json.dumps(platform, indent=2))
        z.writestr(f"{folder}/README.txt", _READ_ME.format(
            name=safe, underline="=" * len(safe),
            dataset=dataset_name, workspace=workspace_name))
    return buf.getvalue()


_READ_ME = """{name}
{underline}

Generated by the Power BI Control Center agent.

HOW TO OPEN
  1. Unzip this folder somewhere local (not inside OneDrive sync, it can lock
     files while Desktop has them open).
  2. Open {name}.pbip in Power BI Desktop.
       Requires a reasonably recent Desktop. If .pbip does not open, enable
       File > Options > Preview features > "Power BI Project (.pbip) save option".
  3. Desktop live-connects to the semantic model:
       workspace : {workspace}
       model     : {dataset}
     You will be prompted to sign in. You see only data your permissions allow
     -- row-level security still applies. This file contains NO data.

WHAT YOU GET
  A starting point, not a finished report. Visuals are grounded in real fields
  from the model, but layout, formatting and business framing are yours.

IF A VISUAL LOOKS EMPTY
  The field exists in the catalog snapshot but may have been renamed or removed
  in the model since the last catalog build. Check the snapshot freshness stamp
  shown in the agent, then re-point the visual in Desktop.

PUBLISHING
  Publish from Desktop when you are happy with it. The agent deliberately does
  not publish for you.
"""


# ---------------------------------------------------------------------------
# Planning: choose what goes on the page, from the snapshot
# ---------------------------------------------------------------------------
_DIM_PREFERENCE = ("name", "desc", "label", "title", "category", "type",
                   "group", "region", "city", "store", "product", "customer",
                   "supplier", "vendor", "channel", "brand", "status")
_SKIP_COL = ("key", "id", "guid", "code", "flag", "amount", "qty", "quantity",
             "cost", "price", "value", "count", "number")


def plan_report(snap, dataset_id: str, question: str,
                report_name: Optional[str] = None) -> Dict[str, Any]:
    """Pick measures, a grouping column and a date column from the snapshot."""
    ds = snap.execute(
        "SELECT name, workspace_name FROM datasets WHERE dataset_id = ?",
        (dataset_id,)).fetchall()
    if not ds:
        raise ValueError(f"dataset {dataset_id} is not in the snapshot")
    dataset_name, workspace_name = ds[0][0], ds[0][1]

    mrows = snap.execute(
        "SELECT table_name, measure_name FROM measures "
        "WHERE dataset_id = ? AND NOT is_hidden ORDER BY measure_name",
        (dataset_id,)).fetchall()
    measures = [Field(t, m, True) for t, m in mrows]

    # Rank measures by overlap with the question so "store performance" leads
    # with a sales-ish measure rather than whatever sorts first alphabetically.
    words = {w for w in re.findall(r"[a-z]+", (question or "").lower())
             if len(w) > 2}
    if words and measures:
        measures.sort(key=lambda f: -sum(
            1 for w in words if w in f.name.lower()))

    crows = snap.execute(
        "SELECT table_name, column_name, data_type FROM columns "
        "WHERE dataset_id = ? AND NOT is_hidden", (dataset_id,)).fetchall()

    date_col = None
    for t, c, dt in crows:
        if (dt or "").lower() in ("datetime", "date"):
            date_col = Field(t, c)
            break

    dim = None
    best = -1
    for t, c, dt in crows:
        if (dt or "").lower() not in ("string", "text", ""):
            continue
        low = c.lower()
        if any(s in low for s in _SKIP_COL):
            continue
        score = 0
        if any(p in low for p in _DIM_PREFERENCE):
            score += 2
        score += sum(2 for w in words if w in low or w in t.lower())
        if score > best:
            best, dim = score, Field(t, c)

    name = report_name or _title_from_question(question)
    pages = [_build_page(name, measures, dim, date_col, 0)]

    return {
        "report_name": name,
        "dataset_id": dataset_id,
        "dataset_name": dataset_name,
        "workspace_name": workspace_name,
        "pages": pages,
        "fields_used": {
            "measures": [m.query_ref for m in measures[:4]],
            "group_by": dim.query_ref if dim else None,
            "date": date_col.query_ref if date_col else None,
        },
        "visual_count": len(pages[0]["visualContainers"]),
    }


_STOP = {"build", "me", "a", "an", "the", "report", "on", "for", "of", "about",
         "create", "make", "generate", "please", "show", "with", "and", "to"}


def _title_from_question(q: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", q or "")
             if w.lower() not in _STOP]
    return (" ".join(words[:5]).title() or "Report") + " Report"


def generate(snap, dataset_id: str, question: str,
             report_name: Optional[str] = None) -> Tuple[bytes, Dict[str, Any]]:
    """Plan and build. Returns (zip_bytes, spec_without_layout)."""
    spec = plan_report(snap, dataset_id, question, report_name)
    blob = build_pbip(spec["report_name"], spec["workspace_name"],
                      spec["dataset_name"], spec["dataset_id"], spec["pages"])
    meta = {k: v for k, v in spec.items() if k != "pages"}
    meta["size_bytes"] = len(blob)
    meta["file_name"] = _safe_name(spec["report_name"]) + ".pbip.zip"
    return blob, meta
