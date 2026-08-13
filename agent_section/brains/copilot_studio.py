"""
Copilot Studio brain -- direct-to-engine, via the Microsoft 365 Agents SDK.

    pip install microsoft-agents-copilotstudio-client

WHY THIS PATH
    You get Copilot's own reasoning, its DAX generation engine, and whatever
    knowledge sources and MCP tools you wired into the agent in Copilot Studio,
    without reimplementing any of it in Python.

THE ONE HARD CONSTRAINT
    The client requires a USER token. Service-to-service is not supported yet.
    Practically:
      - every answer is scoped to the signed-in user, so RLS applies. Good.
      - your daemon jobs (Sunday rebuild, 6-hourly delta) CANNOT call this.
        Leave them on plain Python writing JSON to SharePoint.

SETUP (once)
    1. Build + PUBLISH the agent in Copilot Studio.
    2. Settings -> Advanced -> Metadata: copy Schema name + Environment ID.
    3. Entra app registration, Public client/native, redirect http://localhost:
         Power Platform API -> CopilotStudio.Copilots.Invoke   (delegated)
         Microsoft Graph    -> User.Read                       (delegated)
    4. Set the four COPILOTSTUDIOAGENT__* environment variables.

The token flow is left injectable: in a web app you already have the user's
token from your own sign-in, and you should pass that rather than triggering an
interactive browser login on your server.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

_MISSING = (
    "microsoft-agents-copilotstudio-client is not installed. "
    "Run: pip install microsoft-agents-copilotstudio-client"
)


def _env(key: str) -> str:
    v = (os.getenv(key) or "").strip()
    if v:
        return v
    if key == "COPILOTSTUDIOAGENT__TENANTID":
        return (os.getenv("TENANT_ID") or "").strip()
    if key == "COPILOTSTUDIOAGENT__AGENTAPPID":
        return (os.getenv("CLIENT_ID") or "").strip()
    return ""


def _settings():
    from microsoft_agents.copilotstudio.client import ConnectionSettings
    direct_url = (os.getenv("COPILOTSTUDIOAGENT__DIRECTCONNECTURL") or "").strip()
    if direct_url:
        # DirectConnect mode: env id / schema name are not needed.
        return ConnectionSettings(environment_id="", agent_identifier="",
                                  direct_connect_url=direct_url)
    env_id = _env("COPILOTSTUDIOAGENT__ENVIRONMENTID")
    schema = _env("COPILOTSTUDIOAGENT__SCHEMANAME")
    if not env_id or not schema:
        raise RuntimeError(
            "Set COPILOTSTUDIOAGENT__ENVIRONMENTID and "
            "COPILOTSTUDIOAGENT__SCHEMANAME (from Studio publish / embed URL)"
        )
    return ConnectionSettings(
        environment_id=env_id,
        agent_identifier=schema,
        cloud=None, copilot_agent_type=None, custom_power_platform_cloud=None,
    )


def _client(user_token: str):
    try:
        from microsoft_agents.copilotstudio.client import CopilotClient
    except ImportError as exc:  # pragma: no cover - depends on optional dep
        raise RuntimeError(_MISSING) from exc
    return CopilotClient(_settings(), user_token)


def ask(question: str, *, user_upn: str, user_token: str,
        dataset_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """Synchronous wrapper -- Flask is sync, the SDK is async."""
    timeout_s = timeout_s or float(os.getenv("AGENT_TIMEOUT", "120"))
    return asyncio.run(_ask_async(question, user_upn, user_token,
                                  dataset_id, conversation_id, timeout_s))


async def _ask_async(question, user_upn, user_token, dataset_id,
                     conversation_id, timeout_s) -> Dict[str, Any]:
    client = _client(user_token)

    # Pin the model the user picked in the UI. The agent should read this from
    # conversation context rather than re-guessing which model was meant.
    prompt = question
    if dataset_id:
        prompt = f"{question}\n\n[semantic model id: {dataset_id}]"

    texts: List[str] = []
    suggested: List[str] = []
    conv_id = conversation_id

    async def _run():
        nonlocal conv_id
        from microsoft_agents.activity import ActivityTypes

        if not conv_id:
            async for act in client.start_conversation(
                    emit_start_conversation_event=True):
                if getattr(act, "conversation", None) is not None:
                    conv_id = getattr(act.conversation, "id", None) or conv_id
                if act.type == ActivityTypes.message and act.text:
                    # greeting -- keep it out of the answer body
                    pass

        async for reply in client.ask_question(prompt, conv_id):
            if reply.type == ActivityTypes.message and reply.text:
                texts.append(reply.text)
            for a in (getattr(reply, "suggested_actions", None) or []):
                title = getattr(a, "title", None)
                if title:
                    suggested.append(title)

    await asyncio.wait_for(_run(), timeout=timeout_s)

    return {
        "brain": "copilot",
        "plane": "live",
        "answer": "\n\n".join(texts).strip() or "(no response from the agent)",
        "conversation_id": conv_id,
        "suggested_actions": suggested,
        "rows": [], "row_count": 0, "citations": [], "dax": None,
        "requires_approval": False, "plan": None, "warnings": [],
    }


def health() -> Dict[str, Any]:
    """Config check that does NOT need a user token -- safe for /health."""
    try:
        import microsoft_agents.copilotstudio.client  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    keys = ("COPILOTSTUDIOAGENT__ENVIRONMENTID", "COPILOTSTUDIOAGENT__SCHEMANAME",
            "COPILOTSTUDIOAGENT__TENANTID", "COPILOTSTUDIOAGENT__AGENTAPPID")
    missing = [k for k in keys if not _env(k)]
    return {
        "sdk_installed": installed,
        "missing_env": missing,
        "environment_id": _env("COPILOTSTUDIOAGENT__ENVIRONMENTID")[:12] + "…"
            if _env("COPILOTSTUDIOAGENT__ENVIRONMENTID") else "",
        "schema_name": _env("COPILOTSTUDIOAGENT__SCHEMANAME"),
        "direct_connect": bool(os.getenv("COPILOTSTUDIOAGENT__DIRECTCONNECTURL")),
        "ready": installed and not missing,
    }
