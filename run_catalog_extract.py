"""
Control Center catalog extract — SharePoint-only, zero local persistence.

Flow:
  1) Create a disposable temp directory
  2) Run Scanner / ops into that temp dir
  3) Clean SharePoint .../latest/ then upload JSON
  4) Delete the temp directory (always)

Commands:
  Fresh full rebuild:
    python run_catalog_extract.py --fresh -v

  Every 6 hours:
    python run_catalog_extract.py --ops-only

  Wipe SharePoint latest only:
    python run_catalog_extract.py --clean-sharepoint-only
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv(override=True)

from catalog_service import catalog_config as cfg

logger = logging.getLogger("catalog_extract")

# Active temp root for this process (deleted on exit / finally)
_TEMP_ROOT: Optional[Path] = None


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def _cleanup_temp(path: Optional[Path] = None) -> None:
    global _TEMP_ROOT
    target = path or _TEMP_ROOT
    if not target:
        return
    try:
        if Path(target).exists():
            shutil.rmtree(target, ignore_errors=True)
            print(f"Deleted temp extract folder: {target}")
    except Exception as exc:
        print(f"! temp cleanup failed for {target}: {exc}")
    if _TEMP_ROOT and path is None:
        _TEMP_ROOT = None
    elif path is not None and _TEMP_ROOT == path:
        _TEMP_ROOT = None


def make_temp_root() -> Path:
    """Create disposable temp workspace for this extract run."""
    global _TEMP_ROOT
    root = Path(tempfile.mkdtemp(prefix="pbi_cc_extract_"))
    (root / "latest").mkdir(parents=True, exist_ok=True)
    _TEMP_ROOT = root
    atexit.register(_cleanup_temp)
    print(f"Temp extract workspace: {root}")
    return root


def publish_names(include_inventory: bool = False) -> List[str]:
    names = list(cfg.SHAREPOINT_PUBLISH_FILES)
    if include_inventory and "inventory.json" not in names:
        names.append("inventory.json")
    for extra in ("refresh_snapshot.json", "usage_snapshot.json", "ops_summary.json",
                  "workspace_catalog.json", "impact_index.json", "summary.json", "sources.json"):
        if extra not in names:
            names.append(extra)
    # de-dupe preserve order
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def sharepoint_latest_remote() -> str:
    return f"{(cfg.SHAREPOINT_FOLDER_PATH or '').strip('/')}/latest"


def clean_sharepoint_latest(names: Optional[List[str]] = None) -> List[str]:
    cfg.validate_sharepoint_config()
    from catalog_service.metadata_lib.sharepoint_client import SharePointClient

    remote = sharepoint_latest_remote()
    sp = SharePointClient()
    sp.resolve_site_and_drive()
    print(f"Cleaning SharePoint: {remote}")
    to_clear = names or publish_names(include_inventory=True)
    deleted = sp.clear_folder(remote, names=to_clear)
    # wipe any other stale JSON left behind
    for d in sp.clear_folder(remote, extensions=[".json"]):
        if d not in deleted:
            deleted.append(d)
    for d in deleted:
        print(f"  deleted {d}")
    print(f"  cleaned {len(deleted)} file(s)")
    return deleted


def load_catalog_from_sharepoint() -> dict:
    """
    Force-load workspace_catalog.json from SharePoint.

    Uses SharePointClient.download_file directly (handles Graph size=0 via
    streaming GET). Falls back to CatalogService disk/SP path if needed.
    """
    if not cfg.sharepoint_configured():
        raise RuntimeError("SharePoint is not configured (SHAREPOINT_* env vars).")

    cfg.validate_sharepoint_config()
    from catalog_service.metadata_lib.sharepoint_client import SharePointClient

    remote = f"{sharepoint_latest_remote()}/workspace_catalog.json"
    sp = SharePointClient()
    sp.resolve_site_and_drive()
    print(f"Downloading catalog from SharePoint: {remote}")
    try:
        raw = sp.download_file(remote, max_attempts=3, timeout=1800)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "SharePoint latest/workspace_catalog.json missing (404).\n"
            "Run a fresh extract first:\n"
            "  python run_catalog_extract.py --fresh -v\n"
            f"Detail: {exc}"
        ) from exc
    except Exception as exc:
        # Last try via CatalogService (disk mirror / alternate path)
        print(f"  direct download failed ({exc}); trying CatalogService…")
        try:
            from catalog_service import catalog_service
            catalog_service.invalidate()
            cat = catalog_service.get_workspace_catalog(force_refresh=True)
            if cat:
                print("Loaded workspace_catalog.json via CatalogService fallback")
                return cat
        except Exception as exc2:
            print(f"  CatalogService fallback also failed: {exc2}")
        raise FileNotFoundError(
            "SharePoint latest/workspace_catalog.json could not be downloaded.\n"
            "Graph may report size=0 for a corrupt/empty upload, or the file is missing.\n"
            "Run a fresh extract:\n"
            "  python run_catalog_extract.py --fresh -v\n"
            f"Detail: {exc}"
        ) from exc

    if not raw or len(raw) < 50:
        raise FileNotFoundError(
            f"SharePoint workspace_catalog.json is empty/tiny ({len(raw or b'')} bytes). "
            "File is corrupt — run: python run_catalog_extract.py --fresh -v"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8-sig")
    cat = json.loads(text)
    if not isinstance(cat, dict) or not (cat.get("workspaces") or cat.get("datasets")):
        raise FileNotFoundError(
            "SharePoint workspace_catalog.json parsed but has no workspaces/datasets. "
            "Run: python run_catalog_extract.py --fresh -v"
        )
    print(
        f"Loaded workspace_catalog.json from SharePoint "
        f"({len(raw) / (1024 * 1024):.1f} MB, "
        f"workspaces={len(cat.get('workspaces') or [])})"
    )
    return cat


def publish_temp_latest(
    latest_dir: Path,
    *,
    include_inventory: bool = False,
    clean_first: bool = True,
) -> None:
    """
    Upload temp latest/ JSON to SharePoint latest/.

    clean_first=True  → wipe named/JSON then upload (use on --fresh).
    clean_first=False → overlay only files produced this run (safer for --ops-only
    so a partial ops run cannot wipe workspace_catalog / impact packs).
    """
    cfg.validate_sharepoint_config()
    from catalog_service.metadata_lib.sharepoint_client import SharePointClient

    names = publish_names(include_inventory=include_inventory)
    # Also publish thin UI packs if present
    for thin in (
        "ui_home_index.json",
        "ui_impact_tables.json",
        "ui_impact_reports.json",
        "ui_report_directory.json",
    ):
        if thin not in names:
            names.append(thin)
    existing = [n for n in names if (latest_dir / n).is_file()]
    missing = [n for n in names if n not in existing]
    if missing:
        print(f"NOTE: not produced this run (skip): {', '.join(missing)}")
    if not existing:
        raise FileNotFoundError(f"No JSON to publish under {latest_dir}")

    remote = sharepoint_latest_remote()
    sp = SharePointClient()
    print(
        f"Publishing temp -> SharePoint {remote} "
        f"(clean_first={clean_first}, files={len(existing)})"
    )
    results = sp.replace_directory(
        latest_dir,
        remote_folder=remote,
        names=existing,
        clean_first=clean_first,
    )
    for r in results:
        print(" ", r.get("webUrl") or r.get("remote") or r)

    # small audit trail (summary only under runs/)
    try:
        summary = latest_dir / "summary.json"
        if summary.is_file():
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            audit = f"{(cfg.SHAREPOINT_FOLDER_PATH or '').strip('/')}/runs/{run_id}/summary.json"
            sp.upload_file(summary, audit)
            print(f"  audit -> {audit}")
    except Exception as exc:
        print(f"  ! audit skipped: {exc}")

    # drop in-process cache so a co-hosted app picks up new files
    try:
        from catalog_service import catalog_service
        catalog_service.invalidate()
    except Exception:
        pass


def run_full_extract(temp_root: Path, args) -> Path:
    """Scanner + ops into temp_root; returns temp_root/latest."""
    from catalog_service.metadata_lib.pipeline import run_extraction
    from catalog_service.ops_snapshot import run_ops_enrichment

    latest = temp_root / "latest"
    workspace_ids = None
    if args.workspaces:
        workspace_ids = [x.strip() for x in args.workspaces.split(",") if x.strip()]

    # Never let pipeline upload or keep permanent local catalog dirs
    prev_upload = cfg.SHAREPOINT_UPLOAD_ENABLED
    cfg.SHAREPOINT_UPLOAD_ENABLED = False
    try:
        paths = run_extraction(
            workspace_ids=workspace_ids,
            exclude_personal=not args.include_personal,
            save_raw=not args.no_raw,
            output_dir=temp_root,  # all under disposable temp
        )
    finally:
        cfg.SHAREPOINT_UPLOAD_ENABLED = prev_upload

    print("Scanner artifacts:")
    for k, v in paths.items():
        print(f"  {k}: {v}")

    cat_path = latest / "workspace_catalog.json"
    if cat_path.is_file():
        catalog = json.loads(cat_path.read_text(encoding="utf-8"))
        op_paths = run_ops_enrichment(
            catalog,
            out_dir=latest,
            skip_refresh=args.skip_refresh,
            skip_usage=args.skip_usage,
            force_full_usage=args.force_full_usage or args.fresh,
        )
        print("Ops artifacts:")
        for k, v in op_paths.items():
            print(f"  {k}: {v}")
    else:
        print("WARNING: workspace_catalog.json missing after Scanner extract")

    return latest


def run_ops_only(temp_root: Path, args) -> Path:
    """Download catalog from SP into temp, enrich ops, return temp latest."""
    from catalog_service.ops_snapshot import run_ops_enrichment

    latest = temp_root / "latest"
    catalog = load_catalog_from_sharepoint()
    # seed catalog into temp for ops writer
    (latest / "workspace_catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False),
        encoding="utf-8",
    )
    # bring prior ops snapshots from SP when present (incremental usage)
    try:
        from catalog_service import catalog_service
        for name in ("usage_snapshot.json", "refresh_snapshot.json", "ops_summary.json"):
            data = catalog_service.get_json(name, force_refresh=True)
            if not data:
                continue
            (latest / name).write_text(
                json.dumps(data, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"  seeded {name} from SharePoint for incremental ops")
            if name == "usage_snapshot.json" and isinstance(data.get("usageState"), dict):
                (latest / "usage_state.json").write_text(
                    json.dumps(data["usageState"], ensure_ascii=False),
                    encoding="utf-8",
                )
                print("  rehydrated usage_state.json from SharePoint usage snapshot")
    except Exception as exc:
        print(f"  ! could not seed prior ops from SP: {exc}")

    paths = run_ops_enrichment(
        catalog,
        out_dir=latest,
        skip_refresh=args.skip_refresh,
        skip_usage=args.skip_usage,
        force_full_usage=args.force_full_usage,
    )
    print("Ops artifacts:")
    for k, v in paths.items():
        print(f"  {k}: {v}")
    return latest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="SharePoint-only catalog extract (temp build  publish  delete temp)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python run_catalog_extract.py --fresh -v\n"
            "  python run_catalog_extract.py --ops-only\n"
            "  python run_catalog_extract.py --clean-sharepoint-only\n"
        ),
    )
    parser.add_argument("--workspaces", type=str, default=None)
    parser.add_argument("--include-personal", action="store_true")
    parser.add_argument("--no-raw", action="store_true",
                        help="Skip writing raw_scan.json into temp")
    parser.add_argument("--ops-only", action="store_true",
                        help="6h job: refresh + usage only (catalog from SharePoint)")
    parser.add_argument("--fresh", action="store_true",
                        help="Full Scanner + ops; wipe SharePoint latest first")
    parser.add_argument("--clean-sharepoint-only", action="store_true")
    parser.add_argument("--skip-refresh", action="store_true")
    parser.add_argument("--skip-usage", action="store_true")
    parser.add_argument("--force-full-usage", action="store_true")
    parser.add_argument("--include-inventory", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if not cfg.sharepoint_configured():
        print("ERROR: SharePoint is required for extract and UI.")
        print("Set SHAREPOINT_SITE_HOSTNAME, SHAREPOINT_SITE_PATH, credentials in .env")
        return 2

    if args.clean_sharepoint_only:
        clean_sharepoint_latest()
        print("SharePoint latest cleaned.")
        return 0

    if args.fresh:
        args.force_full_usage = True

    temp_root = make_temp_root()
    try:
        if args.fresh:
            print("=== FRESH RUN ===")
            print("Clearing SharePoint latest before rebuild")
            clean_sharepoint_latest()
            latest = run_full_extract(temp_root, args)
        elif args.ops_only:
            print("=== OPS-ONLY (6h) ===")
            latest = run_ops_only(temp_root, args)
        else:
            print("=== FULL EXTRACT ===")
            latest = run_full_extract(temp_root, args)

        # Thin UI packs so browser/home never need 300MB catalog blobs
        try:
            from catalog_service.thin_packs import write_thin_packs
            packs = write_thin_packs(latest)
            print("Thin UI packs:")
            for k, v in packs.items():
                print(f"  {k}: {v}")
        except Exception as exc:
            print(f"WARNING: thin pack build failed (UI can fall back): {exc}")

        # Fresh: wipe then replace. Ops-only: overlay produced files only so a
        # partial ops run cannot delete workspace_catalog / impact packs.
        publish_temp_latest(
            latest,
            include_inventory=args.include_inventory,
            clean_first=bool(args.fresh) or not args.ops_only,
        )
        print("Published to SharePoint latest/ (source of truth).")
        return 0
    except Exception:
        logging.exception("Extract failed")
        return 1
    finally:
        _cleanup_temp(temp_root)


if __name__ == "__main__":
    sys.exit(main())
