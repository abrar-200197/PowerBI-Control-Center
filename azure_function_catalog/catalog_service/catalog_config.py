"""
Catalog configuration for precomputed Power BI metadata.

Used by CatalogService + vendored metadata_lib (Scanner extract, SharePoint).
All values come from environment / .env — safe defaults keep Control Center
working when SharePoint catalog is not configured (live API fallback).
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Power BI service principal (shared with Control Center)
# ---------------------------------------------------------------------------
TENANT_ID = os.getenv("TENANT_ID", "")
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")

ADMIN_API_BASE = "https://api.powerbi.com/v1.0/myorg/admin"
PBI_SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]

# Scanner tuning (batch extract)
WORKSPACE_BATCH_SIZE = int(os.getenv("SCANNER_BATCH_SIZE", "100"))
SCAN_POLL_INTERVAL_SEC = float(os.getenv("SCANNER_POLL_INTERVAL_SEC", "5"))
SCAN_POLL_MAX_WAIT_SEC = int(os.getenv("SCANNER_POLL_MAX_WAIT_SEC", "3600"))
HTTP_TIMEOUT_SEC = int(os.getenv("HTTP_TIMEOUT_SEC", "120"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
RETRY_BASE_DELAY_SEC = float(os.getenv("RETRY_BASE_DELAY_SEC", "2"))

SCAN_DATASET_SCHEMA = True
SCAN_DATASET_EXPRESSIONS = True
SCAN_LINEAGE = True
SCAN_DATASOURCE_DETAILS = True
SCAN_GET_ARTIFACT_USERS = False

WORKSPACE_ALLOWLIST = [
    w.strip()
    for w in os.getenv("WORKSPACE_ALLOWLIST", "").split(",")
    if w.strip()
]

OUTPUT_DIR = Path(os.getenv("METADATA_OUTPUT_DIR", PROJECT_ROOT / "data" / "catalog_output"))
LOG_DIR = Path(os.getenv("METADATA_LOG_DIR", PROJECT_ROOT / "logs"))

# ---------------------------------------------------------------------------
# SharePoint / Graph (optional — catalog fast path)
# ---------------------------------------------------------------------------
SHAREPOINT_TENANT_ID = os.getenv("SHAREPOINT_TENANT_ID", "") or TENANT_ID
SHAREPOINT_CLIENT_ID = os.getenv("SHAREPOINT_CLIENT_ID", "") or CLIENT_ID
SHAREPOINT_CLIENT_SECRET = os.getenv("SHAREPOINT_CLIENT_SECRET", "") or CLIENT_SECRET
SHAREPOINT_SITE_HOSTNAME = os.getenv("SHAREPOINT_SITE_HOSTNAME", "")
SHAREPOINT_SITE_PATH = os.getenv("SHAREPOINT_SITE_PATH", "")
SHAREPOINT_DRIVE_NAME = os.getenv("SHAREPOINT_DRIVE_NAME", "Documents")
SHAREPOINT_FOLDER_PATH = os.getenv("SHAREPOINT_FOLDER_PATH", "PowerBI Reports MetaData")
# Default ON — extract publishes to SharePoint as system of record
SHAREPOINT_UPLOAD_ENABLED = os.getenv("SHAREPOINT_UPLOAD_ENABLED", "true").lower() in (
    "1", "true", "yes", "y",
)
# Before each publish, delete listed files under latest/ so UI cannot serve stale mix
SHAREPOINT_CLEAN_BEFORE_UPLOAD = os.getenv("SHAREPOINT_CLEAN_BEFORE_UPLOAD", "true").lower() in (
    "1", "true", "yes", "y",
)
SHAREPOINT_PUBLISH_FILES = [
    "summary.json",
    "impact_index.json",
    "workspace_catalog.json",
    "sources.json",
    "refresh_snapshot.json",
    "usage_snapshot.json",
    "ops_summary.json",
    # Thin UI packs (KB–low-MB) — browser / home never need the 300MB+ blobs
    "ui_home_index.json",
    "ui_impact_tables.json",
    "ui_impact_reports.json",
    "ui_report_directory.json",
]

# Report Catalog → Archive PBIX/RDL to SharePoint (decommission activity)
# Same site/drive as catalog metadata; different folder tree under Backup - Reports & Archives.
# Path pattern:
#   {SHAREPOINT_DECOMM_BASE}/<latest dated child>/
#     <WorkspaceName>/[<PBI Folder>/]<ReportName>.pbix|.rdl
_DEFAULT_DECOMM_BASE = (
    "BA - Retail Offshore GCC Team/Backup - Reports & Archives/Report Decommission Activity"
)
SHAREPOINT_DECOMM_FOLDER_PATH = (
    os.getenv("SHAREPOINT_DECOMM_FOLDER_PATH") or _DEFAULT_DECOMM_BASE
).strip().strip("/")

# Ops snapshot tuning (batch job)
USAGE_LOOKBACK_DAYS = int(os.getenv("USAGE_LOOKBACK_DAYS", "60"))
OPS_REFRESH_WORKERS = int(os.getenv("OPS_REFRESH_WORKERS", "8"))
OPS_USAGE_DAY_WORKERS = int(os.getenv("OPS_USAGE_DAY_WORKERS", "6"))
OPS_HTTP_TIMEOUT_SEC = int(os.getenv("OPS_HTTP_TIMEOUT_SEC", "30"))
# Exclude internal / dev viewers from usage counts (comma-separated Azure AD group display names)
# Members are resolved via Microsoft Graph (needs GroupMember.Read.All + User.Read.All for UPNs)
USAGE_EXCLUDE_GROUP_NAMES = [
    g.strip()
    for g in (os.getenv("USAGE_EXCLUDE_GROUP_NAMES") or "Sg_GCC_CentralAnalytics_Unv").split(",")
    if g.strip()
]
# Explicit UPNs/emails always excluded (Activity usually logs UPN, not Graph object id).
# Default = Central Analytics SG roster. Override/extend via USAGE_EXCLUDE_USER_UPNS env
# (comma-separated). Env values are MERGED with the default list.
_DEFAULT_USAGE_EXCLUDE_USER_UPNS = (
    "mahmed@ashleyfurnitureindia.com,"
    "pshivanandam@ashleyfurnitureindia.com,"
    "aramalingam@ashleyfurnitureindia.com,"
    "mthanapathi@ashleyfurnitureindia.com,"
    "ksambasivam@ashleyfurnitureindia.com,"
    "danandkumar@ashleyfurnitureindia.com,"
    "jravikumar@ashleyfurnitureindia.com,"
    "kviswanathan@ashleyfurnitureindia.com,"
    "ychandran@ashleyfurnitureindia.com,"
    "nkathiresan@ashleyfurnitureindia.com,"
    # Same person often appears as @ashleyfurniture.com in Activity Events
    "mahmed@ashleyfurniture.com"
)
USAGE_EXCLUDE_USER_UPNS = sorted(
    {
        u.strip().lower()
        for u in (
            [x for x in _DEFAULT_USAGE_EXCLUDE_USER_UPNS.split(",") if x.strip()]
            + [
                x.strip().lower()
                for x in (os.getenv("USAGE_EXCLUDE_USER_UPNS") or "").split(",")
                if x.strip()
            ]
        )
        if u.strip()
    }
)

# Catalog load mode — SharePoint is the source of truth.
# Values other than sharepoint/off are coerced to sharepoint when SP is configured.
CATALOG_DATA_SOURCE = (os.getenv("CATALOG_DATA_SOURCE") or "sharepoint").lower()
# Build scratch for extract-before-upload only (NOT a UI data source)
CATALOG_LOCAL_DIR = Path(
    os.getenv("METADATA_OUTPUT_DIR", PROJECT_ROOT / "data" / "catalog_output")
) / "latest"
# Durable server-side mirror of SharePoint latest/ (NOT source of truth).
# After a verified download, subsequent app starts load from here when meta
# (size + lastModified) still matches Graph — browser never downloads these files.
#
# Azure App Service Linux: /app is often ephemeral/not reliably writable.
# Prefer /home/data (App Service home mount) when available, else project data, else /tmp.
def _default_catalog_cache_dir() -> Path:
    env = (os.getenv("CATALOG_CACHE_DIR") or "").strip()
    if env:
        p = Path(env)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return p

    candidates = []
    if os.getenv("WEBSITE_SITE_NAME") or os.path.isdir("/home"):
        candidates.append(Path("/home/data/catalog_cache/latest"))
    candidates.append(PROJECT_ROOT / "data" / "catalog_cache" / "latest")
    candidates.append(
        Path(os.getenv("TMPDIR") or os.getenv("TEMP") or "/tmp")
        / "pbi_catalog_cache"
        / "latest"
    )

    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return cand
        except Exception:
            continue
    # Last resort — even if not writable yet; writer will re-mkdir
    return candidates[-1]


CATALOG_CACHE_DIR = _default_catalog_cache_dir()
CATALOG_CACHE_TTL_SEC = int(os.getenv("CATALOG_CACHE_TTL_SEC", "3600"))
# How often to re-check SharePoint item meta (size/mtime) against the disk mirror.
CATALOG_DISK_REVALIDATE_SEC = int(os.getenv("CATALOG_DISK_REVALIDATE_SEC", "300"))
# Local folder is never treated as source of truth (SharePoint owns truth).
ALLOW_LOCAL_CATALOG_FALLBACK = False
# Use catalog for /api/reports structure when available
CATALOG_FAST_PATH_ENABLED = os.getenv("CATALOG_FAST_PATH_ENABLED", "true").lower() in (
    "1", "true", "yes", "y",
)
# Keep huge JSON parsed in process memory (default OFF — prevents App Service OOM).
# When false: workspace_catalog / impact_index stay on disk only; parsed only for
# the duration of a request (or rebuild), then dropped. Thin ui_*.json still memory-cached.
CATALOG_KEEP_HEAVY_IN_MEMORY = os.getenv(
    "CATALOG_KEEP_HEAVY_IN_MEMORY", "false"
).lower() in ("1", "true", "yes", "y")

REQUIRED_CATALOG_FILES = (
    "workspace_catalog.json",
    "impact_index.json",
    "summary.json",
)
# Multi‑hundred‑MB artifacts — never hold long-lived in worker RAM by default.
HEAVY_CATALOG_FILES = frozenset({
    "workspace_catalog.json",
    "impact_index.json",
    "inventory.json",
    "refresh_snapshot.json",
    "usage_snapshot.json",
    "ui_impact_reports.json",  # ~50MB with details; prefer detail APIs + disk
})
# Never ship these large blobs to the browser — server-side only.
BROWSER_BLOCKED_CATALOG_FILES = frozenset({
    "workspace_catalog.json",
    "impact_index.json",
    "inventory.json",
    "refresh_snapshot.json",
})
# Safe small JSON the browser may request via /api/catalog/data/*
BROWSER_ALLOWED_CATALOG_FILES = frozenset({
    "summary.json",
    "ops_summary.json",
    "sources.json",
    "ui_home_index.json",
    "ui_impact_tables.json",
    # Grid list only is safe; API strips detailsByReportId before browser use
    "ui_impact_reports.json",
    "ui_report_directory.json",
})


def sharepoint_configured() -> bool:
    return bool(
        SHAREPOINT_SITE_HOSTNAME
        and SHAREPOINT_SITE_PATH
        and SHAREPOINT_CLIENT_ID
        and SHAREPOINT_CLIENT_SECRET
        and SHAREPOINT_TENANT_ID
    )


def validate_pbi_config() -> None:
    missing = [k for k, v in {
        "TENANT_ID": TENANT_ID,
        "CLIENT_ID": CLIENT_ID,
        "CLIENT_SECRET": CLIENT_SECRET,
    }.items() if not v]
    if missing:
        raise ValueError(f"Missing Power BI env vars: {', '.join(missing)}")


def validate_sharepoint_config() -> None:
    missing = [k for k, v in {
        "SHAREPOINT_TENANT_ID": SHAREPOINT_TENANT_ID,
        "SHAREPOINT_CLIENT_ID": SHAREPOINT_CLIENT_ID,
        "SHAREPOINT_CLIENT_SECRET": SHAREPOINT_CLIENT_SECRET,
        "SHAREPOINT_SITE_HOSTNAME": SHAREPOINT_SITE_HOSTNAME,
        "SHAREPOINT_SITE_PATH": SHAREPOINT_SITE_PATH,
    }.items() if not v]
    if missing:
        raise ValueError(f"Missing SharePoint env vars: {', '.join(missing)}")


# Aliases expected by vendored metadata_lib.pipeline
validate_config = validate_pbi_config
