# Catalog Extract — Azure Function (smart timer)

Schedules SharePoint catalog rebuilds for Power BI Control Center.

## Schedule (UTC)

| Cron (`function.json`) | Fires |
|------------------------|--------|
| `0 30 0,6,12,18 * * *` | **00:30, 06:30, 12:30, 18:30** every day |

### Smart mode

| When (UTC) | Mode | Command |
|------------|------|---------|
| **Sunday 06:30** | **Fresh full rebuild** | `python run_catalog_extract.py --fresh -v` |
| All other slots | Ops only (6h) | `python run_catalog_extract.py --ops-only -v` |

Example week cadence:

- Sun 06:30 → fresh  
- Sun 12:30 → ops  
- Sun 18:30 → ops  
- Mon 00:30 → ops  
- Mon 06:30 → ops  
- … every 6 hours at `:30`

## Plan requirements

- Use **Premium** or **Dedicated** (App Service) plan — **not** Consumption for `--fresh` (can run 1–5 hours).
- `host.json` sets `"functionTimeout": "05:00:00"` (needs plan that allows it).
- Enough memory for large catalog JSON (~GB recommended during fresh).

## Deploy layout (flat — Python v2 programming model)

All Azure Function entrypoints are **root files**. No `CatalogExtractTimer/` folder.

```text
azure_function_catalog/          (= wwwroot after publish)
  function_app.py                ← timer CatalogExtractTimer (v2 decorator)
  host.json
  requirements.txt
  .python_version                ← 3.11
  run_catalog_extract.py
  powerbi_connector.py
  catalog_service/               ← Python package (imports only, not a "function")
```

### Publish

```powershell
cd azure_function_catalog
func azure functionapp publish afi-powerbi-metadata-extractor-prod-func --python --build remote
```

Stack must be **Python 3.11** (not 3.6). After publish, Overview → Functions should list **CatalogExtractTimer**.

`catalog_service/` must stay a folder — that is normal Python packaging (`import catalog_service`), not an Azure Functions convention.

## App settings (Application settings)

Same as local extract / web app (no secrets in git):

| Setting | Purpose |
|---------|---------|
| `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET` | Power BI SP |
| `SHAREPOINT_SITE_HOSTNAME`, `SHAREPOINT_SITE_PATH` | Graph site |
| `SHAREPOINT_FOLDER_PATH`, `SHAREPOINT_DRIVE_NAME` | Library path |
| `SHAREPOINT_*` client override | Optional separate Graph app |
| `CATALOG_REPO_ROOT` | Path containing `run_catalog_extract.py` if non-default |
| `CATALOG_EXTRACT_VERBOSE` | `true` / `false` (default true) |
| `USAGE_LOOKBACK_DAYS` | default 30 |
| `AzureWebJobsStorage` | Required by Functions runtime |

Copy from `local.settings.json.example` into Azure Portal → Configuration.

## Manual test

Portal → Function → **Code + Test** → **Run**, or:

```bash
# Local
cd azure_function_catalog
cp local.settings.json.example local.settings.json
# fill values; set CATALOG_REPO_ROOT to repo root
func start
```

Force mode without waiting for Sunday:

```bash
# From repo root
python run_catalog_extract.py --ops-only -v
python run_catalog_extract.py --fresh -v
```

## Mode decision unit check

```bash
python -c "from datetime import datetime, timezone; import sys; sys.path.insert(0,'azure_function_catalog/CatalogExtractTimer'); import importlib.util; ..."
```

Or run `python azure_function_catalog/test_decide_mode.py` from repo root.

## Notes

- Does **not** replace the crash-test timer; runs **alongside** it.
- Concurrent fresh + crash-test: avoid heavy overlap if memory is tight.
- SharePoint `latest/` is cleaned only on `--fresh` (existing extract behavior).
