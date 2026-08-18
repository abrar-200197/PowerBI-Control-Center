"""
Azure Functions v2 (Python) — single root entry (no CatalogExtractTimer/ folder).

Schedule (NCRONTAB UTC): 0 30 0,6,12,18 * * *
  Sunday 06:30 UTC → python run_catalog_extract.py --fresh
  All other slots  → python run_catalog_extract.py --ops-only

Package root (flat deploy):
  function_app.py, host.json, requirements.txt, run_catalog_extract.py,
  powerbi_connector.py, catalog_service/   ← Python package only
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

import azure.functions as func

logger = logging.getLogger("CatalogExtractTimer")

app = func.FunctionApp()

_ROOT = Path(__file__).resolve().parent


def _repo_root() -> Path:
    env = (os.getenv("CATALOG_REPO_ROOT") or "").strip()
    if env:
        return Path(env)
    if (_ROOT / "run_catalog_extract.py").is_file():
        return _ROOT
    parent = _ROOT.parent
    if (parent / "run_catalog_extract.py").is_file():
        return parent
    return _ROOT


def decide_mode(now: datetime | None = None) -> Tuple[str, List[str]]:
    """Sunday 06:30 UTC → fresh; otherwise ops-only."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    is_sunday = now.weekday() == 6
    is_six_thirty_window = now.hour == 6 and 25 <= now.minute <= 40
    verbose = os.getenv("CATALOG_EXTRACT_VERBOSE", "true").lower() in (
        "1", "true", "yes", "y",
    )
    extra: List[str] = ["-v"] if verbose else []

    if is_sunday and is_six_thirty_window:
        return "fresh", ["--fresh", *extra]
    return "ops-only", ["--ops-only", *extra]


def run_extract(argv: List[str]) -> int:
    root = _repo_root()
    script = root / "run_catalog_extract.py"
    if not script.is_file():
        raise FileNotFoundError(
            f"run_catalog_extract.py not found at {script}. "
            "Set CATALOG_REPO_ROOT or deploy extract scripts next to function_app.py."
        )
    # Ensure cwd is on PYTHONPATH so `import config` / catalog_service work
    env = os.environ.copy()
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(root) + (os.pathsep + prev if prev else "")

    cmd = [sys.executable, "-u", str(script), *argv]
    logger.info("Running: %s (cwd=%s)", " ".join(cmd), root)
    # Capture output — previous code discarded stdout so failures looked empty (exit 1 in ~0.6s)
    proc = subprocess.run(
        cmd,
        cwd=str(root),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        # Azure logs truncate long lines; keep tail of useful extract output
        tail = out if len(out) <= 12000 else ("…\n" + out[-12000:])
        logger.info("extract stdout:\n%s", tail)
    if err:
        tail_e = err if len(err) <= 8000 else ("…\n" + err[-8000:])
        logger.error("extract stderr:\n%s", tail_e)
    if proc.returncode != 0:
        logger.error(
            "extract exit=%s missing_config=%s missing_connector_dep=%s",
            proc.returncode,
            not (root / "config.py").is_file(),
            "No module named" in (out + "\n" + err),
        )
    return int(proc.returncode)


@app.function_name(name="CatalogExtractTimer")
@app.timer_trigger(
    schedule="0 30 0,6,12,18 * * *",
    arg_name="mytimer",
    run_on_startup=False,
    use_monitor=True,
)
def catalog_extract_timer(mytimer: func.TimerRequest) -> None:
    utc_now = datetime.now(timezone.utc)
    if mytimer.past_due:
        logger.warning("Timer is past due at %s", utc_now.isoformat())

    mode, argv = decide_mode(utc_now)
    logger.info(
        "Catalog extract smart timer at %s UTC → mode=%s argv=%s",
        utc_now.strftime("%Y-%m-%d %H:%M:%S"),
        mode,
        argv,
    )

    try:
        code = run_extract(argv)
    except Exception:
        logger.exception("Catalog extract failed to start")
        raise

    # If ops-only cannot load workspace_catalog (empty/size-0/missing on SP),
    # auto-escalate to a full Scanner rebuild once. Opt out with
    # CATALOG_AUTO_FRESH_ON_OPS_FAIL=false
    auto_fresh = os.getenv("CATALOG_AUTO_FRESH_ON_OPS_FAIL", "true").lower() in (
        "1", "true", "yes", "y",
    )
    if code != 0 and mode == "ops-only" and auto_fresh:
        logger.warning(
            "ops-only failed (exit=%s) — auto-escalating to --fresh once "
            "(set CATALOG_AUTO_FRESH_ON_OPS_FAIL=false to disable)",
            code,
        )
        verbose = os.getenv("CATALOG_EXTRACT_VERBOSE", "true").lower() in (
            "1", "true", "yes", "y",
        )
        fresh_argv = ["--fresh"] + (["-v"] if verbose else [])
        try:
            code = run_extract(fresh_argv)
            mode = "fresh-after-ops-fail"
        except Exception:
            logger.exception("Auto-fresh extract failed to start")
            raise

    if code != 0:
        raise RuntimeError(
            f"run_catalog_extract exited with code {code} (mode={mode})"
        )

    logger.info("Catalog extract finished OK (mode=%s)", mode)
