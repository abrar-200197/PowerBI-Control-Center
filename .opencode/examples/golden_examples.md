# Golden Examples — Power BI Control Center

> Patterns copied from THIS repo. When implementing here, follow these exact
> shapes — not generic patterns. Generated 2026-11-20.

## 1. Blueprint endpoint (success/error shape)

From `routes/catalog.py:95-112` — the standard endpoint skeleton:

```python
@catalog_bp.route('/api/catalog/status')
@login_required
def api_catalog_status():
    """Catalog availability, mode, freshness — for UI banners and ops."""
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
        return jsonify({
            'success': True,
            'enabled': False, ...
    ...
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
```

Rules: `@login_required` above every route (exception: deliberate ones with an
in-code comment); success payloads are flat dicts starting with `success: True`;
errors return the exception string with 500. Do not invent new response shapes.

## 2. The real auth decorator (app.py:446-462)

```python
def login_required(f):
    """Decorator to require login; enforces browser-close cookie + 12h absolute max."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session or _session_expired():
            if 'user' in session:
                session.clear()
            if request.path.startswith('/api/'):
                return jsonify({
                    'success': False,
                    'error': 'Not authenticated or session expired',
                    'redirect': url_for('login')
                }), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function
```

401 JSON for `/api/*`, redirect for pages. Frontend code relies on this split.

## 3. Lazy proxy for shared names (routes/catalog.py:24-32)

Blueprints and features must not import `app` at module top (app.py is the
`__main__` module → circular import). Resolve lazily at call time:

```python
def login_required(f):
    """Lazy proxy for app.login_required (avoids circular import at load time)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not hasattr(wrapper, '_decorated'):
            from app import login_required as _real_login_required
            wrapper._decorated = _real_login_required(f)
        return wrapper._decorated(*args, **kwargs)
    return wrapper
```

## 4. Scoped logger (routes/similarity.py:27-33)

For a named concern, isolate the logger completely — never touch root logging:

```python
similarity_logger = logging.getLogger('similarity_analysis')
similarity_logger.setLevel(logging.INFO)
if not similarity_logger.handlers:
    _sim_log_handler = logging.StreamHandler()
    _sim_log_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s [similarity] %(message)s'))
    similarity_logger.addHandler(_sim_log_handler)
    similarity_logger.propagate = False
```

Everywhere else the convention is `print()` with emoji markers — this works
because app.py reconfigures stdout/stderr to UTF-8. Keep that reconfigure.

## 5. Background job with progress polling (app.py)

```python
generation_progress = {}  # job_id → {'progress': 0-100, 'status': '…', 'complete': False, 'error': None}
...
generation_progress[job_id] = {'progress': 0, 'status': 'Starting...', 'complete': False}
thread = threading.Thread(
    target=_generate_documentation_background,
    args=(workspace_id, report_id, dataset_id, report_name, job_id, user_token),
    daemon=True
)
thread.start()
return jsonify({'success': True, 'job_id': job_id, 'message': 'Documentation generation started'})
```

Poller: `GET /api/generate/progress/<job_id>` returns
`generation_progress.get(job_id, {...default...})`; a cleanup thread deletes
the entry and the generated file after 5 minutes. Pass the user token as a
thread **argument** — session/request context is not available in threads.

## 6. Persistent day-bucket cache (app.py _usage_cache_path)

```python
def _usage_cache_path(workspace_id: str) -> str:
    """Disk path for per-workspace Activity Events day cache."""
    root = (os.getenv("USAGE_CACHE_DIR") or "").strip()
    if not root:
        # Prefer durable App Service path; fall back to repo-local .usage_cache
        if os.getenv("WEBSITE_HOSTNAME"):
            root = os.path.join("/home", "data", "usage_cache")
        else:
            root = os.path.join(os.getcwd(), ".usage_cache")
    return os.path.join(root, f"usage_{workspace_id}.json")
...
persistent_cache = {'daily_data': {}, 'last_updated': None}
if os.path.exists(cache_file):
    with open(cache_file, 'r', encoding='utf-8') as f:
        persistent_cache = json.load(f)
missing_dates = [d for d in all_dates_needed if d not in cached_dates]
```

Cache-location pattern to copy: env override → Azure durable path → repo-local
gitignored dir. Variants in the wild: in-memory TTL dicts
(`_CACHE = {'ts': 0.0, 'payload': None}`, 300 s, `features/decommission_inventory.py`),
pickle with `cached_at` + 24 h TTL (`.visual_cache/<report_id>.pkl`), and
lock-guarded TTL caches with `threading.Lock`
(`routes/semantic_models.py` scan cache, 45 min).

## 7. Offline test pattern (tests/test_crash_schema_validation.py)

```python
def _analyzer():
    return CrashTestAnalyzer(
        workspace_id="ws", report_id="rpt", dataset_id="ds",
        access_token="token",
    )

def test_schema_validation_flags_missing_fields():
    a = _analyzer()
    a.dataset_schema = {"tables": ["Sales"], "columns": {"Sales": [...]}, "measures": {...}}
    a.visual_metadata = [{"displayName": "Overview", "visuals": [...]}]
    n = a._validate_visual_bindings_against_schema()
    assert n == 1
...
if __name__ == "__main__":
    test_schema_validation_flags_missing_fields(); ...; print("OK")
```

Plain pytest-style functions, bare asserts, dicts injected as attributes, no
mocks, no network — plus a standalone runner. Copy this shape. Every standalone
test file must also start with the two-part shim (repo-root `sys.path.insert`
+ UTF-8 stream reconfigure) — see the headers of `tests/test_*.py`.

## 8. Service/manager class shape

`CatalogService` (`catalog_service/service.py`): one class, module-level
singleton `catalog_service`, `RLock` around shared state, `status()` /
`is_available()` introspection, `get_json(name, force_refresh=False)` as the
single entry to the artifact store, thin per-concern query methods on top.
Follow this shape for new serving layers.

## API request/response examples

- Success: `GET /api/home-summary` → `{'success': True, 'enabled': ..., <kpis>}` (+ `X-Data-Source` header)
- Auth failure: any `/api/*` → `401 {'success': False, 'error': 'Not authenticated or session expired', 'redirect': '/login'}`
- Endpoint error: `500 {'success': False, 'error': '<str(e)>'}`
- Machine endpoint: `POST /api/catalog/refresh-internal` requires header
  `X-Catalog-Refresh-Key` matching env `CATALOG_REFRESH_SECRET` (else 401/503)
- Crash test POST: body may include `visual_field_bindings` (page-shaped list
  or `{visual_id: {page, title, type, fields}}`); response carries
  `health_score`, `status`, `issues[]`, `score_breakdown`, `visual_analysis{...}`
- File compare: `POST /api/report-differences/compare` multipart
  (`version_a`, `version_b` PBIP zips) → `{'success', 'summary', 'diff'}`

## Anti-patterns — things NOT to do here

1. **Don't top-level-import `app` from routes/features** — circular import.
   Use the lazy proxy.
2. **Don't trust the README** — its `docs/` links are dead and its test
   commands reference missing scripts. Verify against code.
3. **Don't import root modules in test files without the shim** — every
   standalone test file needs the repo-root `sys.path.insert` + UTF-8 stream
   reconfigure header (see `tests/test_expression_parser.py`); without it you
   get `ModuleNotFoundError` or cp1252 `UnicodeEncodeError` crashes.
4. **Don't use `features/auth_decorators.py`** — stale duplicate pointing at a
   nonexistent `auth_sso.login`. The live decorator is in `app.py`.
5. **Don't edit one copy of a mirrored file** — `azure_function_catalog/`
   duplicates shared modules; apply the same change to both (root is
   authoritative when they disagree).
6. **Don't touch request/session inside executor threads** — pass values as args.
7. **Don't add `url_prefix` to blueprints** — routes are written as full
   absolute paths; changing this breaks every link.
8. **Don't eagerly load heavy catalog blobs into memory** — OOM protection;
   extend thin packs instead.
9. **Don't introduce new response envelope shapes** — reuse
   `{'success': True/False, ...}`.
10. **Don't put state under OneDrive-synced dirs** (sessions, caches) — use the
    env override → `/home/data` (Azure) → `%TEMP%`/gitignored repo dir pattern.
11. **Don't add network calls to tests** — keep the dict-injection style.
12. **Don't reorder the top of app.py** — `load_dotenv(override=True)` must
    precede the catalog import (import-time env binding), and the UTF-8
    reconfigure must stay first-ish for cp1252 safety.
