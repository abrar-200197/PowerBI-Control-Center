"""
Agent section -- pure logic, no web framework.

Everything the /agent section does lives here as plain functions so it can be
tested without Flask and reused from FastAPI, a CLI, or a background job. The
Flask layer (blueprint.py) is a thin shim that does nothing but unpack the
request, call in here, and jsonify the result.

The brain is pluggable, because the right backend depends on what your tenant
has switched on today:

  AGENT_BRAIN=copilot   Copilot Studio agent via microsoft-agents-copilotstudio-client
  AGENT_BRAIN=mcp       Power BI remote MCP server (Copilot's own DAX engine)
  AGENT_BRAIN=local     the orchestrator in this package (no tenant needed)
  AGENT_BRAIN=auto      (default) copilot -> mcp -> local, first one configured

'local' always works, which is what makes this testable and what stops a
preview feature outage from taking your Agent tab down.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Model picker
# ---------------------------------------------------------------------------


def _escape_like(s: str) -> str:
    """Neutralise SQL LIKE wildcards in user-typed search text."""
    return (s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))


def list_models(snap, search: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    """Semantic models the user can pick from.

    Read from the governance snapshot rather than the REST API: it is already
    in SharePoint, it is indexed, and it means the picker renders instantly
    instead of waiting on a tenant round-trip.
    """
    sql = """
        SELECT d.dataset_id, d.name, d.workspace_id, d.workspace_name,
               (SELECT COUNT(*) FROM measures m WHERE m.dataset_id = d.dataset_id),
               (SELECT COUNT(*) FROM tables t   WHERE t.dataset_id = d.dataset_id),
               (SELECT COUNT(*) FROM reports r  WHERE r.dataset_id = d.dataset_id)
        FROM datasets d
    """
    params: tuple = ()
    if search:
        # Escape LIKE wildcards. Without this, a user typing "%" matches every
        # model, and -- far more likely here -- an underscore in a real model
        # name ("FACT_SALES") would behave as a single-character wildcard.
        sql += (" WHERE LOWER(d.name) LIKE ? ESCAPE '\\'"
                " OR LOWER(d.workspace_name) LIKE ? ESCAPE '\\'")
        like = "%" + _escape_like(search.lower()) + "%"
        params = (like, like)
    sql += " ORDER BY d.workspace_name, d.name LIMIT ?"
    params = params + (int(limit),)

    out = []
    for row in snap.execute(sql, params).fetchall():
        out.append({
            "dataset_id": row[0],
            "name": row[1],
            "workspace_id": row[2],
            "workspace_name": row[3],
            "measure_count": row[4],
            "table_count": row[5],
            "report_count": row[6],
        })
    return out


def model_profile(snap, dataset_id: str) -> Dict[str, Any]:
    """What the user sees after picking a model: its shape, and its provenance.

    Showing the source systems and the snapshot age up front is deliberate --
    it is the difference between "the agent said 4.2M" and "the agent said 4.2M
    from a model fed by these two systems, scanned this morning".
    """
    row = snap.execute(
        "SELECT dataset_id, name, workspace_id, workspace_name "
        "FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    if not row:
        raise LookupError(f"dataset {dataset_id} is not in the snapshot")

    measures = [
        {"table": t, "name": n, "expression": e}
        for t, n, e in snap.execute(
            "SELECT table_name, measure_name, expression FROM measures "
            "WHERE dataset_id = ? ORDER BY table_name, measure_name",
            (dataset_id,)).fetchall()
    ]
    tables = [r[0] for r in snap.execute(
        "SELECT table_name FROM tables WHERE dataset_id = ? ORDER BY table_name",
        (dataset_id,)).fetchall()]
    sources = [
        {"kind": k, "server": s, "database": d}
        for k, s, d in snap.execute(
            "SELECT datasource_type, server, database FROM datasources "
            "WHERE dataset_id = ?", (dataset_id,)).fetchall()
    ]
    reports = [
        {"report_id": i, "name": n}
        for i, n in snap.execute(
            "SELECT report_id, name FROM reports WHERE dataset_id = ? "
            "ORDER BY name", (dataset_id,)).fetchall()
    ]

    as_of = _manifest(snap)

    return {
        "dataset_id": row[0],
        "name": row[1],
        "workspace_id": row[2],
        "workspace_name": row[3],
        "tables": tables,
        "measures": measures,
        "datasources": sources,
        "reports": reports,
        "snapshot": as_of,
        "suggestions": _suggestions(row[1], measures, tables),
    }


def _manifest(snap) -> Optional[Dict[str, Any]]:
    """Snapshot provenance, shown next to every answer.

    The column is `built_at_utc`. This was originally wrapped in a bare
    `except Exception: pass`, which meant a column-name typo showed up as a
    silently missing timestamp instead of an error -- so the UI quietly stopped
    telling users how stale their metadata was. Only a genuinely absent
    manifest table is tolerated now.
    """
    try:
        row = snap.execute(
            "SELECT build_id, built_at_utc, mode, status FROM manifest "
            "LIMIT 1").fetchone()
    except Exception as exc:  # noqa: BLE001
        if "no such table" in str(exc).lower():
            return None
        raise
    if not row:
        return None
    return {"build_id": row[0], "built_utc": row[1],
            "mode": row[2], "status": row[3]}


def _suggestions(model_name: str, measures, tables) -> List[str]:
    """Concrete starter questions built from THIS model's real fields.

    Generic examples ("try asking about your data!") teach the user nothing.
    Naming a real measure shows them the agent already knows the model.
    """
    out: List[str] = []
    if measures:
        lead = measures[0]["name"]
        out.append(f"What is {lead}?")
        out.append(f"Show me {lead} by month")
        out.append(f"How is {lead} calculated?")
        if len(measures) > 1:
            out.append(f"Build me a report on {lead} and {measures[1]['name']}")
    out.append(f"What sources feed {model_name}?")
    out.append(f"Which reports use {model_name}?")
    return out[:6]


# ---------------------------------------------------------------------------
# Brain selection
# ---------------------------------------------------------------------------
BRAIN_COPILOT, BRAIN_MCP, BRAIN_LOCAL = "copilot", "mcp", "local"
BRAIN_LOOP = "loop"     # LLM in a loop with tools -- a real agent

_COPILOT_KEYS = (
    "COPILOTSTUDIOAGENT__ENVIRONMENTID",
    "COPILOTSTUDIOAGENT__SCHEMANAME",
    "COPILOTSTUDIOAGENT__TENANTID",
    "COPILOTSTUDIOAGENT__AGENTAPPID",
)


def _copilot_env(key: str) -> str:
    """Studio vars, with TENANT_ID / CLIENT_ID fallbacks for the shared app reg."""
    v = (os.getenv(key) or "").strip()
    if v:
        return v
    if key == "COPILOTSTUDIOAGENT__TENANTID":
        return (os.getenv("TENANT_ID") or "").strip()
    if key == "COPILOTSTUDIOAGENT__AGENTAPPID":
        return (os.getenv("CLIENT_ID") or os.getenv("COPILOTSTUDIOAGENT__CLIENTID") or "").strip()
    return ""


def _copilot_configured() -> bool:
    # Direct-connect URL alone is enough for the SDK settings path
    if (os.getenv("COPILOTSTUDIOAGENT__DIRECTCONNECTURL") or "").strip():
        return True
    return all(_copilot_env(k) for k in _COPILOT_KEYS)


def _mcp_configured() -> bool:
    return bool(os.getenv("PBI_MCP_ENDPOINT"))


def _loop_configured() -> bool:
    """Our own agent loop needs an LLM to be the brain."""
    return all(os.getenv(k) for k in (
        "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_API_KEY"))


def resolve_brain(requested: Optional[str] = None) -> str:
    """Pick the backend. Explicit request wins, but never silently: asking for
    an unconfigured brain falls back to local rather than erroring, so the tab
    still works while an admin is still enabling a preview feature."""
    want = (requested or os.getenv("AGENT_BRAIN") or "auto").lower()
    if want == BRAIN_COPILOT and _copilot_configured():
        return BRAIN_COPILOT
    if want == BRAIN_MCP and _mcp_configured():
        return BRAIN_MCP
    if want == BRAIN_LOOP and _loop_configured():
        return BRAIN_LOOP
    if want == BRAIN_LOCAL:
        return BRAIN_LOCAL
    if want == "auto":
        if _copilot_configured():
            return BRAIN_COPILOT
        if _loop_configured():
            return BRAIN_LOOP     # a real agent beats the rule engine
        if _mcp_configured():
            return BRAIN_MCP
    return BRAIN_LOCAL


def brain_status() -> Dict[str, Any]:
    """Surfaced in the UI so it is never a mystery which brain answered."""
    active = resolve_brain()
    return {
        "active": active,
        "copilot_studio_configured": _copilot_configured(),
        "mcp_configured": _mcp_configured(),
        "loop_configured": _loop_configured(),
        "requested": (os.getenv("AGENT_BRAIN") or "auto").lower(),
        "missing_copilot_keys": [k for k in _COPILOT_KEYS if not _copilot_env(k)],
        "is_real_agent": active in (BRAIN_COPILOT, BRAIN_LOOP),
        "note": {
            BRAIN_COPILOT: "Copilot Studio agent (direct-to-engine, runs as the "
                           "signed-in user)",
            BRAIN_LOOP: "LLM in a loop with tools — plans, chains tool calls "
                        "and self-corrects",
            BRAIN_MCP: "Power BI remote MCP server (Copilot's DAX engine)",
            BRAIN_LOCAL: "Rule-based fallback — regex routing, single pass, "
                         "no LLM. Set AZURE_OPENAI_* for the real agent.",
        }[active],
    }


# ---------------------------------------------------------------------------
# Ask
# ---------------------------------------------------------------------------


def ask(question: str, *, user_upn: str, user_token: Optional[str],
        dataset_id: Optional[str] = None, snapshot_path: Optional[str] = None,
        conversation_id: Optional[str] = None,
        brain: Optional[str] = None, snap=None) -> Dict[str, Any]:
    """Answer one question about one semantic model.

    dataset_id is threaded through because the user already picked a model in
    the UI -- making the agent re-guess it from the text would be a needless
    way to get the wrong model.
    """
    if not (question or "").strip():
        raise ValueError("question is empty")

    chosen = resolve_brain(brain)
    if chosen == BRAIN_COPILOT:
        return _ask_copilot_studio(question, user_upn, user_token,
                                   dataset_id, conversation_id)
    if chosen == BRAIN_MCP:
        return _ask_mcp(question, user_upn, user_token, dataset_id)
    if chosen == BRAIN_LOOP:
        return _ask_loop(question, user_upn, user_token, dataset_id,
                         snapshot_path, snap=snap)
    return _ask_local(question, user_upn, user_token, dataset_id, snapshot_path)


def _ask_loop(question, user_upn, user_token, dataset_id, snapshot_path,
              snap=None):
    """The real agent: an LLM decides which tools to call, sees the results,
    and calls more until it can answer.

    Falls back to the rule-based path if the LLM is unreachable -- an Azure
    outage should degrade the answer quality, not take the Agent tab down.
    """
    from agent.db import Snapshot
    from agent.llm import LLMUnavailable, default_llm
    from agent.loop import AgentContext, run_agent

    llm = default_llm()
    if llm is None:
        return _ask_local(question, user_upn, user_token, dataset_id,
                          snapshot_path)

    conn = snap or Snapshot(snapshot_path)
    ctx = AgentContext(conn=conn, user_upn=user_upn, user_token=user_token,
                       allow_write=True, dataset_id=dataset_id)
    try:
        res = run_agent(llm, ctx, question)
    except LLMUnavailable as exc:
        out = _ask_local(question, user_upn, user_token, dataset_id,
                         snapshot_path)
        out.setdefault("warnings", []).append(
            f"LLM unavailable ({exc}); answered with the rule-based fallback.")
        return out

    artifact = res.artifacts[0] if res.artifacts else None
    plan = None
    if artifact:
        plan = {"action": "generate_pbip", "dataset_id": dataset_id,
                "question": question,
                "report_name": artifact["meta"].get("report_name")}

    return {
        "brain": BRAIN_LOOP,
        "plane": res.plane,
        "answer": res.answer,
        "rows": [], "row_count": 0,
        "citations": [f"{t['tool']}({t['plane']})" for t in res.tool_calls],
        "dax": next((t["args"].get("dax") for t in reversed(res.tool_calls)
                     if t["tool"] == "run_dax"), None),
        "requires_approval": False,
        "plan": plan,
        "steps": res.steps,
        "tool_calls": res.tool_calls,
        "warnings": ([] if res.stopped_because == "answered"
                     else [f"stopped early: {res.stopped_because}"]),
    }


def _ask_local(question, user_upn, user_token, dataset_id, snapshot_path):
    from agent.orchestrator import Orchestrator
    orc = Orchestrator(snapshot_path)
    out = orc.ask(question, user_upn, user_token)
    out["brain"] = BRAIN_LOCAL
    # The user already picked the model in the UI. Honour that choice over
    # whatever the router inferred from the text.
    if dataset_id and isinstance(out.get("plan"), dict):
        out["plan"]["dataset_id"] = dataset_id
    return out


def _ask_copilot_studio(question, user_upn, user_token, dataset_id,
                        conversation_id):
    """Copilot Studio direct-to-engine.

    NOTE: the Copilot Studio client requires a USER token -- service-to-service
    is not supported yet. That is a feature here rather than a limitation: it
    means every answer is scoped by the caller's own permissions and RLS
    applies automatically. It also means unattended/daemon callers cannot use
    this path at all, so keep your Sunday rebuild on plain Python.
    """
    if not user_token:
        return _auth_required(question, BRAIN_COPILOT)

    from agent_section.brains import copilot_studio  # lazy: optional dependency
    return copilot_studio.ask(question, user_upn=user_upn,
                              user_token=user_token, dataset_id=dataset_id,
                              conversation_id=conversation_id)


def _ask_mcp(question, user_upn, user_token, dataset_id):
    if not user_token:
        return _auth_required(question, BRAIN_MCP)
    from agent_section.brains import mcp_remote  # lazy: optional dependency
    return mcp_remote.ask(question, user_upn=user_upn, user_token=user_token,
                          dataset_id=dataset_id)


def _auth_required(question: str, brain: str) -> Dict[str, Any]:
    """Fail closed, and say why.

    Never fall back to the governance snapshot for a data question. The
    snapshot is admin-collected metadata with no RLS -- answering from it would
    silently hand the user numbers they may not be entitled to see.
    """
    return {
        "brain": brain,
        "plane": "live",
        "answer": (
            "**Sign-in required.** This is a data question, so it has to run "
            "against the semantic model using *your* credentials — that is what "
            "makes row-level security apply.\n\n"
            "I will not answer it from the governance snapshot: that snapshot "
            "is admin-collected metadata with no RLS, so using it here would "
            "bypass your organisation's data access rules."
        ),
        "rows": [], "row_count": 0, "citations": [], "dax": None,
        "requires_approval": False, "plan": None,
        "warnings": ["no delegated user token on the request"],
        "auth_required": True,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def build_report(snap, dataset_id: str, question: str,
                 report_name: Optional[str] = None):
    """Generate a downloadable PBIP. Returns (zip_bytes, meta).

    Deliberately NOT a publish: the file live-connects to the model, carries no
    data, needs no workspace write permission, and RLS applies when the user
    opens it. Nothing here can damage a workspace.
    """
    from agent import report_builder as R
    return R.generate(snap, dataset_id, question, report_name)
