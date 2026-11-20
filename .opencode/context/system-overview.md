# System Overview — Power BI Control Center

> Generated from codebase analysis, 2026-11-20. Update after major
> architectural changes (see `AGENTS.md`).

## Project type & tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12 (Docker) / 3.11 (GitHub deploy path), Flask 3.0.0, Flask-Session 0.8.0 |
| Auth | MSAL 1.24.0 — user-delegated auth-code flow (web app) + service principal client-credentials (batch/extract) |
| Microsoft APIs | Power BI REST (`api.powerbi.com/v1.0/myorg`), Admin Scanner API, Fabric API (`api.fabric.microsoft.com/v1`), Microsoft Graph (`graph.microsoft.com/v1.0`, SharePoint drive), DAX via `datasets/executeQueries` |
| AI | Azure OpenAI GPT-4o (`ai_generator.py`, deployment `gpt-4o`, hardcoded fallbacks on failure) |
| Browser automation | Playwright headless Chromium + `powerbi-client` / `powerbi-report-authoring` JS SDK (loaded from jsDelivr inside the自动化 page) |
| Documents | python-docx (Word), openpyxl/xlsxwriter (Excel), Mermaid ER via `mmdc` CLI when available |
| Frontend | Vanilla JS inline in Jinja templates; CDN libs (jQuery 3.6, select2, SheetJS xlsx, powerbi-client 2.23); dark theme via CSS variables; no bundler |
| Runtime | Dev: `python app.py` (localhost:5000). Prod: gunicorn 1 worker × 8 threads on :8000 in Docker (Azure Web App `afi-powerbi-documentation-prod-app`); separate Oryx code-deploy path to `afi-it-financials-automation-prod-app`; Azure Function timer for batch extract |

## Architecture patterns

1. **Live monolith + extracted edges.** `app.py` (~9.7k lines) is the running
   application. Five blueprints in `routes/` and a "planning phase" `features/`
   package are partially extracted; where a route exists in both, the app.py
   copy is authoritative. Shared names (`login_required`, token getters) are
   resolved lazily from `app` at call time to dodge circular imports.
2. **Two-phase metadata ("Combined Approach").** Phase 1: Admin Scanner API for
   dataset schema/expressions/relationships. Phase 2: report-definition export
   (.pbix zip parse: classic `Report/Layout` or PBIR `visual.json`) for field
   bindings, plus Playwright render walk when runtime error detection is needed.
3. **Precomputed catalog with SharePoint as source of truth.** Batch job
   (`run_catalog_extract.py`) extracts → normalizes → builds indexes → publishes
   to SharePoint `latest/` → notifies the web app. The web app serves from
   SharePoint via a verified disk mirror (`data/catalog_cache/latest/` with
   `.meta.json` sidecars) and an in-process TTL cache; heavy blobs are never
   held in memory — UI gets "thin packs" (KB-scale derived JSON).
4. **Offline-capable tests.** Plain pytest-style functions; dict fixtures
   injected as attributes; pure helpers only (no network, no mocks).

## Key components & locations

| Component | Location | Notes |
|---|---|---|
| Flask app, auth, 42 routes | `app.py` | Blueprint registration L906–941; `login_required` L446; MSAL cache-in-session L333–362; entry L9671 |
| Blueprints | `routes/{catalog,similarity,semantic_models,report_diff,decommission}.py` | No `url_prefix`; lazy proxies for auth; ~36 routes |
| Planning-phase features | `features/` | pbip_parser, pbip_differ, decommission_inventory, report_archive_service, report_usage_tracker, semantic_model_lineage, utils (live); auth_decorators (stale) |
| REST connector | `powerbi_connector.py` | `PowerBIConnector`; user token preferred, SP fallback; refresh-info helpers shared with ops_snapshot |
| Scanner connector | `scanner_connector.py` | `PowerBIScanner`; getInfo/scanStatus/scanResult; fresh token per call |
| DAX relationships | `xmla_connector.py` | `XMLAConnector`; NOT the XMLA endpoint — `executeQueries` with `INFO.VIEW.RELATIONSHIPS()` fallback chain |
| Orchestrator | `combined_metadata_fetcher.py` | `CombinedMetadataFetcher` — Scanner + Playwright phases (note: passes a list to `run_scan`, which expects a string — known mismatch) |
| Visual extraction | `visual_metadata_extractor.py` | .pbix export parse (4 field-extraction strategies) + Playwright embed/render scan; token cache w/ lock; 401/403 retry backoff |
| Crash Test | `crash_test_analyzer.py` | `CrashTestAnalyzer.run_crash_test(...)`; scoring in domain-model doc |
| Doc generation | `ai_generator.py`, `document_creator.py` | GPT-4o sections + docx assembly; background thread with progress dict |
| Catalog serving | `catalog_service/service.py` | `catalog_service` singleton; `get_json`, thin query APIs, home summary classification |
| Catalog batch | `catalog_service/metadata_lib/pipeline.py` + `run_catalog_extract.py` | `run_extraction()`; artifacts → temp dir → SharePoint `latest/` |
| Supporting indexes | `catalog_service/{field_usage_index,unused_fields_index,semantic_models_index,thin_packs,ops_snapshot}.py` | Weekly field-usage, unused-fields, semantic-models index (→ SharePoint "agent" folder), thin packs, 6-hourly refresh/usage snapshot |
| Batch scheduler | `azure_function_catalog/function_app.py` | Timer `CatalogExtractTimer` cron `0 30 0,6,12,18 * * *` UTC; Sunday 06:30 `--fresh`, else `--ops-only`; auto-escalate to fresh on ops failure |
| Tests | `tests/` + root `test_crash_test_render_scan.py` + `azure_function_catalog/test_decide_mode.py` | Offline dict-fixture style; one file has broken imports |

## Catalog data flow

```
run_catalog_extract.py (--fresh | --ops-only)   [writes only to a temp dir]
  --fresh:    pipeline.run_extraction()
              Scanner batches (100 ws/batch) → normalize_workspaces()
              → inventory.json + impact_index.json + sources.json + summary.json
              → workspace_catalog.json
              → ops_snapshot.run_ops_enrichment() → refresh_snapshot / usage_snapshot / ops_summary
              → field_usage_index → unused_fields_index
  --ops-only: download workspace_catalog.json from SharePoint → ops enrichment only
  both:       thin_packs.write_thin_packs() (ui_*.json)
              semantic_models_index → SharePoint "agent" subfolder
              _validate_extract_output() → publish to SP {folder}/latest/
              notify_app_catalog_refresh() → POST {app}/api/catalog/refresh-internal
                                            (X-Catalog-Refresh-Key)

serving:  CatalogService.get_json()
          SharePoint latest/  (source of truth)
          → data/catalog_cache/latest/*.json  (verified disk mirror)
          → in-process TTL cache (3600 s; heavy blobs not kept in memory)
          → thin API endpoints → browser (ui_*.json / summary.json only)
```

## Auth model (two paradigms)

**User-delegated (web app, `app.py` only).** Auth-code flow `/login` →
`/getAToken`; MSAL `SerializableTokenCache` serialized into the Flask session;
`get_user_powerbi_token()` (silent refresh when >5 min left) and
`get_user_fabric_token()` (Fabric audience, OBO fallback). Tokens are handed to
connectors explicitly (`PowerBIConnector(user_token=...)`,
`VisualMetadataExtractor(user_token=...)`, `XMLAConnector(...)`, background doc
generation thread args). Session: server-side Flask-Session
(`pbi_session` cookie), 12 h absolute cap, browser-close cookie.

**Service principal (batch/extract).** Five separate implementations: raw token
POST in `powerbi_connector` / `scanner_connector` (no cache; scanner forces a
fresh token per call), cached raw-token impl in `visual_metadata_extractor`
(120 s pre-expiry refresh), and MSAL `ConfidentialClientApplication` in
`catalog_service/metadata_lib/auth.py` (`PowerBIAuth`, PBI scope),
`sharepoint_client.py` (`GraphAuth`, Graph scope), `ops_snapshot.py`
(`OpsAuth`, two apps: PBI + Graph), plus one more in
`features/decommission_inventory.py`. Acquisition is duplicated, not shared —
keep that in mind when changing token plumbing.

## External dependencies

- **API hosts**: `api.powerbi.com`, `login.microsoftonline.com`,
  `graph.microsoft.com`, `api.fabric.microsoft.com`,
  `agr-dev-openai.openai.azure.com` (OpenAI), jsDelivr CDN (JS SDK inside
  Playwright page; CDN libs in templates).
- **Python**: Flask, Flask-Session, msal, requests, openai, httpx, python-docx,
  python-dotenv, Pillow, playwright, openpyxl, pandas, xlsxwriter, aiohttp,
  gunicorn. Not in requirements but referenced: `redis` (optional session
  backend), pytest (ad hoc).
- **Tooling**: Playwright Chromium; `mmdc` (Mermaid CLI, optional for docx diagrams).

## API surface summary (~78 routes)

- **Pages**: `/` (home dashboard), `/documentation`, `/impact`, `/lineage`,
  `/orphaned-reports`, `/semantic-models`, `/similarity-analysis`,
  `/report-differences`, `/decommissioned-reports`
- **Auth**: `/login`, `/getAToken`, `/logout`, `/api/auth-status`, `/api/test-sso`;
  env-gated debug routes (`ENABLE_DEBUG_ROUTES`)
- **Workspace/data** (app.py): `/api/workspaces`, `/api/reports`,
  `/api/reports-metadata/<ws>`, `/api/workspace-summary/<ws>`,
  `/api/workspace/{table-search,content-search,deep-content-search}[/export]`,
  `/api/report-usage/<ws>`, `/api/orphaned-reports/<ws>`,
  `/api/export-inactive-reports/<ws>`, `/api/decommission-list-candidates/<ws>`,
  `/api/clear-cache` (POST)
- **Report lifecycle** (app.py): `/api/report-details/<id>`,
  `/api/reports/crash-test/<id>` (GET/POST; POST accepts frontend
  `visual_field_bindings`), `/api/reports/archive-to-sharepoint` (POST)
- **Doc generation** (app.py): `POST /api/generate` → `job_id`;
  `GET /api/generate/progress/<job_id>`; `GET /api/generate/download/<job_id>`
- **Lineage** (app.py): `/api/dataset/lineage`, `/api/lineage`,
  `/api/report/visual-lineage/<id>`, `/api/lineage/debug`
- **Catalog/Impact** (`routes/catalog.py`): `/api/catalog/status`, `refresh`
  (POST), `refresh-internal` (secret-key auth), `data/<name>` (small artifacts
  only), `/api/catalog/impact/*` thin APIs (compact wire keys), `/api/home-summary[/details]`
- **Similarity** (`routes/similarity.py`): `/api/reports/similarity-analysis/<ws>`,
  `/api/similarity-analysis` (multi-type), `/api/similarity-analysis/export`
- **Semantic models** (`routes/semantic_models.py`): `/api/semantic-models`,
  `/api/semantic-model-details`, `/api/semantic-model-health-check` (POST),
  `/api/semantic-model-documentation`
- **Diff** (`routes/report_diff.py`): `POST /api/report-differences/compare`
  (multipart, two PBIP zips)
- **Decommission** (`routes/decommission.py`): `/api/decommissioned-reports`
  (+ `/dashboard-config`, `/dashboard-embed-token`, `/publish-dataset-feed` POST, `/export`)

Response convention throughout: `{'success': True, ...}` /
`{'success': False, 'error': ...}` with 500 on exceptions; ACL filtering via
`_user_allowed_workspace_ids()` on catalog APIs; `Cache-Control` handled by an
`after_request` hook (no-store unless a handler set `private, max-age`).
