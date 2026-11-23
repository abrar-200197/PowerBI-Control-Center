"""Regression guard: app.py must define every name its code references.

Incident (2026-11-23): the blueprint-extraction commit (28bdeb9) restored 14
route bodies that call helper functions but did NOT restore the helpers
(``_catalog_reports_shell`` and friends). The /api/reports catalog fast-path
raised NameError at runtime and silently fell back to the slow live Scanner
path in production. pyflakes catches this class of bug statically.

Run standalone:  python tests/test_apppy_undefined_names.py
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# UTF-8 stdout/stderr (repo pattern for cp1252 consoles).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

REPO = Path(__file__).resolve().parents[1]


def _undefined_names(target: str) -> list:
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", target],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=120,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return [
        line for line in out.splitlines()
        if "undefined name" in line or "undefined local" in line
    ]


def test_app_py_has_no_undefined_names():
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        print("pyflakes not installed (pip install -r requirements-dev.txt) - SKIP")
        return
    hits = _undefined_names("app.py")
    assert not hits, f"undefined names in app.py: {hits}"


def test_routes_have_no_undefined_names():
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        print("pyflakes not installed (pip install -r requirements-dev.txt) - SKIP")
        return
    hits = []
    for route_file in sorted((REPO / "routes").glob("*.py")):
        hits.extend(_undefined_names(str(route_file.relative_to(REPO))))
    assert not hits, f"undefined names in routes/: {hits}"


if __name__ == "__main__":
    test_app_py_has_no_undefined_names()
    test_routes_have_no_undefined_names()
    print("All tests passed.")
