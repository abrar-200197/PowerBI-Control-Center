"""
agent/db.py — snapshot backend.

Uses DuckDB when installed (production), falls back to sqlite3 otherwise so the
demo and the test suite run anywhere. The governance SQL is deliberately
ANSI-plain so both engines execute it unchanged.

Connections are opened READ-ONLY where the engine supports it: a prompt-injected
"DROP TABLE" cannot execute even if it somehow reached the driver.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - depends on environment
    import duckdb  # type: ignore
    HAVE_DUCKDB = True
except Exception:  # pragma: no cover
    duckdb = None  # type: ignore
    HAVE_DUCKDB = False


def engine_name(path: str | os.PathLike) -> str:
    return "duckdb" if (HAVE_DUCKDB and str(path).endswith(".duckdb")) else "sqlite"


class Snapshot:
    """Thread-safe read-only handle to the catalog snapshot.

    gunicorn runs gthreads, so a single connection is shared across threads;
    sqlite needs check_same_thread=False plus our own lock. DuckDB connections
    are thread-safe for reads but we serialize anyway for uniform behaviour.
    """

    def __init__(self, path: str | os.PathLike):
        self.path = str(path)
        self.engine = engine_name(self.path)
        self._lock = threading.Lock()
        if not Path(self.path).is_file():
            raise FileNotFoundError(
                f"snapshot not found: {self.path}\n"
                f"Run:  python demo/seed_demo_db.py   (creates a demo snapshot)"
            )
        if self.engine == "duckdb":
            self._con = duckdb.connect(self.path, read_only=True)  # type: ignore
        else:
            self._con = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
            )

    def execute(self, sql: str, params: Any = ()):
        with self._lock:
            if self.engine == "duckdb":
                return self._con.execute(sql, list(params or []))
            return self._con.execute(sql, tuple(params or ()))

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:
            pass


_snapshot: Optional[Snapshot] = None
_snap_lock = threading.Lock()


def get_snapshot(path: Optional[str] = None) -> Snapshot:
    """Process-wide singleton. Call reset_snapshot() after a new build lands."""
    global _snapshot
    with _snap_lock:
        if _snapshot is None or (path and path != _snapshot.path):
            if _snapshot is not None:
                _snapshot.close()
            _snapshot = Snapshot(path or default_snapshot_path())
        return _snapshot


def reset_snapshot() -> None:
    global _snapshot
    with _snap_lock:
        if _snapshot is not None:
            _snapshot.close()
        _snapshot = None


def default_snapshot_path() -> str:
    explicit = os.getenv("CATALOG_SNAPSHOT_PATH")
    if explicit:
        return explicit
    root = Path(__file__).resolve().parent.parent
    for cand in (root / "data" / "catalog.duckdb", root / "data" / "catalog.sqlite"):
        if cand.is_file():
            return str(cand)
    # default target for a fresh build
    return str(root / "data" / ("catalog.duckdb" if HAVE_DUCKDB else "catalog.sqlite"))
