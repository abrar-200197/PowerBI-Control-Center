"""
agent/orchestrator.py — the Router/Planner and the five specialists.

Flow for every turn:

    route()                    deterministic intent -> plane (security choke point)
      |
      +-- SNAPSHOT --> GovernanceAgent  (DuckDB/sqlite, no RLS, metadata only)
      +-- LIVE     --> GovernanceAgent resolves dataset+schema FIRST,
      |                then DataAgent generates DAX -> executeQueries (OBO, RLS)
      +-- WRITE    --> AuthoringAgent / ReportAgent: PLAN ONLY, never auto-apply
      |
      +-- Narrator --> formats the answer + citations + freshness stamp

Governance runs before Data on every live turn because the user says "sales",
not "ds-7f3a / FACT_SALES / [Net Sales]". Resolving that first is what makes the
DAX valid AND gives the citation for free.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from . import report_builder as R
from . import tools_governance as G
from . import tools_live as L
from .context import AuthRequired, Intent, Plane, TurnContext
from .dax_generator import MAX_REPAIRS, get_generator, schema_to_prompt
from .db import Snapshot, get_snapshot
from .executor import execute_plan, preflight, writes_enabled
from .narrator import narrate
from .router import route

log = logging.getLogger("agent")

STOPWORDS = {
    "what", "is", "the", "for", "of", "in", "on", "a", "an", "show", "me",
    "how", "much", "many", "total", "give", "get", "list", "all", "which",
    "report", "reports", "model", "models", "dataset", "datasets", "and",
    "please", "tell", "about", "does", "do", "use", "used", "from", "by",
    "calculated", "defined", "sources", "source", "last", "night", "unused",
}


def keywords(text: str) -> List[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_ ]*", text or "")
    toks: List[str] = []
    for w in re.split(r"\s+", " ".join(words)):
        wl = w.strip().lower()
        if wl and wl not in STOPWORDS and len(wl) > 2:
            toks.append(wl)
    return toks


# ---------------------------------------------------------------------------
# Governance Agent — snapshot plane. Never returns fact values.
# ---------------------------------------------------------------------------
class GovernanceAgent:
    def __init__(self, snap: Snapshot):
        self.snap = snap

    def run(self, ctx: TurnContext) -> TurnContext:
        m = G.assert_fresh(self.snap)          # refuse partial builds
        as_of = m.get("built_at_utc")
        kw = keywords(ctx.question)
        probe = " ".join(kw[:3]) if kw else ""

        if ctx.intent is Intent.MEASURE_DEFINITION:
            res = G.find_measure(self.snap, probe or ctx.question)
            for r in res.rows[:5]:
                ctx.add(f"{r['measure_name']} = {r['expression']}",
                        "duckdb:measures",
                        detail=f"{r.get('dataset_name')} / {r.get('table_name')}",
                        build_id=res.build_id, as_of_utc=as_of)
                ctx.dataset_id = ctx.dataset_id or r.get("dataset_id")
            ctx.entities["rows"] = res.rows

        elif ctx.intent is Intent.LINEAGE_SOURCES:
            ds = self._resolve_dataset(ctx, probe)
            if ds:
                res = G.model_sources(self.snap, ds)
                for r in res.rows[:20]:
                    ctx.add(
                        f"{r.get('kind') or 'source'}: "
                        f"{r.get('server') or ''}{('/' + r['database']) if r.get('database') else ''}"
                        f"{(' <- ' + r['table_name']) if r.get('table_name') else ''}",
                        "duckdb:datasources", build_id=res.build_id, as_of_utc=as_of)
                ctx.entities["rows"] = res.rows

        elif ctx.intent is Intent.IMPACT_ANALYSIS:
            target = self._impact_target(ctx.question) or probe
            res = G.impact_of(self.snap, target)
            reports = {r["report_id"]: r.get("report_name") for r in res.rows if r.get("report_id")}
            datasets = {r["dataset_id"]: r.get("dataset_name") for r in res.rows if r.get("dataset_id")}
            if res.rows:
                ctx.add(
                    f"{len(reports)} report(s) across {len(datasets)} model(s) "
                    f"depend on {res.rows[0].get('physical_table') or target}",
                    "duckdb:impact", build_id=res.build_id, as_of_utc=as_of)
            for rid, rname in list(reports.items())[:25]:
                ctx.add(f"report: {rname or rid}", "duckdb:impact",
                        build_id=res.build_id, as_of_utc=as_of)
            ctx.entities["rows"] = res.rows

        elif ctx.intent is Intent.REFRESH_STATUS:
            only_failed = bool(re.search(r"\b(fail|error|broke)", ctx.question, re.I))
            res = G.refresh_status(self.snap, only_failed=only_failed)
            for r in res.rows[:25]:
                ctx.add(f"{r.get('dataset_name') or r['dataset_id']}: {r.get('status')}"
                        f"{(' — ' + r['error_code']) if r.get('error_code') else ''}",
                        "duckdb:refresh", detail=str(r.get("last_refresh_utc") or ""),
                        build_id=res.build_id, as_of_utc=as_of)
            ctx.entities["rows"] = res.rows

        elif ctx.intent is Intent.USAGE_STATS:
            unused = bool(re.search(r"\b(unused|not used|nobody|no one|zero)", ctx.question, re.I))
            res = G.usage_stats(self.snap, unused_only=unused)
            for r in res.rows[:25]:
                ctx.add(f"{r.get('report_name')}: {r.get('views') or 0} views, "
                        f"{r.get('distinct_users') or 0} users",
                        "duckdb:usage", detail=str(r.get("last_viewed_utc") or "never"),
                        build_id=res.build_id, as_of_utc=as_of)
            ctx.entities["rows"] = res.rows

        elif ctx.intent is Intent.MODEL_INVENTORY:
            res = G.find_dataset(self.snap, probe or "")
            for r in res.rows[:25]:
                ctx.add(f"{r['name']} ({r.get('workspace_name')}): "
                        f"{r.get('table_count') or 0} tables, "
                        f"{r.get('measure_count') or 0} measures",
                        "duckdb:datasets", build_id=res.build_id, as_of_utc=as_of)
            ctx.entities["rows"] = res.rows

        return ctx

    # -- helpers ----------------------------------------------------------
    def _resolve_dataset(self, ctx: TurnContext, probe: str) -> Optional[str]:
        if ctx.dataset_id:
            return ctx.dataset_id
        for cand in ([probe] + keywords(ctx.question)):
            if not cand:
                continue
            res = G.find_dataset(self.snap, cand)
            if res.rows:
                ctx.dataset_id = res.rows[0]["dataset_id"]
                ctx.workspace_id = res.rows[0].get("workspace_id")
                ctx.entities["dataset_name"] = res.rows[0].get("name")
                return ctx.dataset_id
        res = G.find_dataset(self.snap, "")
        if res.rows:                      # single-model tenants: just use it
            ctx.dataset_id = res.rows[0]["dataset_id"]
            ctx.workspace_id = res.rows[0].get("workspace_id")
            ctx.entities["dataset_name"] = res.rows[0].get("name")
            ctx.errors.append("dataset not named in question; used the top match")
        return ctx.dataset_id

    @staticmethod
    def _impact_target(q: str) -> Optional[str]:
        m = re.search(r"\b((?:dbo|stg|edw|dw)\.[A-Za-z0-9_]+)", q, re.I)
        if m:
            return m.group(1)
        m = re.search(r"\b([A-Za-z0-9_]*(?:fact|dim)[A-Za-z0-9_]*)\b", q, re.I)
        return m.group(1) if m else None

    def ground(self, dataset_id: str) -> str:
        return schema_to_prompt(
            G.model_schema(self.snap, dataset_id).rows,
            G.find_measure(self.snap, "", dataset_id).rows,
            G.relationships(self.snap, dataset_id).rows,
        )


# ---------------------------------------------------------------------------
# Data Agent — live plane. OBO only. RLS enforced by Power BI.
# ---------------------------------------------------------------------------
class DataAgent:
    def __init__(self, gov: GovernanceAgent):
        self.gov = gov
        self.gen = get_generator()

    def run(self, ctx: TurnContext) -> TurnContext:
        ctx.enforce_plane()                       # belt and braces: no token, no data
        ds = self.gov._resolve_dataset(ctx, " ".join(keywords(ctx.question)[:3]))
        if not ds:
            ctx.errors.append("could not resolve which semantic model to query")
            return ctx

        schema = self.gov.ground(ds)
        ctx.add(f"model: {ctx.entities.get('dataset_name') or ds}", "duckdb:datasets")

        err: Optional[str] = None
        dax: Optional[str] = None
        for attempt in range(MAX_REPAIRS + 1):
            dax = self.gen.generate(ctx.question, schema, err, dax)
            ctx.dax = dax
            try:
                res = L.execute_dax(ds, dax, ctx.obo_token or "", ctx.user_upn,
                                    group_id=ctx.workspace_id)
            except L.DaxError as e:
                err = str(e)
                ctx.errors.append(f"attempt {attempt + 1}: DAX rejected — {err}")
                continue                          # repair loop
            except L.LiveUnavailable as e:
                ctx.errors.append(str(e))
                return ctx                        # NOT repairable by new DAX
            ctx.result_rows = len(res.rows)
            ctx.entities["rows"] = res.rows
            ctx.dax = res.dax
            for w in res.warnings:
                ctx.errors.append(w)
            ctx.add(f"{len(res.rows)} row(s) from live query",
                    "executeQueries", detail=f"{res.elapsed_ms}ms")
            return ctx

        ctx.errors.append(
            f"could not produce valid DAX after {MAX_REPAIRS + 1} attempts")
        return ctx


# ---------------------------------------------------------------------------
# Authoring / Report Agents — WRITE plane. Plan only; a human applies.
# ---------------------------------------------------------------------------
class AuthoringAgent:
    """Produces the TOM script for review. Deliberately does NOT execute.

    Applying requires: XMLA read/write enabled on the capacity, a DEV model,
    and explicit human approval. An XMLA write makes the model no longer
    round-trippable through its original PBIX -- warn every time.
    """

    def __init__(self, gov: GovernanceAgent):
        self.gov = gov

    def run(self, ctx: TurnContext) -> TurnContext:
        ds = self.gov._resolve_dataset(ctx, " ".join(keywords(ctx.question)[:3]))
        name = self._proposed_name(ctx.question)
        schema = self.gov.ground(ds) if ds else ""
        expr = get_generator().generate(
            f"Write only the DAX expression body for a measure named "
            f"'{name}' that answers: {ctx.question}", schema)
        expr = expr.replace("EVALUATE", "").strip()

        ctx.entities["plan"] = {
            "action": "add_measure",
            "dataset_id": ds,
            "dataset_name": ctx.entities.get("dataset_name"),
            "measure_name": name,
            "expression": expr,
            "target": "DEV model only",
            "script": _tom_script(ctx.entities.get("dataset_name") or "", name, expr),
        }
        ctx.requires_approval = True
        ctx.add(f"proposed measure [{name}] = {expr}", "plan")
        ctx.errors.append(
            "NOT APPLIED. Review, then run against a DEV model. XMLA writes "
            "break PBIX round-tripping: the desktop file and the service model "
            "diverge permanently."
        )
        return ctx

    @staticmethod
    def _proposed_name(q: str) -> str:
        m = re.search(r"measure\s+(?:for|called|named)?\s*['\"]?([A-Za-z0-9 _%]+)", q, re.I)
        if m:
            return m.group(1).strip().title()[:60]
        kw = keywords(q)
        return (" ".join(kw[:3]).title() or "New Measure")[:60]


def _tom_script(dataset_name: str, measure: str, expression: str) -> str:
    return f'''# Requires: pip install semantic-link-labs ; XMLA read/write ENABLED
# Run against a DEV workspace. Never prod. Human approval required.
import sempy_labs as labs
from sempy_labs.tom import connect_semantic_model

DATASET = "{dataset_name}"
WORKSPACE = "YOUR-DEV-WORKSPACE"

with connect_semantic_model(dataset=DATASET, workspace=WORKSPACE,
                            readonly=False) as tom:
    tom.add_measure(
        table_name="FACT_SALES",          # <-- confirm the home table
        measure_name="{measure}",
        expression="""{expression}""",
        format_string="#,0.00",
        description="Created by the agent; reviewed by <your name> on <date>",
    )
# Then refresh the model and validate against a known-good number.
'''


class ReportAgent:
    """Generates a downloadable PBIP the user opens in Power BI Desktop.

    Deliberately does NOT publish. No workspace write permission, no approval
    gate, no blast radius -- the worst case is a file someone deletes. The
    report live-connects to the semantic model, so it carries no data and RLS
    still applies when it is opened.

    Set REPORT_MODE=publish to get the old clone+rebind behaviour instead.
    """

    def __init__(self, gov: GovernanceAgent):
        self.gov = gov
        self.snap = gov.snap

    def run(self, ctx: TurnContext) -> TurnContext:
        if os.getenv("REPORT_MODE", "download").lower() == "publish":
            return self._plan_publish(ctx)
        return self._plan_download(ctx)

    # -- download (default) ------------------------------------------------
    def _plan_download(self, ctx: TurnContext) -> TurnContext:
        ds = self.gov._resolve_dataset(ctx, " ".join(keywords(ctx.question)[:3]))
        if not ds:
            ctx.errors.append(
                "I could not tell which semantic model you mean. Name it, "
                "e.g. 'build a report on store performance from Retail Sales'."
            )
            return ctx

        spec = R.plan_report(self.snap, ds, ctx.question)
        ctx.entities["plan"] = {
            "action": "generate_pbip",
            "report_name": spec["report_name"],
            "dataset_id": ds,
            "dataset_name": spec["dataset_name"],
            "workspace_name": spec["workspace_name"],
            "fields_used": spec["fields_used"],
            "visual_count": spec["visual_count"],
            "question": ctx.question,
        }
        ctx.requires_approval = False      # nothing is mutated; just download
        ctx.add(f"report grounded in {spec['dataset_name']}", "plan")
        return ctx

    # -- publish (opt-in) --------------------------------------------------
    def _plan_publish(self, ctx: TurnContext) -> TurnContext:
        ds = self.gov._resolve_dataset(ctx, " ".join(keywords(ctx.question)[:3]))

        # Resolve a REAL template from the snapshot instead of emitting a
        # placeholder. A plan carrying <TEMPLATE_REPORT_ID> can never execute,
        # which is what made "create a report" look permanently broken.
        template_id = template_name = template_ws = None
        if ds:
            row = self.snap.execute(
                "SELECT report_id, name, workspace_id FROM reports "
                "WHERE dataset_id = ? ORDER BY name LIMIT 1", (ds,)
            ).fetchall()
            if row:
                template_id, template_name, template_ws = row[0]

        target_ws = ctx.workspace_id or os.getenv("PBI_DEV_WORKSPACE_ID") or template_ws
        subject = " ".join(keywords(ctx.question)[:3]).title() or "Report"

        plan: Dict[str, Any] = {
            "action": "clone_and_rebind",
            "template_report_id": template_id or "<TEMPLATE_REPORT_ID>",
            "template_report_name": template_name,
            "template_workspace_id": template_ws,
            "target_workspace_id": target_ws or "<DEV_WORKSPACE_ID>",
            "rebind_dataset_id": ds or "<DATASET_ID>",
            "new_report_name": f"{subject} (agent draft)",
            "steps": [
                "POST /reports/{templateId}/Clone  (name, targetWorkspaceId)",
                "POST /reports/{newId}/Rebind      (datasetId)",
                "GET  /reports/{newId}             (verify webUrl renders)",
                "Human reviews before sharing.",
            ],
        }
        ctx.entities["plan"] = plan
        ctx.requires_approval = True
        ctx.add("planned report clone+rebind", "plan")

        blocks = preflight(plan, ctx.obo_token, approve=False)
        # Drop the generic "not approved" line; the UI says that already.
        ctx.entities["blockers"] = [b for b in blocks
                                    if not b.startswith("Not approved")]
        if template_id:
            ctx.errors.append(
                f"NOT APPLIED. Template resolved: '{template_name}'. "
                f"Approve to execute the clone+rebind."
            )
        else:
            ctx.errors.append(
                "NOT APPLIED. No existing report is bound to that dataset, so "
                "there is nothing to clone. Create one curated template report "
                "first, then ask again."
            )
        return ctx


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class Orchestrator:
    def __init__(self, snapshot_path: Optional[str] = None):
        self.snap = get_snapshot(snapshot_path)
        self.gov = GovernanceAgent(self.snap)
        self.data = DataAgent(self.gov)
        self.authoring = AuthoringAgent(self.gov)
        self.report = ReportAgent(self.gov)

    def ask(self, question: str, user_upn: str,
            obo_token: Optional[str] = None) -> Dict[str, Any]:
        ctx = TurnContext(user_upn=user_upn, question=question, obo_token=obo_token)
        try:
            route(ctx)                                   # security choke point
        except AuthRequired as e:
            ctx.errors.append(str(e))
            return {**narrate(ctx), "auth_required": True, "audit": ctx.audit()}

        try:
            if ctx.plane is Plane.SNAPSHOT:
                self.gov.run(ctx)
            elif ctx.plane is Plane.LIVE:
                self.data.run(ctx)
            elif ctx.plane is Plane.WRITE:
                (self.authoring if ctx.intent is Intent.CREATE_MEASURE
                 else self.report).run(ctx)
        except Exception as exc:                          # never leak a stack
            log.exception("turn %s failed", ctx.turn_id)
            ctx.errors.append(f"{type(exc).__name__}: {exc}")

        out = narrate(ctx)
        out["audit"] = ctx.audit()
        if ctx.entities.get("blockers"):
            out["blockers"] = ctx.entities["blockers"]
        out["writes_enabled"] = writes_enabled()
        log.info("turn=%s plane=%s intent=%s rows=%s",
                 ctx.turn_id, ctx.plane, ctx.intent, ctx.result_rows)
        return out

    def approve(self, plan: Dict[str, Any], user_upn: str,
                obo_token: Optional[str] = None,
                dry_run: bool = False) -> Dict[str, Any]:
        """Execute a previously-proposed plan. This is the human gate.

        Call it from a route that requires an authenticated session. Never
        call it automatically from ask().
        """
        res = execute_plan(plan, obo_token, user_upn,
                           approve=True, dry_run=dry_run)
        return res.to_dict()
