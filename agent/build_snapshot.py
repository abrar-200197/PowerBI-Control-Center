"""
Build data/catalog.sqlite from workspace_catalog.json (+ optional usage).

The Agent model picker and governance tools read this snapshot. Without it,
/agent/api/models fails and the UI sticks on "Loading...".
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _candidate_catalog_paths() -> List[Path]:
    env = (os.getenv("WORKSPACE_CATALOG_PATH") or "").strip()
    out: List[Path] = []
    if env:
        out.append(Path(env))
    try:
        from catalog_service import catalog_config as cfg  # type: ignore
        cache = getattr(cfg, "CATALOG_CACHE_DIR", None)
        if cache:
            out.append(Path(cache) / "workspace_catalog.json")
    except Exception:
        pass
    out.extend([
        ROOT / "data" / "catalog_cache" / "latest" / "workspace_catalog.json",
        Path("/home/data/catalog_cache/latest/workspace_catalog.json"),
        ROOT / "data" / "workspace_catalog.json",
    ])
    return out


def find_workspace_catalog() -> Optional[Path]:
    for p in _candidate_catalog_paths():
        try:
            if p.is_file() and p.stat().st_size > 100:
                return p
        except OSError:
            continue
    return None


def find_usage_snapshot(catalog_path: Path) -> Optional[Path]:
    env = (os.getenv("USAGE_SNAPSHOT_PATH") or "").strip()
    cands = []
    if env:
        cands.append(Path(env))
    cands.append(catalog_path.parent / "usage_snapshot.json")
    cands.append(ROOT / "data" / "catalog_cache" / "latest" / "usage_snapshot.json")
    for p in cands:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def default_snapshot_out() -> Path:
    env = (os.getenv("CATALOG_SNAPSHOT_PATH") or "").strip()
    if env:
        return Path(env)
    return ROOT / "data" / "catalog.sqlite"


DDL = """
CREATE TABLE manifest (
  build_id TEXT, built_at_utc TEXT, mode TEXT,
  schema_version TEXT, row_counts TEXT, status TEXT
);
CREATE TABLE datasets (
  dataset_id TEXT PRIMARY KEY, name TEXT, workspace_id TEXT, workspace_name TEXT,
  configured_by TEXT, storage_mode TEXT, table_count INTEGER, measure_count INTEGER,
  has_schema INTEGER
);
CREATE TABLE tables (
  dataset_id TEXT, table_name TEXT, is_hidden INTEGER,
  source_type_label TEXT, server_name TEXT,
  PRIMARY KEY (dataset_id, table_name)
);
CREATE TABLE columns (
  dataset_id TEXT, table_name TEXT, column_name TEXT,
  data_type TEXT, is_hidden INTEGER
);
CREATE TABLE measures (
  dataset_id TEXT, table_name TEXT, measure_name TEXT,
  expression TEXT, description TEXT, format_string TEXT, is_hidden INTEGER
);
CREATE TABLE datasources (
  dataset_id TEXT, datasource_type TEXT, server TEXT, database TEXT, gateway_id TEXT
);
CREATE TABLE reports (
  report_id TEXT PRIMARY KEY, name TEXT, workspace_id TEXT, workspace_name TEXT,
  dataset_id TEXT
);
CREATE TABLE relationships (
  dataset_id TEXT, from_table TEXT, from_column TEXT,
  to_table TEXT, to_column TEXT, cardinality TEXT,
  is_active INTEGER, cross_filtering TEXT
);
CREATE TABLE refresh (
  dataset_id TEXT, status TEXT, last_refresh_utc TEXT,
  refresh_type TEXT, error_code TEXT, is_failed INTEGER
);
CREATE TABLE usage (
  report_id TEXT PRIMARY KEY, views INTEGER,
  distinct_users INTEGER, last_viewed_utc TEXT
);
CREATE TABLE impact (
  table_key TEXT, source_type TEXT, server TEXT, database TEXT,
  schema_name TEXT, physical_table TEXT, model_table_name TEXT,
  dataset_id TEXT, workspace_id TEXT, report_id TEXT,
  report_count INTEGER, dataset_count INTEGER, workspace_count INTEGER
);
CREATE INDEX ix_meas_ds ON measures(dataset_id);
CREATE INDEX ix_meas_name ON measures(measure_name);
CREATE INDEX ix_tab_ds ON tables(dataset_id);
CREATE INDEX ix_col_ds ON columns(dataset_id, table_name);
CREATE INDEX ix_rep_ds ON reports(dataset_id);
CREATE INDEX ix_ds_name ON datasets(name);
"""


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return []


def _bool_int(v: Any) -> int:
    return 1 if v in (True, 1, "1", "true", "True") else 0


def _s(v: Any, default: str = "") -> str:
    """SQLite-safe scalar string (dicts/lists become JSON or empty)."""
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False)[:2000]
        except Exception:
            return default
    return str(v)


def _load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"expected object in {path}")
    return data


def _iter_workspace_datasets(cat: Dict[str, Any]) -> Iterable[Tuple[dict, dict]]:
    """Yield (workspace, dataset_stub) from workspaces[]."""
    for ws in _as_list(cat.get("workspaces")):
        if not isinstance(ws, dict):
            continue
        for d in _as_list(ws.get("datasets")):
            if isinstance(d, dict):
                yield ws, d


def _rich_datasets(cat: Dict[str, Any]) -> Dict[str, dict]:
    raw = cat.get("datasets")
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    if isinstance(raw, list):
        out = {}
        for d in raw:
            if isinstance(d, dict) and d.get("id"):
                out[str(d["id"])] = d
        return out
    return {}



def build_snapshot(
    catalog_path: Optional[Path] = None,
    out_path: Optional[Path] = None,
    usage_path: Optional[Path] = None,
) -> Path:
    """Materialize catalog.sqlite. Returns path written."""
    cat_path = Path(catalog_path) if catalog_path else find_workspace_catalog()
    if not cat_path or not cat_path.is_file():
        raise FileNotFoundError(
            "workspace_catalog.json not found. Expected under data/catalog_cache/latest/"
        )
    dest = Path(out_path) if out_path else default_snapshot_out()
    dest.parent.mkdir(parents=True, exist_ok=True)

    cat = _load_json(cat_path)
    rich = _rich_datasets(cat)
    usage_file = Path(usage_path) if usage_path else find_usage_snapshot(cat_path)
    usage = _load_json(usage_file) if usage_file else {}
    report_views = usage.get("report_views") if isinstance(usage.get("report_views"), dict) else {}
    last_viewed = usage.get("last_viewed") if isinstance(usage.get("last_viewed"), dict) else {}

    # Atomic write via temp file in same dir
    fd, tmp_name = tempfile.mkstemp(prefix="catalog_", suffix=".sqlite", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        con = sqlite3.connect(str(tmp))
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.executescript(DDL)

        counts = _populate(con, cat, rich, report_views, last_viewed)
        build_id = uuid.uuid4().hex[:12]
        con.execute(
            "INSERT INTO manifest VALUES (?,?,?,?,?,?)",
            (build_id, _utc_now(), "workspace_catalog", "1",
             json.dumps(counts), "complete"),
        )
        con.commit()
        con.close()
        # Replace existing
        if dest.exists():
            dest.unlink()
        tmp.replace(dest)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return dest


def _populate(con, cat, rich, report_views, last_viewed) -> Dict[str, int]:
    ds_rows, tab_rows, col_rows, meas_rows = [], [], [], []
    src_rows, rel_rows, ref_rows = [], [], []
    seen_ds = set()

    # Prefer rich top-level datasets; supplement with workspace stubs
    for did, d in rich.items():
        if did in seen_ds:
            continue
        seen_ds.add(did)
        _add_dataset(d, did, d.get("workspaceId") or "", d.get("workspaceName") or "",
                     ds_rows, tab_rows, col_rows, meas_rows, src_rows, rel_rows, ref_rows)

    for ws, stub in _iter_workspace_datasets(cat):
        did = str(stub.get("id") or "")
        if not did or did in seen_ds:
            continue
        seen_ds.add(did)
        # thin stub only
        _add_dataset(stub, did, ws.get("id") or "", ws.get("name") or "",
                     ds_rows, tab_rows, col_rows, meas_rows, src_rows, rel_rows, ref_rows)

    rep_rows, usage_rows = [], []
    seen_rep = set()
    for ws in _as_list(cat.get("workspaces")):
        if not isinstance(ws, dict):
            continue
        wid, wname = ws.get("id") or "", ws.get("name") or ""
        for r in _as_list(ws.get("reports")):
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "")
            if not rid or rid in seen_rep:
                continue
            seen_rep.add(rid)
            rep_rows.append((
                rid, _s(r.get("name")), _s(wid), _s(wname),
                _s(r.get("datasetId") or r.get("dataset_id") or "") or None,
            ))
            views = report_views.get(rid)
            if views is None:
                views = r.get("view_count")
            lv = last_viewed.get(rid)
            lv_ts = None
            if isinstance(lv, dict):
                lv_ts = lv.get("timestamp")
            elif isinstance(lv, str):
                lv_ts = lv
            if lv_ts is None:
                raw_lv = r.get("last_viewed")
                if isinstance(raw_lv, dict):
                    lv_ts = raw_lv.get("timestamp") or raw_lv.get("date")
                elif raw_lv is not None:
                    lv_ts = str(raw_lv)
            if views is not None or lv_ts:
                try:
                    v_int = int(views) if views is not None and not isinstance(views, (dict, list)) else 0
                except (TypeError, ValueError):
                    v_int = 0
                usage_rows.append((rid, v_int, None, _s(lv_ts) if lv_ts else None))

    con.executemany(
        "INSERT OR REPLACE INTO datasets VALUES (?,?,?,?,?,?,?,?,?)", ds_rows)
    con.executemany(
        "INSERT OR REPLACE INTO tables VALUES (?,?,?,?,?)", tab_rows)
    con.executemany(
        "INSERT INTO columns VALUES (?,?,?,?,?)", col_rows)
    con.executemany(
        "INSERT INTO measures VALUES (?,?,?,?,?,?,?)", meas_rows)
    con.executemany(
        "INSERT INTO datasources VALUES (?,?,?,?,?)", src_rows)
    con.executemany(
        "INSERT INTO relationships VALUES (?,?,?,?,?,?,?,?)", rel_rows)
    con.executemany(
        "INSERT OR REPLACE INTO reports VALUES (?,?,?,?,?)", rep_rows)
    con.executemany(
        "INSERT OR REPLACE INTO refresh VALUES (?,?,?,?,?,?)", ref_rows)
    con.executemany(
        "INSERT OR REPLACE INTO usage VALUES (?,?,?,?)", usage_rows)

    return {
        "datasets": len(ds_rows), "tables": len(tab_rows),
        "columns": len(col_rows), "measures": len(meas_rows),
        "reports": len(rep_rows), "usage": len(usage_rows),
        "datasources": len(src_rows), "relationships": len(rel_rows),
    }


def _add_dataset(d, did, wid, wname, ds_rows, tab_rows, col_rows, meas_rows,
                 src_rows, rel_rows, ref_rows):
    tables = _as_list(d.get("tables"))
    # measures may be nested under tables or top-level
    top_measures = _as_list(d.get("measures"))
    mcount = d.get("measureCount")
    if mcount is None:
        mcount = len(top_measures)
        for t in tables:
            mcount += len(_as_list(t.get("measures")))
    tcount = d.get("tableCount")
    if tcount is None:
        tcount = len(tables)
    has_schema = 1 if tables else 0
    ds_rows.append((
        did, _s(d.get("name")), _s(wid or d.get("workspaceId")),
        _s(wname or d.get("workspaceName")),
        _s(d.get("configuredBy") or d.get("dataset_owner")),
        _s(d.get("targetStorageMode") or d.get("storage_mode")),
        int(tcount or 0), int(mcount or 0), has_schema,
    ))

    for t in tables:
        if not isinstance(t, dict):
            continue
        tname = _s(t.get("name"))
        server = t.get("serverName") or ""
        if isinstance(server, (dict, list)):
            server = _s(server)
        srcs = _as_list(t.get("sources"))
        if not server and srcs and isinstance(srcs[0], dict):
            server = srcs[0].get("server") or ""
        tab_rows.append((
            did, tname, _bool_int(t.get("isHidden")),
            _s(t.get("sourceTypeLabel")), _s(server) or None,
        ))
        for c in _as_list(t.get("columns")):
            if not isinstance(c, dict):
                continue
            col_rows.append((
                did, tname, _s(c.get("name")),
                _s(c.get("dataType")), _bool_int(c.get("isHidden")),
            ))
        for m in _as_list(t.get("measures")):
            if not isinstance(m, dict):
                continue
            meas_rows.append((
                did, tname, _s(m.get("name")),
                _s(m.get("expression")), _s(m.get("description")),
                _s(m.get("formatString")), _bool_int(m.get("isHidden")),
            ))
        for s in srcs:
            if not isinstance(s, dict):
                continue
            src_rows.append((
                did, _s(s.get("source_type") or s.get("datasourceType")),
                _s(s.get("server")), _s(s.get("database")),
                _s(s.get("gatewayId") or s.get("gateway_id")),
            ))

    for m in top_measures:
        if not isinstance(m, dict):
            continue
        meas_rows.append((
            did, _s(m.get("table") or m.get("table_name")),
            _s(m.get("name") or m.get("measure_name")),
            _s(m.get("expression")), _s(m.get("description")),
            _s(m.get("formatString") or m.get("format_string")),
            _bool_int(m.get("isHidden")),
        ))

    for rel in _as_list(d.get("relationships")):
        if not isinstance(rel, dict):
            continue
        rel_rows.append((
            did,
            _s(rel.get("fromTable") or rel.get("from_table")),
            _s(rel.get("fromColumn") or rel.get("from_column")),
            _s(rel.get("toTable") or rel.get("to_table")),
            _s(rel.get("toColumn") or rel.get("to_column")),
            _s(rel.get("cardinality")),
            _bool_int(rel.get("isActive", rel.get("is_active", True))),
            _s(rel.get("crossFilteringBehavior") or rel.get("cross_filtering")),
        ))

    for ds in _as_list(d.get("datasources")):
        if not isinstance(ds, dict):
            continue
        conn = ds.get("connectionDetails") if isinstance(ds.get("connectionDetails"), dict) else {}
        src_rows.append((
            did,
            _s(ds.get("datasourceType") or ds.get("type")),
            _s(conn.get("server") or ds.get("server")),
            _s(conn.get("database") or ds.get("database")),
            _s(ds.get("gatewayId")),
        ))

    status = d.get("last_refresh_status") or ""
    last_r = d.get("last_refreshed") or ""
    if isinstance(last_r, dict):
        last_r = last_r.get("endTime") or last_r.get("startTime") or last_r.get("timestamp") or ""
    rtype = d.get("refresh_type") or d.get("history_refresh_type") or ""
    if status or last_r:
        failed = 1 if str(status).lower() in ("failed", "disabled", "cancelled") else 0
        ref_rows.append((did, _s(status), _s(last_r), _s(rtype), "", failed))


def ensure_snapshot(out_path: Optional[Path] = None, *, force: bool = False) -> Path:
    """Return a usable snapshot path, building it if missing or force=True."""
    dest = Path(out_path) if out_path else default_snapshot_out()
    if dest.is_file() and dest.stat().st_size > 1000 and not force:
        return dest
    return build_snapshot(out_path=dest)


if __name__ == "__main__":
    t0 = time.time()
    path = build_snapshot()
    print(f"wrote {path} in {time.time()-t0:.1f}s ({path.stat().st_size} bytes)")
