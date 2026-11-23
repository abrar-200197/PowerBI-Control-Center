# AGENTS.md — Power BI Control Center

Instructions for AI coding agents (AshleyCode/OpenCode) working in this repo.
This file layers on top of the user-level profile rules; where they conflict,
repo-specific guidance here wins.

Agent context — read before implementing anything:

- `.opencode/context/system-overview.md` — architecture, components, API surface
- `.opencode/context/domain-model.md` — entities, business rules, glossary
- `.opencode/examples/golden_examples.md` — patterns to copy, anti-patterns to avoid

Line references below were accurate when written (2026-11-20); re-grep before relying on them.

---

## What this is

A Flask web application for Power BI governance:

- **Automated documentation** — Azure OpenAI GPT-4o section drafts + python-docx Word output
- **Crash Test** — report health diagnostics scored 0–100 (dataset refresh, schema, visual render, DAX)
- **Catalog & Impact Explorer** — precomputed tenant metadata served from SharePoint, reverse impact indexes
- **Usage tracking** — Admin Activity Events (`ViewReport`), 30-day lookback
- **Similarity, PBIP diff, lineage, decommission archiving** to SharePoint

Data sources: Power BI REST API, Admin Scanner API, Fabric API, Microsoft Graph
(SharePoint), DAX via `executeQueries`, and headless Chromium (Playwright) driving
the Power BI JS SDK for visual-level metadata and render-error scanning.

## Commands

| Task | Command |
|---|---|
| Install deps | `pip install -r requirements.txt` |
| Playwright browser | `playwright install chromium` |
| Run app (dev) | `python app.py` → http://localhost:5000 (`debug=True`, reloader OFF) |
| Health check | `GET /health` |
| Catalog extract (full) | `python run_catalog_extract.py --fresh -v` |
| Catalog extract (incremental) | `python run_catalog_extract.py --ops-only` |
| Install dev/test deps | `pip install -r requirements-dev.txt` |
| Tests (full set) | `pytest tests/test_expression_parser.py tests/test_pbir_field_walk.py tests/test_crash_schema_validation.py test_crash_test_render_scan.py` |
| Docker CMD | `gunicorn --bind=0.0.0.0:8000 --timeout 900 --workers 1 --threads 8 --worker-class gthread --max-requests 500 --max-requests-jitter 50 app:app` |
| Publish Azure Function | `func azure functionapp publish afi-powerbi-metadata-extractor-prod-func --python --build remote` (from `azure_function_catalog/`) |

Requires Python 3.12 (Docker image; the GitHub deploy path uses 3.11), an Azure AD
app registration, and a populated `.env` (gitignored — never commit it).

## Architecture map

| Path | Role |
|---|---|
| `app.py` | **The live application** (~9.7k lines): Flask assembly, MSAL auth, 42 routes, background jobs, cache dicts |
| `routes/` | 5 blueprints extracted from app.py: `catalog_bp`, `similarity_bp`, `semantic_models_bp`, `report_diff_bp`, `decommission_bp`. **No `url_prefix`** — routes carry full absolute paths |
| `features/` | "Planning phase" package (its `__init__.py` says so). Several modules are live-imported (pbip_parser/differ, decommission_inventory, report_archive_service, report_usage_tracker, semantic_model_lineage, utils); `auth_decorators.py` is a stale duplicate. Where a route exists in both `app.py` and `features/`, **the app.py copy is authoritative** |
| `catalog_service/` | Precomputed catalog layer: `service.py` (serving + verified disk mirror + TTL cache), `metadata_lib/` (batch extract: pipeline, scanner_client, normalizer, expression_parser, impact_builder, auth), `field_usage_index`, `unused_fields_index`, `semantic_models_index`, `thin_packs`, `ops_snapshot` |
| Root connectors | `powerbi_connector.py` (REST), `scanner_connector.py` (Admin Scanner), `xmla_connector.py` (DAX via `executeQueries`), `combined_metadata_fetcher.py` (orchestrator), `visual_metadata_extractor.py` (.pbix export parse + Playwright render scan) |
| `crash_test_analyzer.py` | Health diagnostics engine (scoring rules in domain-model) |
| `ai_generator.py` | Metadata collector + Azure OpenAI GPT-4o doc sections (hardcoded fallback strings when the LLM call fails) |
| `document_creator.py` | python-docx Word docs incl. Mermaid ER diagram rendered via `mmdc` when available |
| `azure_function_catalog/` | Deployment mirror of the catalog extract as a timer Azure Function. Mostly byte-identical copies of root files, but four have drifted (`catalog_service/service.py`, `metadata_lib/sharepoint_client.py`, `powerbi_connector.py`, `visual_metadata_extractor.py`) — the root copies are richer. Apply edits to shared files in **both** copies |
| `templates/`, `static/` | Jinja templates; page JS is inline `<script>` blocks; CDN libs (jQuery, select2, SheetJS, powerbi-client); **no bundler**. `static/js/app.js` is dead/legacy — referenced by no template |
| `data/` | Runtime caches only, all gitignored: `catalog_cache/latest/` (disk mirror + `.meta.json` sidecars), `flask_sessions/`, `usage_cache/` |

## Conventions

- `@login_required` stacked above `@app.route` / blueprint routes; blueprints are
  imported and registered late in app.py, after the auth helpers exist.
- Route responses: `jsonify({'success': True, ...})` or
  `jsonify({'success': False, 'error': str(e)}), 500`. Do not invent new shapes.
- Logging: mostly `print()` with emoji markers (hence the UTF-8 reconfigure);
  dedicated concerns get an isolated logger with `propagate=False`.
- Long work: `threading.Thread(daemon=True)` + progress dict polled via a
  `/api/.../progress/<job_id>` route, or `ThreadPoolExecutor` inside handlers
  (`max_workers` 10–20).
- Config is env-var driven. `.env` at repo root; `.env.example` does not exist
  (`.dockerignore` expects one) — if you add env vars, document them in a
  `.env.example` (names only).

## Gotchas — read before editing

1. **`.env` load order**: `load_dotenv(override=True)` runs before
   `catalog_service` is imported because `catalog_config.py` binds ~35 constants
   **at import time**. Do not reorder imports in `app.py`.
2. **UTF-8 stdout/stderr reconfigure** at the top of `app.py` (and
   `run_catalog_extract.py`) exists because Windows cp1252 crashes on emoji
   prints when output is redirected. Keep it.
3. **Flask sessions must not live under OneDrive paths** — `_pick_session_dir()`
   routes them to `%TEMP%` (local) or `/home/data` (Azure). OneDrive-synced
   session dirs fail to read back the OAuth `state` → endless `/login` loop.
4. **Circular imports by design**: `routes/*` and `features/*` resolve
   `login_required` and other shared names lazily from `app` at call time.
   Follow the lazy-proxy pattern; do not convert to top-level imports.
5. **Standalone test files need the two-part shim** — a repo-root
   `sys.path.insert` plus the UTF-8 stdout/stderr reconfigure (the code under
   test prints emoji, which crashes on cp1252 consoles otherwise). All four
   test files carry it; copy it into any new test file. (Historical:
   `test_expression_parser.py` used to import a nonexistent
   `metadata_extractor` package — fixed 2026-11-20.)
6. **Tests are not run by CI** (neither `azure-pipelines.yaml` nor the GitHub
   workflow runs tests) and pytest is not in `requirements.txt` — it lives in
   `requirements-dev.txt`. Tests are
   pytest-style functions that also run standalone (`python tests/test_...py`).
7. **README was stale** — dead `docs/` links, test commands referencing
   missing scripts, wrong clone folder name. Cleaned up 2026-11-20; it now
   points at `AGENTS.md` and `.opencode/` for guidance. Trust code over prose
   when they disagree.
8. **`azure_function_catalog/` is a deployment mirror**, not an importable
   package — it imports its own local copies. Shared-file edits must land in
   both places (drift already exists; root is authoritative).
9. **No request/session context inside ThreadPoolExecutor threads** — explicit
   comment at the lineage code. Pass tokens and user IDs as arguments.
10. **app.py defines some caches twice** (L266–276 and L377–387); the second
    block wins (`SCANNER_CACHE_DURATION = 600`). Know which block you are editing.
11. **Catalog heavy blobs stay out of memory** (`CATALOG_KEEP_HEAVY_IN_MEMORY=false`)
    — App Service OOM protection. Don't "fix" this by eagerly loading
    `workspace_catalog.json`; extend the thin-pack pattern instead.
12. **SharePoint is the catalog source of truth**; `data/catalog_cache/latest/`
    is a verified disk mirror (size-checked against Graph metadata), never
    authoritative. SP 404 → 300 s negative cache; on SP failure the stale mirror
    is served rather than an error.
13. Repo lives under **OneDrive sync** — expect file locks and sync lag.
14. `SECRET_KEY` has a hardcoded dev fallback in `app.py` — production must set
    `SECRET_KEY` via env.
15. Don't weaken auth: the `login_required` split (401 JSON for `/api/*`,
    redirect for pages) and the 12-hour absolute session cap are intentional.
    `download_generated_file` deliberately omits `@login_required` (comment in code).
16. **Extraction commits must ship their helpers.** The blueprint-extraction
    commits restored route bodies without the module-level helpers they call
    (`_catalog_reports_shell`, `_enrich_catalog_reports_with_refresh`,
    `_filter_reports_by_refresh_status`, `_fetch_workspace_folder_meta`,
    `_attach_folder_ids`, `_filter_reports_by_folder` in app.py; `threshold`
    + `requests` + loop nesting in routes/similarity.py) — NameErrors that
    silently degraded `/api/reports` to the live-Scanner fallback and killed
    the similarity GET-export path. Guard: `tests/test_apppy_undefined_names.py`
    (pyflakes; run `pip install -r requirements-dev.txt`). When restoring
    route bodies, restore their helpers, and re-run that test.
17. **Pipeline deploys blip; bad rollback tags kill the site.** Every deploy
    runs stop → sleep 15 → start + cold pull: expect a 1–3 min 503 window.
    Never "roll back" to an image tag you have not verified exists in ACR —
    ACR data-plane is not listable from this subscription, a wrong tag gives
    `ImageNotFoundFailure`, the site stays STOPPED (503) and cold-start
    blocked. Verified-good tags as of 2026-11-23: `:593434` (webapp serving;
    its `/mcp` 401 was the PROXY pre-auth check, not proof of sidecar health
    — see #18), `:593433` DOES NOT EXIST in ACR. Container stdout lives in
    `StartupLogs/.sources/*_containerStream.log`; `docker.log` files hold
    orchestrator events only; app boot output including sidecar tracebacks
    is in `StartupLogs/*_success.log`. `/mcp` is only meaningfully probeable
    with a VALID bearer — without one you get the proxy's 401 regardless of
    sidecar health.
18. **The dev env masks pip conflicts — verify resolution before pin
    changes.** `httpx==0.27.0` (needed by openai==1.54.3, which breaks on
    httpx 0.28) made pip backtrack the sidecar's `mcp>=1.2,<2` to mcp 1.12,
    whose func_metadata hard-imports
    `pydantic._internal._typing_extra.eval_type_backport` — removed in
    pydantic 2.11+ (unpinned → 2.12+ in the container). Result: ImportError
    at every container start, webapp fine, `/mcp` → 503 "sidecar
    unreachable". Dev machines never saw it (no openai+httpx clash locally).
    Fix: `httpx==0.27.2` (satisfies mcp 1.30, stays inside 0.27.x). Before
    ANY requirements.txt change run
    `python scripts/check-deps-resolution.py` (clean-venv install + sidecar
    import); after deploy confirm the container stream shows
    "Uvicorn running on http://127.0.0.1:8001" and "MCP warm-up".

## Testing notes

- Style: plain pytest-style functions + bare `assert`; fixtures are plain dicts
  injected as object attributes; no mock library, no network.
- pytest lives in `requirements-dev.txt` (`pip install -r requirements-dev.txt`);
  runtime `requirements.txt` stays lean. Each test file also runs standalone
  (`python tests/<file>.py`).
- Current coverage: PBIR field walking, crash-test schema validation, M
  expression parsing, render-scan merge, Azure Function mode decision.
- Adding a test? Copy the inject-a-dict pattern in golden_examples and keep it
  offline.

## Deployment (two independent paths + a batch function)

- **Docker**: `azure-pipelines.yaml` builds
  `ashleyfurniture.azurecr.io/afi/bi/powerbidocumentation:{BuildId,latest}` and
  deploys to Azure Web App `afi-powerbi-documentation-prod-app`
  (rg `afi-bi-analytics-rg`, managed-identity ACR pull, stop → start → verify).
- **Code deploy**: GitHub Actions on push to `main` → Oryx deploy of a bundled
  venv artifact to Web App `afi-it-financials-automation-prod-app` (Python 3.11).
- **Catalog batch**: Azure Function `CatalogExtractTimer`
  (NCRONTAB `0 30 0,6,12,18 * * *` UTC; Sunday 06:30 → `--fresh`, otherwise
  `--ops-only`) runs `run_catalog_extract.py` as a subprocess, publishes
  artifacts to SharePoint `latest/`, then notifies the web app via
  `POST /api/catalog/refresh-internal` with header `X-Catalog-Refresh-Key`.
- Gunicorn runs **one worker, 8 threads** on purpose (Azure Files session-mount
  race between `/login` and `/getAToken`). Don't raise worker count without
  solving that first.

## MCP server (agent access to the catalog)

`mcp_server/server.py` exposes 12 read-only tools over `catalog_service` so AI
agents can query the catalog without the web UI: workspace/report discovery,
reverse impact analysis, field usage, unused fields, model details.

- Run: `python mcp_server/server.py` (stdio transport; loads the repo `.env`
  itself, works from any working directory)
- Connect in AshleyCode:
  `opencode mcp add powerbi-control-center --global -- python "<repo>\mcp_server\server.py"`
  — then check `/mcps` in the TUI (or `opencode mcp list`).
- Tools: `catalog_status` (call first), `list_workspaces`, `search_reports`,
  `get_workspace_reports`, `get_home_summary`, `top_impact_tables`,
  `lookup_table_impact`, `get_table_impact_detail`, `get_report_sources`,
  `get_model_details`, `get_unused_fields`, `get_field_usage`.
- SECURITY: no per-user ACL — the MCP serves the tenant-wide precomputed
  catalog with the .env service identity (a local stdio server has no
  logged-in user). Scope a machine by setting `MCP_ALLOWED_WORKSPACE_IDS`
  (comma-separated workspace GUIDs in `.env`): workspace/report/field tools
  then filter to that list; impact-analysis tools (`top_impact_tables`,
  `lookup_table_impact`, `get_table_impact_detail`, `get_model_details`,
  `get_home_summary`) are tenant-wide by design and ignore it. Keep it on
  stdio/local; do not expose over HTTP without adding auth + real per-user ACL.
- Smoke test: `python tests/test_mcp_server.py` (spawns the server over
  stdio; may hit SharePoint when the disk mirror needs revalidation — not
  part of the offline pytest set).
- **Zero-git onboarding (pip package)**: colleagues need exactly two
  commands in any terminal — no repo, no script file:
  ```
  python -m pip install --upgrade keyrings.artifacts pbi-control-center-mcp --extra-index-url "https://pkgs.dev.azure.com/ashleyfurniture/Super%20Troopers/_packaging/PbiControlCenter/pypi/simple/"
  pbi-cc-setup
  ```
  pip pulls the wheel from the private feed (one-time browser sign-in via
  `keyrings.artifacts`); `pbi-cc-setup` (shipped in the wheel) seeds the
  credentials template at `~\.ashleycode\mcp\pbi-control-center\.env` and
  registers the server globally. `setup-mcp.ps1 -FromFeed <url>` remains an
  alternative but requires the repo checkout (the script lives in
  `scripts/`). Publish updates with
  `scripts/publish-mcp.ps1 -FeedUploadUrl <feed>/pypi/upload/ -Pat <PAT>`
  after bumping the version. The server finds credentials at
  `~\.ashleycode\mcp\pbi-control-center\.env` first, then repo/CWD `.env`.
  `mcp` is pinned `>=1.2.0,<2` (2.x renamed FastMCP → MCPServer; this repo
  targets the 1.x API).
- **Hosted / remote mode (webapp-like access, same App Service)**:
  the deployed container runs the MCP sidecar (uvicorn, 127.0.0.1:8001,
  started by `start-webapp.sh`) and the Flask app proxies it at `/mcp`
  (`mcp_server/flask_proxy.py`). Set `MCP_HTTP_TOKEN` in App Service
  settings or `/mcp` fails closed (503). Colleagues connect with
  `opencode mcp add powerbi-control-center --global --url "https://<app>.azurewebsites.net/mcp" --header "Authorization=Bearer <token>"`.
  `mcp_server/http_entry.py` also runs standalone for a separate instance.
  **Access decision (2026-11-23)**: production uses the SHARED
  `MCP_HTTP_TOKEN` with tenant-wide catalog scope. Per-user tokens
  (`MCP_USER_TOKENS`) are implemented and dormant — dropped for scale (one
  token per user is unmanageable at team size). The admin per-user
  `artifactAccess` API is RETIRED (returns empty), so live "what this user
  can access" enumeration is not available; live per-user visibility would
  need Entra sign-in on the endpoint + on-behalf-of Power BI access
  (mirroring the webapp) — documented Phase 3 path if/when needed.
- `mcp` used to be dev-only; since the deployed web app hosts the MCP
  sidecar (`/mcp` proxy), `mcp` and `uvicorn` now ship in the deployed
  `requirements.txt`. `pytest` stays in `requirements-dev.txt` only.
- **Default in every session (team-scoped)**: `scripts/setup-mcp.ps1` installs
  a self-contained copy of the server to `~\.ashleycode\mcp\pbi-control-center`
  (jira-style — no repo needed at runtime) and registers it in the user's
  global config. One run per machine:
  `powershell -ExecutionPolicy Bypass -File scripts\setup-mcp.ps1`
  (add `-Dev` to register the repo checkout instead — for live-edit
  development). The script verifies Python and dependencies (offers pip
  install), copies the bundle, seeds `.env` from `.env.example` when the repo
  has none, and validates the install by importing it. Updates: `git pull`
  in the repo, then re-run the script. Only people who run it get it —
  nothing org-wide is touched.
- **Team sharing (in-repo sessions)**: `opencode.jsonc` in the repo root
  registers the server at project level, so colleagues get it automatically
  when they open the repo
  (project config wins over a same-named global entry). Per-machine
  prerequisites: app Python deps plus `mcp`
  (`pip install -r requirements.txt -r requirements-dev.txt`) and a local
  `.env` with Power BI/SharePoint credentials — without it the catalog mode
  is "off" and every tool returns "catalog unavailable". If the relative
  command path misbehaves on a machine (server not starting), fall back to
  the manual registration:
  `opencode mcp add powerbi-control-center --global -- python "<repo>\mcp_server\server.py"`.

## Secrets

- `.env` is local-only and gitignored; credentials come from env/Azure identity.
- `Config.validate()` prints partial secret values to stdout — do not extend it.
- Never commit secrets. Never hardcode tokens or keys.
