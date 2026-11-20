"""Unit tests for the zero-git bootstrap helpers (offline).

Run standalone:  python tests/test_mcp_setup.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# UTF-8 stdout/stderr (repo pattern for cp1252 consoles).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import mcp_server.setup as setup_mod


def test_seed_env_creates_template_with_required_keys():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / ".env"
        created = setup_mod.seed_env(target)
        assert created is True
        text = target.read_text(encoding="utf-8")
        for key in ("TENANT_ID=", "CLIENT_SECRET=", "SHAREPOINT_SITE_HOSTNAME=",
                    "MCP_ALLOWED_WORKSPACE_IDS="):
            assert key in text, f"missing {key} in template"


def test_seed_env_never_overwrites_hand_edited_file():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / ".env"
        setup_mod.seed_env(target)
        target.write_text("TENANT_ID=keep-me", encoding="utf-8")
        created = setup_mod.seed_env(target)
        assert created is False
        assert "keep-me" in target.read_text(encoding="utf-8")


def test_seed_env_overwrite_flag_replaces():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / ".env"
        setup_mod.seed_env(target)
        target.write_text("TENANT_ID=keep-me", encoding="utf-8")
        setup_mod.seed_env(target, overwrite=True)
        assert "keep-me" not in target.read_text(encoding="utf-8")


if __name__ == "__main__":
    test_seed_env_creates_template_with_required_keys()
    test_seed_env_never_overwrites_hand_edited_file()
    test_seed_env_overwrite_flag_replaces()
    print("All tests passed.")
