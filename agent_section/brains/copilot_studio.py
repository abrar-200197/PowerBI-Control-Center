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
    """Full Direct-to-Engine base URL (no /conversations — SDK appends that)."""
    host = _environment_data_plane_host(env_id_hex)
    schema = (schema or "").strip()
    if not schema:
        raise ValueError("schema/agent identifier required")
    return (
        f"https://{host}/copilotstudio/dataverse-backed/authenticated/bots/{schema}"
    )


def _parse_studio_channels_url(raw: str) -> dict:
    """Parse a Copilot Studio Channels → Web app connection URL.

    Example (Microsoft docs):
      https://{id}.environment.api.powerplatform.com/copilotstudio/dataverse-backed/
        authenticated/bots/{agentName}/conversations?api-version=...

    ``{id}`` is often already ``{hex30}.{hex2}`` (split form). Prefer pasting this
    full URL into COPILOTSTUDIOAGENT__DIRECTCONNECTURL when Metadata GUID DNS fails.
    """
    from urllib.parse import urlparse

    s = (raw or "").strip()
    if not s or "://" not in s:
        return {}
    p = urlparse(s)
    host = (p.hostname or "").lower()
    if not host:
        return {}
    path = p.path or ""
    schema = ""
    # .../bots/{schemaName}/...
    parts = [x for x in path.split("/") if x]
    for i, seg in enumerate(parts):
        if seg.lower() == "bots" and i + 1 < len(parts):
            schema = parts[i + 1]
            break
    # Base URL through .../bots/{schema} (SDK adds /conversations)
    base_path = path
    if "/conversations" in base_path:
        base_path = base_path[: base_path.index("/conversations")]
    while base_path.endswith("/"):
        base_path = base_path[:-1]
    base = f"{p.scheme}://{host}{base_path}"
    return {
        "host": host,
        "schema": schema,
        "direct_connect_url": base,
        "is_pp_environment_host": "environment.api.powerplatform." in host,
    }


def _reject_bad_pp_host(direct_url: str) -> None:
    """Fail fast on Dynamics org URLs / bad hosts that always NXDOMAIN for Studio."""
    from urllib.parse import urlparse

    host = (urlparse(direct_url).hostname or "").lower()
    if not host:
        return
    # Classic mistake: paste Dataverse / Dynamics org URL (*.crm.dynamics.com)
    # or a mangled *.crm.environment.api.powerplatform.com host.
    if ".crm." in host or host.endswith(".dynamics.com"):
        raise RuntimeError(
            f"COPILOTSTUDIOAGENT__DIRECTCONNECTURL host looks like Dataverse/Dynamics "
            f"({host!r}), not the Copilot Studio Direct-to-Engine host.\n"
            f"Do NOT use an org URL like https://org.crm.dynamics.com.\n"
            f"Open Copilot Studio → your agent → Channels → Mobile app / "
            f"Custom website / Web app (Direct Line or Direct Connect) and copy the "
            f"connection URL that contains:\n"
            f"  https://{{hex30}}.{{hex2}}.environment.api.powerplatform.com/"
            f"copilotstudio/dataverse-backed/authenticated/bots/{{SchemaName}}\n"
            f"Set that full URL as App Setting COPILOTSTUDIOAGENT__DIRECTCONNECTURL "
            f"and also set COPILOTSTUDIOAGENT__SCHEMANAME to the bot schema name."
        )
    # Unsplit 32-hex GUID before .environment. always NXDOMAIN for PROD (need split).
    labels = host.split(".")
    if (
        len(labels) >= 4
        and labels[1] == "environment"
        and "powerplatform" in host
        and labels[0].replace("-", "").isalnum()
        and len(labels[0].replace("-", "")) >= 32
        and "." not in labels[0]
    ):
        raise RuntimeError(
            f"Direct Connect host {host!r} uses an unsplit environment id "
            f"(missing the . before the last 2 hex digits). "
            f"PROD hosts must look like "
            f"{{30hex}}.{{2hex}}.environment.api.powerplatform.com. "
            f"Paste the Channels URL from Copilot Studio instead of building it by hand."
        )


def _settings():
    from microsoft_agents.copilotstudio.client import ConnectionSettings

    # Prefer full Channels / Direct Connect URL (authoritative host from Microsoft).
    direct_raw = (
        os.getenv("COPILOTSTUDIOAGENT__DIRECTCONNECTURL")
        or os.getenv("COPILOT_DIRECT_CONNECT_URL")
        or ""
    ).strip()
    parsed = _parse_studio_channels_url(direct_raw) if direct_raw else {}

    env_id = _normalize_environment_id(_env("COPILOTSTUDIOAGENT__ENVIRONMENTID"))
    schema = _env("COPILOTSTUDIOAGENT__SCHEMANAME") or (parsed.get("schema") or "")

    if parsed.get("direct_connect_url"):
        direct_url = parsed["direct_connect_url"]
        if parsed.get("schema") and not schema:
            schema = parsed["schema"]
    elif direct_raw and direct_raw.startswith("http"):
        # Non-standard URL — pass through; strip trailing /conversations if present
        direct_url = direct_raw.split("?")[0]
        if "/conversations" in direct_url:
            direct_url = direct_url[: direct_url.index("/conversations")]
        direct_url = direct_url.rstrip("/")
    else:
        direct_url = ""

    if not direct_url:
        if not env_id or not schema:
            raise RuntimeError(
                "Copilot Studio is not configured. Either:\n"
                "  A) Set COPILOTSTUDIOAGENT__DIRECTCONNECTURL to the URL from "
                "Copilot Studio → Channels → Web app (recommended), OR\n"
                "  B) Set COPILOTSTUDIOAGENT__ENVIRONMENTID + "
                "COPILOTSTUDIOAGENT__SCHEMANAME from Settings → Advanced → Metadata.\n"
                "If DNS fails on the built host, use option A (the Channels URL has "
                "the correct Power Platform hostname for your agent)."
            )
        if len(env_id) < 32:
            raise RuntimeError(
                f"COPILOTSTUDIOAGENT__ENVIRONMENTID looks too short after normalize "
                f"({env_id!r}). Prefer pasting the Channels → Web app URL into "
                f"COPILOTSTUDIOAGENT__DIRECTCONNECTURL."
            )
        direct_url = _build_direct_connect_url(env_id, schema)

    _reject_bad_pp_host(direct_url)

    try:
        from microsoft_agents.copilotstudio.client import PowerPlatformCloud
        cloud = PowerPlatformCloud.PROD
    except Exception:
        cloud = None

    # When Direct Connect URL is set, clear environment_id so the SDK never
    # rebuilds a host from Metadata GUID (can disagree with Channels host).
    return ConnectionSettings(
        environment_id="" if direct_raw else (env_id or ""),
        agent_identifier=schema or "unused-when-direct-url",
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
                f"Power Platform hostname does not resolve (NXDOMAIN). "
                f"Built host={exp!r} from env id={h.get('environment_id_normalized')!r}. "
                f"That means the Environment ID used to build the host is wrong for "
                f"this agent (Metadata GUID ≠ Channels data-plane host), OR the host "
                f"is blocked. Fix: open Copilot Studio → Channels → Web app, copy the "
                f"connection URL (contains xxx.yy.environment.api.powerplatform.com/"
                f"copilotstudio/.../bots/YourSchema), and set App Setting "
                f"COPILOTSTUDIOAGENT__DIRECTCONNECTURL to that full URL. "
                f"Also set COPILOTSTUDIOAGENT__SCHEMANAME if not already set."
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


def _dns_probe(host: str) -> Dict[str, Any]:
    """Non-secret DNS check for the Power Platform data-plane host."""
    host = (host or "").strip().lower()
    if not host:
        return {"host": "", "ok": False, "error": "empty_host"}
    try:
        import socket
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addrs = sorted({i[4][0] for i in infos if i and i[4]})
        return {
            "host": host,
            "ok": bool(addrs),
            "addresses": addrs[:5],
            "error": None if addrs else "no_addresses",
        }
    except Exception as exc:
        return {
            "host": host,
            "ok": False,
            "addresses": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _connection_debug() -> Dict[str, Any]:
    """
    Exact URL/host the CopilotClient will use (no secrets / no tokens).

    Exposed via GET /agent/api/status for IT Financials troubleshooting.
    """
    from urllib.parse import urlparse

    out: Dict[str, Any] = {
        "settings_ok": False,
        "settings_error": None,
        "direct_connect_url": "",
        "direct_connect_host": "",
        "direct_connect_path": "",
        "source": None,  # override | built_from_metadata
        "dns": None,
    }
    override_raw = (
        os.getenv("COPILOTSTUDIOAGENT__DIRECTCONNECTURL")
        or os.getenv("COPILOT_DIRECT_CONNECT_URL")
        or ""
    ).strip()
    out["override_configured"] = bool(override_raw)
    if override_raw:
        out["source"] = "override"
        # Never return query strings (could hold tokens in some Studio UIs)
        bare = override_raw.split("?", 1)[0].rstrip("/")
        out["override_preview"] = bare[:160] + ("…" if len(bare) > 160 else "")
    else:
        out["source"] = "built_from_metadata"
        out["override_preview"] = ""

    try:
        settings = _settings()
        url = (getattr(settings, "direct_connect_url", None) or "").strip()
        # Strip query if any
        if "?" in url:
            url = url.split("?", 1)[0]
        url = url.rstrip("/")
        parsed = urlparse(url) if url else None
        host = (parsed.hostname or "") if parsed else ""
        path = (parsed.path or "") if parsed else ""
        out["settings_ok"] = True
        out["direct_connect_url"] = url
        out["direct_connect_host"] = host
        out["direct_connect_path"] = path
        out["agent_identifier"] = getattr(settings, "agent_identifier", None) or ""
        out["environment_id_on_settings"] = (
            (getattr(settings, "environment_id", None) or "")[:16] + "…"
            if (getattr(settings, "environment_id", None) or "")
            else ""
        )
        out["dns"] = _dns_probe(host)
    except Exception as exc:
        out["settings_error"] = f"{type(exc).__name__}: {exc}"
    return out


def health() -> Dict[str, Any]:
    """Config check that does NOT need a user token -- safe for status APIs."""
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
    # Expected data-plane host from Metadata GUID alone
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
    conn = _connection_debug()
    return {
        "sdk_installed": installed,
        "missing_env": missing,
        "environment_id_raw": (raw_env[:20] + "…") if len(raw_env) > 20 else raw_env,
        "environment_id_normalized": (
            norm_env[:16] + "…" if len(norm_env) > 16 else norm_env
        ),
        "environment_id_normalized_full": norm_env,  # needed to compare with docs
        "expected_host_from_metadata": expected_host,
        "direct_connect_url_from_metadata": direct_built,
        "schema_name": schema,
        "direct_connect_override": override,
        # What the running client actually uses (after _settings())
        "connection": conn,
        "dns_ok": bool((conn.get("dns") or {}).get("ok")),
        "ready": installed
        and (len(norm_env) >= 32 or override)
        and bool(schema or override),
    }
