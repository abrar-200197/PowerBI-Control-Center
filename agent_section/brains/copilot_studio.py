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
    aliases = {
        "COPILOTSTUDIOAGENT__ENVIRONMENTID": (
            "COPILOT_ENVIRONMENT_ID", "COPILOTSTUDIO_ENVIRONMENTID",
        ),
        "COPILOTSTUDIOAGENT__SCHEMANAME": (
            "COPILOT_SCHEMA_NAME", "COPILOTSTUDIO_SCHEMANAME",
        ),
        "COPILOTSTUDIOAGENT__TENANTID": (
            "COPILOT_TENANT_ID", "TENANT_ID", "AZURE_TENANT_ID",
        ),
        "COPILOTSTUDIOAGENT__AGENTAPPID": (
            "COPILOT_AGENT_APP_ID", "COPILOTSTUDIOAGENT__CLIENTID",
            "CLIENT_ID", "AZURE_CLIENT_ID",
        ),
    }
    for name in aliases.get(key, ()):
        x = (os.getenv(name) or "").strip()
        if x:
            return x
    return ""


def _normalize_environment_id(raw: str) -> str:
    """Power Platform data-plane host uses the env GUID **without** dashes.

    Studio embed URLs look like:
      .../environments/Default-<guid>/bots/<schema>/...
    App settings often store the dashed GUID or ``Default-<guid>``.

    Host must be:
      {id_no_dashes[:-2]}.{id_no_dashes[-2:]}.environment.api.powerplatform.com
    NOT:
      {id_no_dashes}.environment.api.powerplatform.com   ← DNS NXDOMAIN
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # Strip common Studio URL prefixes / path crumbs
    lower = s.lower()
    for prefix in ("default-", "environments/", "/environments/"):
        if lower.startswith(prefix):
            s = s[len(prefix) :]
            lower = s.lower()
    # If a full URL/path was pasted, take the GUID-looking segment
    if "/" in s:
        parts = [p for p in s.replace("\\", "/").split("/") if p]
        for p in reversed(parts):
            pl = p.lower()
            if pl.startswith("default-"):
                p = p[8:]
            cand = p.replace("-", "")
            if len(cand) >= 32 and all(c in "0123456789abcdef" for c in cand.lower()):
                s = p
                break
    # Keep hex only (drop braces, dashes, spaces)
    hex_only = "".join(c for c in s if c in "0123456789abcdefABCDEF")
    return hex_only.lower()


def _environment_data_plane_host(env_id_hex: str, cloud_suffix: str = "api.powerplatform.com") -> str:
    """PROD host: first 30 hex + '.' + last 2 hex + '.environment.' + suffix."""
    eid = (env_id_hex or "").lower().replace("-", "")
    if len(eid) < 32:
        raise ValueError(f"environment id too short for PP host: {eid!r}")
    # Microsoft PowerPlatformEnvironment.get_environment_endpoint (PROD suffix len=2)
    return f"{eid[:-2]}.{eid[-2:]}.environment.{cloud_suffix}"


def _build_direct_connect_url(env_id_hex: str, schema: str) -> str:
    """Full Direct-to-Engine base URL (no /conversations — SDK appends that).

    Built ourselves so we never depend on SDK host-building quirks / older
    package builds that produced unsplit hosts like:
      {fullguid}.environment.api.powerplatform.com
    """
    host = _environment_data_plane_host(env_id_hex)
    schema = (schema or "").strip()
    if not schema:
        raise ValueError("schema/agent identifier required")
    # Published agents = dataverse-backed path
    return (
        f"https://{host}/copilotstudio/dataverse-backed/authenticated/bots/{schema}"
    )


def _settings():
    from microsoft_agents.copilotstudio.client import ConnectionSettings

    # Explicit override wins (paste full Direct Connect / island URL if needed)
    direct_url = (
        os.getenv("COPILOTSTUDIOAGENT__DIRECTCONNECTURL")
        or os.getenv("COPILOT_DIRECT_CONNECT_URL")
        or ""
    ).strip()

    env_id = _normalize_environment_id(_env("COPILOTSTUDIOAGENT__ENVIRONMENTID"))
    schema = _env("COPILOTSTUDIOAGENT__SCHEMANAME")

    if not direct_url:
        if not env_id or not schema:
            raise RuntimeError(
                "Set COPILOTSTUDIOAGENT__ENVIRONMENTID and "
                "COPILOTSTUDIOAGENT__SCHEMANAME (from Studio publish / embed URL). "
                "Environment id may be dashed GUID or Default-<guid>; we strip dashes."
            )
        if len(env_id) < 32:
            raise RuntimeError(
                f"COPILOTSTUDIOAGENT__ENVIRONMENTID looks too short after normalize "
                f"({env_id!r}). Paste the Environment ID from Studio → Settings → "
                f"Advanced → Metadata (or the GUID from the embed URL)."
            )
        # Always use DirectConnect with a host we build correctly.
        # Avoids ClientConnectorDNSError from unsplit env id hosts on some SDK paths.
        direct_url = _build_direct_connect_url(env_id, schema)

    try:
        from microsoft_agents.copilotstudio.client import PowerPlatformCloud
        cloud = PowerPlatformCloud.PROD
    except Exception:
        cloud = None

    return ConnectionSettings(
        # Empty env/schema when using direct URL is OK per SDK ctor
        environment_id=env_id or "",
        agent_identifier=schema or "",
        cloud=cloud,
        copilot_agent_type=None,
        custom_power_platform_cloud=None,
        direct_connect_url=direct_url,
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

    try:
        await asyncio.wait_for(_run(), timeout=timeout_s)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        # Helpful hint when host still has dashed GUID (old build / bad env)
        if "DNS" in msg or "Cannot connect to host" in msg or "Name or service" in msg:
            h = health()
            exp = h.get("expected_host") or ""
            raise RuntimeError(
                f"{msg}\n"
                f"Copilot Studio host DNS failed. "
                f"Normalized env id={h.get('environment_id_normalized')!r}. "
                f"Expected host≈{exp!r}. "
                f"Fix COPILOTSTUDIOAGENT__ENVIRONMENTID (GUID from Studio Metadata; "
                f"dashes or Default- prefix are OK — we strip them) "
                f"or set COPILOTSTUDIOAGENT__DIRECTCONNECTURL from the embed URL."
            ) from exc
        raise

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
    raw_env = _env("COPILOTSTUDIOAGENT__ENVIRONMENTID")
    norm_env = _normalize_environment_id(raw_env)
    schema = _env("COPILOTSTUDIOAGENT__SCHEMANAME")
    # Expected data-plane host (helps diagnose DNS errors in /agent/api/status)
    expected_host = ""
    direct_built = ""
    if len(norm_env) >= 32:
        try:
            expected_host = _environment_data_plane_host(norm_env)
            if schema:
                direct_built = _build_direct_connect_url(norm_env, schema)
        except Exception:
            pass
    override = bool(
        os.getenv("COPILOTSTUDIOAGENT__DIRECTCONNECTURL")
        or os.getenv("COPILOT_DIRECT_CONNECT_URL")
    )
    return {
        "sdk_installed": installed,
        "missing_env": missing,
        "environment_id_raw": (raw_env[:20] + "…") if len(raw_env) > 20 else raw_env,
        "environment_id_normalized": norm_env[:16] + "…" if len(norm_env) > 16 else norm_env,
        "expected_host": expected_host,
        "direct_connect_url_built": (direct_built[:120] + "…") if len(direct_built) > 120 else direct_built,
        "schema_name": schema,
        "direct_connect_override": override,
        "ready": installed and not missing and (len(norm_env) >= 32 or override),
    }
