"""
The write plane's execution half.

The agents in orchestrator.py PLAN. Nothing in this file runs unless a human
calls execute_plan() with approve=True. That split is the whole point: an LLM
decides *what* to propose, a person decides *whether* it happens.

Guardrails, in the order they fire:
  1. approve=True must be passed explicitly. Default is a dry run.
  2. The target workspace must be on the WRITE_ALLOWLIST. Empty allowlist =
     nothing is writable. Your prod workspace should never be on it.
  3. A delegated (OBO) token is required. Writes run as the USER, so Power BI
     enforces their workspace permissions -- the agent cannot grant access
     the requester does not already have.
  4. DEMO_MODE=1 simulates everything and touches no tenant.

Two operations are supported:
  create_report  -> POST Clone + POST Rebind   (REST, safe, reversible)
  create_measure -> TOM via XMLA               (NOT reversible -- see warning)
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("agent.executor")

PBI_API = os.getenv("PBI_API_BASE", "https://api.powerbi.com/v1.0/myorg")
HTTP_TIMEOUT = int(os.getenv("PBI_TIMEOUT", "180"))


class NotApproved(Exception):
    """execute_plan() was called without approve=True."""


class WorkspaceNotAllowed(Exception):
    """Target workspace is not on the write allowlist."""


class ExecutionFailed(Exception):
    """The tenant rejected the write."""


def write_allowlist() -> List[str]:
    """Workspace IDs this agent may write to. Empty = writes disabled.

    Set PBI_WRITE_WORKSPACES to a comma-separated list of workspace GUIDs.
    Deliberately opt-in: a fresh install cannot write anywhere.
    """
    raw = os.getenv("PBI_WRITE_WORKSPACES", "").strip()
    return [w.strip() for w in raw.split(",") if w.strip()]


def writes_enabled() -> bool:
    return bool(write_allowlist())


@dataclass
class ExecutionResult:
    ok: bool
    action: str
    dry_run: bool
    steps: List[Dict[str, Any]] = field(default_factory=list)
    artifact_id: Optional[str] = None
    artifact_url: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None
    audit_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "dry_run": self.dry_run,
            "steps": self.steps,
            "artifact_id": self.artifact_id,
            "artifact_url": self.artifact_url,
            "warnings": self.warnings,
            "error": self.error,
            "audit_id": self.audit_id,
        }


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _post(url: str, token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    import requests  # lazy: DEMO_MODE needs no network stack

    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json=body,
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        raise ExecutionFailed(f"HTTP {r.status_code} {url.rsplit('/', 1)[-1]}: "
                              f"{r.text[:400]}")
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return {}


def _get(url: str, token: str) -> Dict[str, Any]:
    import requests

    r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                     timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise ExecutionFailed(f"HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def preflight(plan: Dict[str, Any], obo_token: Optional[str],
              approve: bool) -> List[str]:
    """Returns blocking reasons. Empty list means it is safe to execute."""
    blocks: List[str] = []

    if not approve:
        blocks.append("Not approved. Pass approve=true to execute.")

    if not obo_token:
        blocks.append(
            "No delegated token. Writes run as you, so Power BI can enforce "
            "your workspace permissions. Sign in first."
        )

    target = plan.get("target_workspace_id") or ""
    allow = write_allowlist()
    if not allow:
        blocks.append(
            "Writes are disabled. Set PBI_WRITE_WORKSPACES to a comma-separated "
            "list of DEV workspace GUIDs. Never add a production workspace."
        )
    elif target not in allow:
        blocks.append(
            f"Workspace {target or '(unset)'} is not on the write allowlist "
            f"({len(allow)} allowed). Refusing to write outside it."
        )

    unresolved = [k for k, v in plan.items()
                  if isinstance(v, str) and v.startswith("<") and v.endswith(">")]
    if unresolved:
        blocks.append(
            "Plan has unresolved placeholders: " + ", ".join(sorted(unresolved))
            + ". Supply real IDs before executing."
        )
    return blocks


# ---------------------------------------------------------------------------
# create_report : Clone + Rebind
# ---------------------------------------------------------------------------
def _execute_clone_rebind(plan: Dict[str, Any], token: str,
                          dry_run: bool) -> ExecutionResult:
    res = ExecutionResult(ok=False, action="clone_and_rebind", dry_run=dry_run)

    tmpl_ws = plan.get("template_workspace_id") or plan["target_workspace_id"]
    tmpl_id = plan["template_report_id"]
    target_ws = plan["target_workspace_id"]
    dataset_id = plan["rebind_dataset_id"]
    new_name = plan.get("new_report_name") or "Agent Generated Report"

    clone_url = f"{PBI_API}/groups/{tmpl_ws}/reports/{tmpl_id}/Clone"
    clone_body = {"name": new_name, "targetWorkspaceId": target_ws}

    if dry_run:
        res.steps = [
            {"step": "POST " + clone_url, "body": clone_body, "status": "dry-run"},
            {"step": "POST /reports/{newId}/Rebind",
             "body": {"datasetId": dataset_id}, "status": "dry-run"},
            {"step": "GET /reports/{newId}", "status": "dry-run"},
        ]
        res.ok = True
        res.warnings.append("Dry run. Nothing was created.")
        return res

    if os.getenv("DEMO_MODE", "1") == "1":
        new_id = "demo-" + uuid.uuid4().hex[:8]
        res.steps = [
            {"step": "Clone", "status": "simulated", "new_report_id": new_id},
            {"step": "Rebind", "status": "simulated", "dataset_id": dataset_id},
            {"step": "Verify", "status": "simulated"},
        ]
        res.artifact_id = new_id
        res.artifact_url = f"https://app.powerbi.com/groups/{target_ws}/reports/{new_id}"
        res.ok = True
        res.warnings.append(
            "DEMO_MODE: simulated. No report was created in your tenant. "
            "Set DEMO_MODE=0 with a real token to actually create it."
        )
        return res

    # --- real tenant ------------------------------------------------------
    t0 = time.perf_counter()
    cloned = _post(clone_url, token, clone_body)
    new_id = cloned.get("id")
    if not new_id:
        raise ExecutionFailed(f"Clone returned no report id: {cloned}")
    res.steps.append({"step": "Clone", "status": "ok", "new_report_id": new_id,
                      "ms": int((time.perf_counter() - t0) * 1000)})

    # Rebind is a separate call: Clone can take a datasetId, but rebinding
    # explicitly is clearer and works when cloning across workspaces.
    t1 = time.perf_counter()
    _post(f"{PBI_API}/groups/{target_ws}/reports/{new_id}/Rebind",
          token, {"datasetId": dataset_id})
    res.steps.append({"step": "Rebind", "status": "ok",
                      "dataset_id": dataset_id,
                      "ms": int((time.perf_counter() - t1) * 1000)})

    meta = _get(f"{PBI_API}/groups/{target_ws}/reports/{new_id}", token)
    res.steps.append({"step": "Verify", "status": "ok",
                      "web_url": meta.get("webUrl")})
    res.artifact_id = new_id
    res.artifact_url = meta.get("webUrl")
    res.ok = True
    res.warnings.append(
        "Created from a template. Visuals reflect the TEMPLATE's fields; "
        "rebinding does not reshape them. Open it and confirm before sharing."
    )
    return res


# ---------------------------------------------------------------------------
# create_measure : TOM over XMLA
# ---------------------------------------------------------------------------
def _execute_create_measure(plan: Dict[str, Any], token: str,
                            dry_run: bool) -> ExecutionResult:
    res = ExecutionResult(ok=False, action="create_measure", dry_run=dry_run)
    ws = plan["target_workspace_id"]
    model = plan.get("dataset_name") or plan.get("rebind_dataset_id")
    table = plan.get("table_name") or "<TABLE>"
    name = plan.get("measure_name") or "<MEASURE>"
    expr = plan.get("expression") or ""

    res.warnings.append(
        "XMLA writes break PBIX round-tripping permanently: after this, the "
        "original .pbix can no longer be safely re-published over the model."
    )

    if dry_run:
        res.steps = [{"step": "connect_semantic_model(readonly=False)",
                      "status": "dry-run"},
                     {"step": f"add_measure {table}[{name}]",
                      "expression": expr, "status": "dry-run"}]
        res.ok = True
        res.warnings.append("Dry run. The model was not modified.")
        return res

    if os.getenv("DEMO_MODE", "1") == "1":
        res.steps = [{"step": "connect_semantic_model", "status": "simulated"},
                     {"step": f"add_measure {table}[{name}]",
                      "expression": expr, "status": "simulated"}]
        res.artifact_id = f"{table}[{name}]"
        res.ok = True
        res.warnings.append(
            "DEMO_MODE: simulated. Your model was not modified."
        )
        return res

    try:
        import sempy_labs
        from sempy_labs.tom import connect_semantic_model
    except ImportError as e:
        raise ExecutionFailed(
            "semantic-link-labs is not installed. pip install semantic-link-labs. "
            f"({e})"
        )

    t0 = time.perf_counter()
    with connect_semantic_model(dataset=model, workspace=ws,
                                readonly=False) as tom:
        existing = {m.Name for m in tom.all_measures()}
        if name in existing:
            raise ExecutionFailed(
                f"Measure '{name}' already exists in {model}. Refusing to "
                f"overwrite. Rename it or delete the existing one first."
            )
        tom.add_measure(table_name=table, measure_name=name, expression=expr)

    res.steps.append({"step": f"add_measure {table}[{name}]", "status": "ok",
                      "ms": int((time.perf_counter() - t0) * 1000)})
    res.artifact_id = f"{table}[{name}]"
    res.ok = True
    res.warnings.append(
        "Applied. Validate the measure returns sensible values before anyone "
        "builds on it."
    )
    return res


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
_HANDLERS = {
    "clone_and_rebind": _execute_clone_rebind,
    "create_measure": _execute_create_measure,
}


def execute_plan(plan: Dict[str, Any], obo_token: Optional[str],
                 user_upn: str, approve: bool = False,
                 dry_run: bool = False) -> ExecutionResult:
    """Execute a plan produced by the write-plane agents.

    approve=False (the default) refuses. This is intentional: calling this
    function by accident must never mutate a tenant.
    """
    action = (plan or {}).get("action", "")
    handler = _HANDLERS.get(action)
    if handler is None:
        return ExecutionResult(ok=False, action=action or "unknown",
                               dry_run=dry_run,
                               error=f"No executor for action '{action}'.")

    if not dry_run:
        blocks = preflight(plan, obo_token, approve)
        if blocks:
            r = ExecutionResult(ok=False, action=action, dry_run=False,
                                error=" ".join(blocks))
            r.warnings = blocks
            return r

    log.info("EXECUTE action=%s user=%s dry_run=%s ws=%s",
             action, user_upn, dry_run, plan.get("target_workspace_id"))
    try:
        res = handler(plan, obo_token or "", dry_run)
    except Exception as exc:
        log.exception("execution failed")
        return ExecutionResult(ok=False, action=action, dry_run=dry_run,
                               error=f"{type(exc).__name__}: {exc}")

    # Audit line. Never log the token.
    log.info("RESULT %s", json.dumps({
        "audit_id": res.audit_id, "action": action, "user": user_upn,
        "ok": res.ok, "dry_run": dry_run, "artifact": res.artifact_id,
    }))
    return res
