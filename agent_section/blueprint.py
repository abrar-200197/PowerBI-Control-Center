"""
The /agent section, as a Flask blueprint.

Drop-in for an existing app. Three lines in app.py, nothing else touched:

    from agent_section.blueprint import agent_bp
    app.register_blueprint(agent_bp)

Everything here is a thin shim over agent_section.service, which holds the
actual logic and is tested independently of Flask.

TOKEN SOURCING
    The agent must run as the SIGNED-IN USER, not as the app, or row-level
    security silently stops applying. By default this reads the token your app
    already puts in the Flask session. If your app stores it elsewhere, set a
    custom resolver instead of editing this file:

        from agent_section.blueprint import set_token_resolver
        set_token_resolver(lambda: (my_upn(), my_pbi_token()))
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional, Tuple

from flask import (Blueprint, Response, current_app, jsonify, render_template,
                   request, session)

from agent_section import service

agent_bp = Blueprint("agent", __name__, url_prefix="/agent",
                     template_folder="templates")

# --- identity ---------------------------------------------------------------
_token_resolver: Optional[Callable[[], Tuple[str, Optional[str]]]] = None


def set_token_resolver(fn: Callable[[], Tuple[str, Optional[str]]]) -> None:
    """Register a callable returning (user_upn, power_bi_access_token)."""
    global _token_resolver
    _token_resolver = fn


def _identity() -> Tuple[str, Optional[str]]:
    if _token_resolver:
        return _token_resolver()
    upn = (session.get("user_upn") or session.get("upn")
           or session.get("preferred_username") or "unknown@local")
    token = (session.get("pbi_access_token") or session.get("access_token")
             or session.get("powerbi_token"))
    # Allow a proxy/gateway to forward the user assertion explicitly.
    hdr = request.headers.get("X-PBI-Access-Token")
    return upn, (hdr or token)


# --- snapshot ---------------------------------------------------------------
_snap = None


def _snapshot():
    """One long-lived read-only handle. Cheap, and the file only changes when
    the Sunday/6-hourly job swaps it in."""
    global _snap
    if _snap is None:
        from agent.db import Snapshot
        _snap = Snapshot(os.getenv("CATALOG_SNAPSHOT_PATH")
                         or _default_snapshot_path())
    return _snap


def _default_snapshot_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("catalog.duckdb", "catalog.sqlite"):
        p = os.path.join(here, "data", name)
        if os.path.exists(p):
            return p
    return os.path.join(here, "data", "catalog.sqlite")


def reset_snapshot() -> None:
    """Call after the snapshot file is replaced, to drop the stale handle."""
    global _snap
    if _snap is not None:
        try:
            _snap.close()
        except Exception:  # noqa: BLE001
            pass
    _snap = None


# --- routes -----------------------------------------------------------------


@agent_bp.get("/")
def page():
    return render_template("agent.html",
                           brain=service.brain_status())


@agent_bp.get("/api/models")
def api_models():
    search = (request.args.get("q") or "").strip()
    return jsonify({"models": service.list_models(_snapshot(), search)})


@agent_bp.get("/api/models/<dataset_id>")
def api_model(dataset_id: str):
    try:
        return jsonify(service.model_profile(_snapshot(), dataset_id))
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404


@agent_bp.get("/api/status")
def api_status():
    return jsonify(service.brain_status())


@agent_bp.post("/api/ask")
def api_ask():
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    upn, token = _identity()
    snap = _snapshot()          # resolve once; Snapshot.path is always set
    try:
        out = service.ask(
            question, user_upn=upn, user_token=token,
            dataset_id=body.get("dataset_id"),
            snapshot_path=snap.path,
            snap=snap,          # reuse the open handle; the loop brain needs it
            conversation_id=body.get("conversation_id"),
            brain=body.get("brain"),
        )
    except PermissionError as exc:
        return jsonify({"error": str(exc), "auth_required": True}), 403
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("agent ask failed")
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
    return jsonify(out)


@agent_bp.post("/api/report")
def api_report():
    """Generate a PBIP and stream it back. Publishes nothing."""
    body = request.get_json(silent=True) or {}
    dataset_id = body.get("dataset_id")
    if not dataset_id:
        return jsonify({"error": "dataset_id is required"}), 400
    try:
        blob, meta = service.build_report(
            _snapshot(), dataset_id,
            body.get("question") or "report",
            body.get("report_name"))
    except (ValueError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("report build failed")
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    return Response(
        blob, mimetype="application/zip",
        headers={
            "Content-Disposition":
                f'attachment; filename="{meta["file_name"]}"',
            "X-Report-Meta": json.dumps({
                "fields_used": meta["fields_used"],
                "visual_count": meta["visual_count"]}),
        })


@agent_bp.get("/api/health")
def api_health():
    try:
        n = len(service.list_models(_snapshot(), limit=1))
        snap_ok = True
    except Exception as exc:  # noqa: BLE001
        n, snap_ok = 0, False
        current_app.logger.warning("snapshot unavailable: %s", exc)
    return jsonify({"ok": snap_ok, "snapshot_readable": snap_ok,
                    "models_visible": n, "brain": service.brain_status()})
