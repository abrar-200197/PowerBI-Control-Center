# Manual Deployment Steps — SUPERSEDED

> **This document described an architecture that no longer exists in this
> repo** (a webhook-driven Azure Function on a Consumption plan with Python
> 3.9, plus a `register_webhook.py` script — none of which is present in the
> codebase anymore). Following it would provision a Function that cannot run
> the 1–5 hour `--fresh` extract and is missing the
> SHAREPOINT_*/CATALOG_REFRESH_SECRET configuration.
>
> **The real deployment documentation is [`azure_function_catalog/README.md`](azure_function_catalog/README.md)**:
> a timer-triggered Function (`CatalogExtractTimer`, NCRONTAB
> `0 30 0,6,12,18 * * *` UTC; Sunday 06:30 run is `--fresh`) on a
> Premium/Dedicated plan with Python 3.11 and a 5-hour `functionTimeout`.
>
> For the web app see `AGENTS.md` (Deployment section) and `azure-pipelines.yaml`.
