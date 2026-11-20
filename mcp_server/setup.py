"""One-command bootstrap for the Power BI Control Center MCP server.

Zero-git onboarding: after installing the wheel from the private feed
(`pip install pbi-control-center-mcp --extra-index-url <feed>`), running

    pbi-cc-setup

seeds the credentials template at ~\\.ashleycode\\mcp\\pbi-control-center\\.env,
registers the server in the user's global AshleyCode/OpenCode config, and
validates the install. No repo checkout is ever needed.

Runnable as a console script (pbi-cc-setup), as a module
(python -m mcp_server.setup), or imported for the individual helpers.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# ASCII-only output: Windows cp1252 consoles crash on non-ASCII prints.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

SERVER_NAME = "powerbi-control-center"
CONFIG_DIR = Path.home() / ".ashleycode" / "mcp" / "pbi-control-center"
ENV_PATH = CONFIG_DIR / ".env"

TEMPLATE = """\
# Power BI Control Center MCP - credentials (NEVER commit this file).
# Fill in the values, save, then start a new AshleyCode session.
TENANT_ID=
CLIENT_ID=
CLIENT_SECRET=

# SharePoint artifact store (catalog source of truth) - copy from a teammate:
SHAREPOINT_TENANT_ID=
SHAREPOINT_CLIENT_ID=
SHAREPOINT_CLIENT_SECRET=
SHAREPOINT_SITE_HOSTNAME=
SHAREPOINT_SITE_PATH=
SHAREPOINT_DRIVE_NAME=
SHAREPOINT_FOLDER_PATH=

# Optional ACL: restrict the MCP to specific workspaces (comma-separated
# GUIDs). Unset = tenant-wide. Impact-analysis tools stay tenant-wide.
MCP_ALLOWED_WORKSPACE_IDS=
"""


def seed_env(path: Path = ENV_PATH, overwrite: bool = False) -> bool:
    """Write the credentials template. Returns True when a file was written.

    Existing files are preserved unless overwrite=True, so hand-edited
    credentials are never clobbered by a re-run.
    """
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return True


def _opencode_command(args: list[str]) -> list[str] | None:
    """Resolve the AshleyCode CLI to an executable command list, or None."""
    resolved = shutil.which("opencode")
    if not resolved:
        return None
    if resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", resolved, *args]
    return [resolved, *args]


def register() -> bool:
    """Register the server in the user's global config.

    Returns True when a new registration was written, False when an entry
    already exists (never duplicates). Raises SystemExit if the CLI call
    fails.
    """
    candidates = (
        Path.home() / ".config" / "opencode" / "opencode.jsonc",
        Path.home() / ".config" / "opencode" / "opencode.json",
    )
    for candidate in candidates:
        if candidate.exists() and SERVER_NAME in candidate.read_text(encoding="utf-8"):
            print(f"Already registered in {candidate}")
            return False

    argv = _opencode_command(
        ["mcp", "add", SERVER_NAME, "--global", "--", "pbi-cc-mcp"]
    )
    if argv is None:
        print("WARNING: 'opencode' CLI not found on PATH - register manually:")
        print(f"  opencode mcp add {SERVER_NAME} --global -- pbi-cc-mcp")
        return False

    result = subprocess.run(argv)
    if result.returncode != 0:
        raise SystemExit(f"opencode mcp add failed (exit {result.returncode}).")
    return True


def main() -> None:
    print(f"Power BI Control Center MCP setup ({SERVER_NAME})")

    # 1. Credentials template at the location the server reads first.
    created = seed_env()
    if created:
        print(f"Seeded credentials template: {ENV_PATH}")
    else:
        print(f"Credentials file already present: {ENV_PATH}")

    # 2. Validate the install (also loads .env so catalog config is ready).
    import mcp_server.server  # noqa: F401

    print("Server import OK - wheel installation is healthy.")

    # 3. Register globally so the MCP appears in every session.
    register()

    print("")
    print("Done. Next steps:")
    if created:
        print(f"  1. Edit {ENV_PATH} and fill in the credentials")
    print("  2. Start a new AshleyCode session (or restart it)")
    print("  3. Check /mcps -> powerbi-control-center should be connected")
    print("  4. First tool to try: catalog_status")


if __name__ == "__main__":
    main()
