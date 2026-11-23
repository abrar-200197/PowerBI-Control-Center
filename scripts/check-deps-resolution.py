"""Container dependency-resolution check — run before pushing pin changes.

The container installs requirements.txt FRESH on python 3.12; the dev
machine's already-installed set can mask resolver conflicts. That is exactly
how the 2026-11-23 /mcp outage shipped: httpx==0.27.0 forced pip to backtrack
to mcp 1.12 (whose func_metadata hard-imports pydantic internals removed in
pydantic 2.11+), so the MCP sidecar crashed at every container start while
every dev machine worked fine.

What this script does (needs network, ~2-5 min):
  1. creates a throwaway venv,
  2. pip-installs requirements.txt exactly as the container does,
  3. imports mcp_server.server (the sidecar entry chain) and registers tools,
  4. prints the resolved versions of the critical packages.

Usage:  python scripts/check-deps-resolution.py
"""
import re
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

REPO = Path(__file__).resolve().parents[1]
WATCHED = ("mcp", "pydantic", "httpx", "uvicorn", "openai")


def _run(cmd, **kw):
    proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise SystemExit(f"FAILED: {' '.join(cmd[:4])}...")
    return proc.stdout


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pbi-deps-") as tmp:
        venv_dir = Path(tmp) / "venv"
        venv.create(venv_dir, with_pip=True)
        py = venv_dir / "Scripts" / "python.exe"
        if not py.exists():
            py = venv_dir / "bin" / "python"

        print("Installing requirements.txt into a clean venv (as the container does)...", flush=True)
        _run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
        _run([str(py), "-m", "pip", "install", "--quiet", "-r", str(REPO / "requirements.txt")])

        freeze = _run([str(py), "-m", "pip", "freeze"])
        resolved = {}
        for line in freeze.splitlines():
            m = re.match(r"^([A-Za-z0-9_.-]+)==(.+)$", line.strip())
            if m and m.group(1).lower().replace("_", "-") in WATCHED:
                resolved[m.group(1).lower()] = m.group(2)
        print("Resolved:", ", ".join(f"{k}=={v}" for k, v in sorted(resolved.items())), flush=True)

        print("Importing mcp_server.server (the sidecar entry chain)...", flush=True)
        out = _run(
            [str(py), "-c", "import mcp_server.server as s; tools = s.mcp._tool_manager.list_tools(); print('TOOLS:', len(tools))"],
            cwd=str(REPO),
        )
        print(out.strip(), flush=True)

        missing = [k for k in WATCHED if k not in resolved]
        assert not missing, f"packages missing from resolution: {missing}"
        print("DEPENDENCY RESOLUTION OK", flush=True)


if __name__ == "__main__":
    main()
