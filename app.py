"""
Power BI Documentation Web Application
Flask backend for generating Power BI documentation on-demand
"""

from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, session, flash
from datetime import datetime, timezone, timedelta
from functools import wraps
import os
import sys
import time
import threading
import logging
from dotenv import load_dotenv
import msal
import uuid
import requests
import json

# Scoped logger for Similarity Analysis routes — dual-writes alongside the
# existing print() calls there (no prints removed, no behavior change).
# Isolated logger (propagate=False) so it never affects root logging config
# or any other module/route.
similarity_logger = logging.getLogger('similarity_analysis')
similarity_logger.setLevel(logging.INFO)
if not similarity_logger.handlers:
    _sim_log_handler = logging.StreamHandler()
    _sim_log_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s [similarity] %(message)s'))
    similarity_logger.addHandler(_sim_log_handler)
    similarity_logger.propagate = False

# Force UTF-8 stdout/stderr so emoji/log prints (e.g. in visual_metadata_extractor.py)
# never crash with UnicodeEncodeError when process output is redirected/logged to a
# file (e.g. under a process manager) on Windows (default cp1252 console encoding).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

# Import your existing modules
from powerbi_connector import PowerBIConnector
from ai_generator import AIDocGenerator
from document_creator import PowerBIDocumentCreator
from config import Config

# Import visual metadata extractor for deep search
from visual_metadata_extractor import VisualMetadataExtractor
import asyncio

# Load environment variables BEFORE catalog import (catalog_config reads env at import time)
load_dotenv(override=True)

# Precomputed tenant catalog (SharePoint / local) — fast path; live APIs remain fallback
try:
    from catalog_service import catalog_service
    CATALOG_AVAILABLE = True
    print("✅ Catalog service loaded (SharePoint/local fast path enabled when configured)")
except Exception as _catalog_import_err:
    catalog_service = None
    CATALOG_AVAILABLE = False
    print(f"⚠️ Catalog service not available: {_catalog_import_err}")

# Shared exclude: platform usage metrics + [App] shells (Catalog / Home / Decomm / etc.)
try:
    from catalog_service.thin_packs import is_excluded_report_name as _is_excluded_report_name
except Exception:
    def _is_excluded_report_name(name):  # type: ignore
        n = (name or "").strip()
        if not n:
            return False
        if n.startswith("[App]"):
            return True
        return n.casefold() in {
            "usage metrics report",
            "report usage metrics report",
            "dashboard usage metrics report",
        }

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'powerbi-doc-generator-secret-key-2024')

# Session configuration
# IMPORTANT: Client-side signed cookies overflow (~4KB) once we store Power BI
# JWTs + the MSAL token_cache. Browsers then drop the cookie → endless login
# loop. Use server-side filesystem sessions (cookie only holds a small id).
SESSION_MAX_HOURS = int(os.getenv('SESSION_MAX_HOURS', '12'))
app.config['SESSION_PERMANENT'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=SESSION_MAX_HOURS)
app.config['SESSION_REFRESH_EACH_REQUEST'] = False

_on_azure = bool(os.getenv('WEBSITE_HOSTNAME'))
# Secure cookies only work over HTTPS. On localhost (http://) they are dropped by
# the browser → OAuth state missing on /getAToken → endless login loop.
# Force insecure cookies for local dev unless you explicitly serve local HTTPS.
_secure_env = (os.getenv('SESSION_COOKIE_SECURE') or '').strip().lower()
if _on_azure:
    app.config['SESSION_COOKIE_SECURE'] = True
elif _secure_env in ('1', 'true', 'yes', 'on'):
    app.config['SESSION_COOKIE_SECURE'] = True
    print("⚠️ SESSION_COOKIE_SECURE=true on non-Azure — only use with local HTTPS")
else:
    app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_NAME'] = 'pbi_session'

# Server-side session store (Flask-Session).
# Local: prefer %TEMP% (or FLASK_SESSION_DIR). Sessions under OneDrive paths often
# fail to read back the OAuth "state" → endless /login loop.
def _pick_session_dir() -> str:
    env = (os.getenv('FLASK_SESSION_DIR') or '').strip()
    candidates = []
    if env:
        candidates.append(env)
    if _on_azure:
        candidates.append('/home/data/flask_sessions')
    # Local non-synced dirs first
    candidates.append(os.path.join(os.environ.get('TEMP') or os.environ.get('TMP') or '/tmp', 'pbi_cc_flask_sessions'))
    candidates.append(os.path.join(os.getcwd(), 'data', 'flask_sessions'))
    last_err = None
    for cand in candidates:
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, '.write_probe')
            with open(probe, 'w', encoding='utf-8') as f:
                f.write('ok')
            os.remove(probe)
            return cand
        except Exception as exc:
            last_err = exc
            continue
    raise RuntimeError(f"No writable Flask session directory (last error: {last_err})")


_sess_dir = _pick_session_dir()

# Default filesystem. Optional Redis when SESSION_REDIS_URL is set (recommended on Azure
# multi-instance / to survive worker recycles without losing mid-login OAuth state).
# Existing successful-login behavior is unchanged when Redis is unset.
app.config['SESSION_FILE_DIR'] = _sess_dir
# Old default 500 pruned server session files aggressively and could delete the
# mid-login OAuth "state" file between /login and /getAToken under load.
try:
    _sess_threshold = int(os.getenv('SESSION_FILE_THRESHOLD', '10000'))
except ValueError:
    _sess_threshold = 10000
app.config['SESSION_FILE_THRESHOLD'] = max(500, _sess_threshold)
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_KEY_PREFIX'] = 'pbi_cc:'
# Ensure Flask always saves session after login/callback
app.config['SESSION_PERMANENT'] = False

_session_backend = 'unset'
_redis_url = (os.getenv('SESSION_REDIS_URL') or os.getenv('REDIS_URL') or '').strip()
if _redis_url:
    try:
        import redis as _redis_mod
        app.config['SESSION_TYPE'] = 'redis'
        app.config['SESSION_REDIS'] = _redis_mod.from_url(_redis_url)
        from flask_session import Session as _FlaskSession
        _FlaskSession(app)
        _session_backend = f'redis:{_redis_url.split("@")[-1] if "@" in _redis_url else "configured"}'
    except Exception as _redis_err:
        print(f"⚠️ SESSION_REDIS_URL set but Redis session init failed ({_redis_err}); using filesystem")
        _redis_url = ''

if not _redis_url:
    app.config['SESSION_TYPE'] = 'filesystem'
    try:
        from flask_session import Session as _FlaskSession
        _FlaskSession(app)
        _session_backend = f'filesystem:{_sess_dir} (threshold={app.config["SESSION_FILE_THRESHOLD"]})'
    except Exception as _sess_err:
        _session_backend = f'cookie-fallback ({_sess_err})'
        print(f"⚠️ Flask-Session unavailable, cookie sessions may overflow: {_sess_err}")

print(f"Session configuration:")
print(f"   SECRET_KEY: {'Set from environment' if os.getenv('SECRET_KEY') else 'Using default (set SECRET_KEY in production!)'}")
print(f"   Session backend: {_session_backend}")
print(f"   Absolute max age: {SESSION_MAX_HOURS}h from login")
print(f"   Cookie secure: {app.config['SESSION_COOKIE_SECURE']}")
print(f"   Cookie SameSite: {app.config['SESSION_COOKIE_SAMESITE']}")
print(f"   REDIRECT will log after auth constants load")

# ============================================================================
# CACHE CONTROL - Prevent stale content after deployments
# ============================================================================
@app.after_request
def add_cache_control_headers(response):
    """
    Prevent browser/CDN caching issues that cause stale content after deployments.

    Issue: Docker layer caching + browser caching = users seeing old code
    Solution: Force no-cache for dynamic content (HTML, JSON)
    """
    # Don't cache API/HTML by default — but keep explicit private caches
    # (e.g. /api/catalog/impact/tables sets private max-age for fast revisits).
    ct = response.content_type or ''
    existing_cc = (response.headers.get('Cache-Control') or '').lower()
    allow_private = 'private' in existing_cc and 'max-age' in existing_cc
    if ('application/json' in ct or 'text/html' in ct) and not allow_private:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

# ⚡ PERFORMANCE OPTIMIZATION: Enhanced caching system
# User-specific workspace caching
workspaces_cache = {}  # Format: {user_id: {'data': [...], 'timestamp': float}}
workspace_cache = {}   # Format: {workspace_id_user_id: {'data': {...}, 'timestamp': float}}
scanner_cache = {}     # Format: {workspace_id_dataset_id: {'data': {...}, 'timestamp': float}}
reports_cache = {}     # Format: {workspace_id_user_id_folder: {'data': [...], 'timestamp': float}}

# Cache durations (in seconds)
WORKSPACES_CACHE_DURATION = 300  # 5 minutes - workspaces list rarely changes
CACHE_DURATION = 300  # 5 minutes - general cache
SCANNER_CACHE_DURATION = 1800  # 30 minutes - scanner data is expensive to fetch

print("✅ Usage cache will persist in memory (cleared only on explicit refresh or restart)")

# Azure AD SSO Configuration
TENANT_ID = os.getenv('TENANT_ID')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
REDIRECT_PATH = "/getAToken"  # Must match redirect URI in Azure AD app registration



# Auto-detect environment and set appropriate redirect URI.
# Localhost must NOT keep a production https://… REDIRECT_URI from .env —
# that causes Azure AD to bounce to the wrong host or drop the local session.
_env_redirect = (os.getenv('REDIRECT_URI') or '').strip()
if os.getenv('WEBSITE_HOSTNAME'):
    # Azure App Service — prefer explicit env, else hostname
    if _env_redirect and 'localhost' not in _env_redirect.lower():
        REDIRECT_URI = _env_redirect
    else:
        REDIRECT_URI = f"https://{os.getenv('WEBSITE_HOSTNAME')}{REDIRECT_PATH}"
    print(f"🌐 Running on Azure App Service: {REDIRECT_URI}")
else:
    # Local dev — always http://localhost:5000 unless env is already localhost
    if _env_redirect and 'localhost' in _env_redirect.lower():
        REDIRECT_URI = _env_redirect
    else:
        if _env_redirect:
            print(
                f"⚠️ Ignoring REDIRECT_URI={_env_redirect!r} on local run "
                f"(use http://localhost:5000{REDIRECT_PATH})"
            )
        REDIRECT_URI = f'http://localhost:5000{REDIRECT_PATH}'
    print(f"💻 Running locally: {REDIRECT_URI}")

# Scopes for user-delegated permissions
# IMPORTANT: Use .default scope to get all consented permissions for Power BI API
# This ensures we get a token for Power BI API, not Microsoft Graph
SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]

# Fabric API scope - needed for getDefinition and other Fabric-specific endpoints
FABRIC_SCOPE = ["https://api.fabric.microsoft.com/.default"]

# Optional: If you also need Graph API access, request it separately
GRAPH_SCOPE = ["User.Read"]

# Base MSAL app (no per-request cache). Prefer _msal_for_request() when
# acquiring/refreshing tokens so the session token_cache is actually used.
msal_app = msal.ConfidentialClientApplication(
    CLIENT_ID,
    authority=AUTHORITY,
    client_credential=CLIENT_SECRET,
)


def _load_cache():
    """Load MSAL token cache from the Flask session."""
    cache = msal.SerializableTokenCache()
    if session.get("token_cache"):
        cache.deserialize(session["token_cache"])
    return cache


def _save_cache(cache):
    """Persist MSAL token cache back into the Flask session."""
    if cache is not None and cache.has_state_changed:
        session["token_cache"] = cache.serialize()
        session.modified = True


def _msal_for_request(cache=None):
    """Confidential client bound to this request's serialized token cache.

    Without binding the cache, acquire_token_silent / get_accounts see an empty
    in-memory cache on every worker, so Fabric step-up / token refresh fails.
    """
    if cache is None:
        cache = _load_cache()
    app_cca = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
        token_cache=cache,
    )
    return app_cca, cache




# Initialize Power BI connector
config = Config()
powerbi = PowerBIConnector()
# Authentication will happen automatically when needed via _get_headers()

# Initialize AI generator
ai_generator = AIDocGenerator(
    openai_api_key=config.OPENAI_API_KEY
)

# Cache for workspaces and reports (to avoid repeated API calls)
# IMPORTANT: These are keyed by user_id to prevent cross-user data leakage
workspaces_cache = {}  # Key: user_id, Value: {'data': [], 'timestamp': 0}
reports_cache = {}  # Key: {workspace_id}_{user_id}, Value: {'data': [], 'timestamp': 0}
scanner_cache = {}  # Key: workspace_id, Value: {'data': {...}, 'timestamp': 0}
usage_cache = {}  # Key: workspace_id, Value: {'data': {...}, 'timestamp': datetime}
# Workspace folder tree (Fabric API) — shared across users, short TTL
workspace_folders_cache = {}  # Key: workspace_id, Value: {'folders': [], 'timestamp': float}
CACHE_DURATION = 300  # 5 minutes
SCANNER_CACHE_DURATION = 600  # 10 minutes for scanner results (expensive operation)
WORKSPACE_FOLDERS_CACHE_DURATION = 600  # 10 minutes — folder names change rarely

# Progress tracking for document generation
generation_progress = {}  # Key: job_id, Value: {'progress': 0-100, 'status': 'message', 'file_path': 'path/to/file.docx', 'complete': False, 'error': None}

# Persistent file-based cache directory for usage metrics
USAGE_CACHE_DIR = os.path.join(os.getcwd(), 'data', 'usage_cache')
os.makedirs(USAGE_CACHE_DIR, exist_ok=True)

def clear_user_cache(user_id):
    """Clear all cached data for a specific user"""
    global workspaces_cache, reports_cache

    # Clear workspace cache for this user
    cache_key = f"workspaces_{user_id}"
    if cache_key in workspaces_cache:
        del workspaces_cache[cache_key]
        print(f"🗑️ Cleared workspace cache for user: {user_id}")

    # Clear report caches for this user
    keys_to_delete = [k for k in reports_cache.keys() if k.endswith(f"_{user_id}")]
    for key in keys_to_delete:
        del reports_cache[key]
    if keys_to_delete:
        print(f"🗑️ Cleared {len(keys_to_delete)} report cache entries for user: {user_id}")

def clear_all_caches():
    """Clear all cached data - used on server startup or manual refresh"""
    global workspaces_cache, reports_cache, scanner_cache, workspace_folders_cache
    workspaces_cache = {}
    reports_cache = {}
    scanner_cache = {}
    workspace_folders_cache = {}
    print("🗑️ Cleared ALL workspace, report, scanner, and folder caches")


def _session_expired() -> bool:
    """True when absolute session age exceeds SESSION_MAX_HOURS (default 12)."""
    if 'user' not in session:
        return True
    started = session.get('login_at')
    if not started:
        # Legacy sessions without stamp — force re-login once
        return True
    try:
        from datetime import datetime, timezone
        if isinstance(started, (int, float)):
            started_ts = float(started)
        else:
            s = str(started)
            if s.endswith('Z'):
                s = s[:-1] + '+00:00'
            started_ts = datetime.fromisoformat(s).timestamp()
        age_sec = time.time() - started_ts
        return age_sec > (SESSION_MAX_HOURS * 3600)
    except Exception:
        return True


def login_required(f):
    """Decorator to require login; enforces browser-close cookie + 12h absolute max."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session or _session_expired():
            if 'user' in session:
                # Absolute age exceeded — hard clear
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


# Debug/diagnostic routes (env/session/token inspection) are disabled by default
# in any deployed environment to avoid leaking config/token details. They only
# respond when ENABLE_DEBUG_ROUTES=1/true is explicitly set (e.g. local dev).
# No behavior change for local dev if that flag is already set; this only
# restricts access when it is not.
_DEBUG_ROUTES_ENABLED = (os.getenv('ENABLE_DEBUG_ROUTES', '').strip().lower() in ('1', 'true', 'yes'))


def debug_only(f):
    """Decorator: 404s the route unless ENABLE_DEBUG_ROUTES is set."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not _DEBUG_ROUTES_ENABLED:
            from flask import abort
            abort(404)
        return f(*args, **kwargs)
    return decorated_function


@app.route('/debug/env')
@debug_only
def debug_env():
    """Debug endpoint to check environment configuration"""
    return jsonify({
        'WEBSITE_HOSTNAME': os.getenv('WEBSITE_HOSTNAME'),
        'REDIRECT_URI_ENV': os.getenv('REDIRECT_URI'),
        'REDIRECT_URI_ACTUAL': REDIRECT_URI,
        'REDIRECT_PATH': REDIRECT_PATH,
        'SECRET_KEY_SET': bool(os.getenv('SECRET_KEY')),
        'CLIENT_ID': CLIENT_ID,
        'TENANT_ID': TENANT_ID,
        'AUTHORITY': AUTHORITY,
        'session_cookie_secure': app.config.get('SESSION_COOKIE_SECURE'),
        'session_cookie_samesite': app.config.get('SESSION_COOKIE_SAMESITE'),
        'request_scheme': request.scheme,
        'request_host': request.host,
        'full_url': request.url
    })


@app.route('/debug/session-test')
@debug_only
def session_test():
    """Test if sessions are working"""
    import uuid

    # Try to set a test value in session
    test_value = str(uuid.uuid4())
    session['test_key'] = test_value

    # Check if we can read it back
    retrieved = session.get('test_key')

    return jsonify({
        'session_working': retrieved == test_value,
        'test_value_set': test_value,
        'test_value_retrieved': retrieved,
        'session_keys': list(session.keys()),
        'cookie_name': app.config.get('SESSION_COOKIE_NAME'),
        'cookies_in_request': list(request.cookies.keys())
    })


def _oauth_redirect_uri() -> str:
    """Must match the browser host (localhost vs 127.0.0.1) and Entra registration."""
    if os.getenv('WEBSITE_HOSTNAME'):
        return REDIRECT_URI
    # Prefer URI stored on /login so code exchange matches authorize request
    stored = session.get('oauth_redirect_uri')
    if stored:
        return stored
    root = (request.url_root or 'http://localhost:5000/').rstrip('/')
    return f"{root}{REDIRECT_PATH}"


@app.route('/login')
def login():
    """Initiate Azure AD SSO login"""
    # Drop prior auth data but keep the filesystem session identity stable so the
    # browser cookie still maps to the same server-side file after AAD returns.
    for _k in (
        'user', 'access_token', 'fabric_access_token',
        'token_cache', 'login_at', 'next', 'test_key',
    ):
        session.pop(_k, None)

    # Generate a unique state token to prevent CSRF attacks
    session["state"] = str(uuid.uuid4())
    # Use the host the user opened (localhost ≠ 127.0.0.1 cookies)
    redirect_uri = REDIRECT_URI
    if not os.getenv('WEBSITE_HOSTNAME'):
        root = (request.url_root or 'http://localhost:5000/').rstrip('/')
        redirect_uri = f"{root}{REDIRECT_PATH}"
    session["oauth_redirect_uri"] = redirect_uri
    session.modified = True
    session.permanent = True  # keep cookie across the AAD round-trip

    print("\n🔐 LOGIN INITIATED:")
    print(f"   Redirect URI: {redirect_uri}")
    print(f"   Config REDIRECT_URI: {REDIRECT_URI}")
    print(f"   Request host: {request.host}")
    print(f"   State token: {session['state']}")
    print(f"   Cookie secure: {app.config.get('SESSION_COOKIE_SECURE')}")
    print(f"   Session dir: {app.config.get('SESSION_FILE_DIR')}")
    print(f"   Session keys: {list(session.keys())}")
    print(f"   Cookies in: {list(request.cookies.keys())}")

    # Build authorization URL with prompt=select_account to force account selection
    auth_url = msal_app.get_authorization_request_url(
        SCOPE,
        state=session["state"],
        redirect_uri=redirect_uri,
        prompt="select_account"  # Force user to select account (no auto-login)
    )

    print(f"   Auth URL: {auth_url[:120]}...")

    return redirect(auth_url)


@app.route(REDIRECT_PATH)
def authorized():
    """Handle the redirect from Azure AD after authentication"""

    redirect_uri = _oauth_redirect_uri()

    # Debug logging for production troubleshooting
    print(f"\n🔍 SSO CALLBACK RECEIVED:")
    print(f"   Request URL: {request.url}")
    print(f"   Request host: {request.host}")
    print(f"   Request state: {request.args.get('state')}")
    print(f"   Session state: {session.get('state')}")
    print(f"   Session keys: {list(session.keys())}")
    print(f"   oauth_redirect_uri: {redirect_uri}")
    print(f"   Cookies: {list(request.cookies.keys())}")
    print(f"   Has code: {bool(request.args.get('code'))}")
    print(f"   Has error: {bool(request.args.get('error'))}")

    # Verify state. On mismatch: clear orphan cookie/session once, then offer a clean
# /login — never leave users refreshing a one-time getAToken?code=… URL.
# Successful path below is unchanged (code exchange / session fill).
    if request.args.get('state') != session.get("state"):
        print(f"❌ STATE MISMATCH - Session may not be persisting!")
        print(f"   SESSION_COOKIE_SECURE={app.config.get('SESSION_COOKIE_SECURE')}")
        print(f"   REDIRECT_URI config={REDIRECT_URI}")
        print(f"   request_state={request.args.get('state')!r} session_state={session.get('state')!r}")
        print(f"   cookies={list(request.cookies.keys())} session_keys={list(session.keys())}")
        print(f"   session_backend={_session_backend}")

        # Drop broken server session + oauth leftovers so the next /login is clean.
        try:
            session.clear()
            session.modified = True
        except Exception as _clr_err:
            print(f"   session.clear failed: {_clr_err}")

        _cookie_name = app.config.get('SESSION_COOKIE_NAME') or 'pbi_session'
        _retry_cookie = 'pbi_oauth_retry'
        _is_azure = bool(os.getenv('WEBSITE_HOSTNAME'))
        _secure = bool(app.config.get('SESSION_COOKIE_SECURE'))
        _samesite = app.config.get('SESSION_COOKIE_SAMESITE') or 'Lax'
        # One auto-retry via /login (avoids stuck getAToken bookmark loops).
        # Tracked with a short-lived cookie so the Entra round-trip still counts
        # as "already retried" if state is missing again.
        _already_retried = (request.cookies.get(_retry_cookie) or '').strip() == '1'
        if not _already_retried:
            print("   → one-shot clean redirect to /login (cleared orphan session)")
            resp = redirect(url_for('login'))
            resp.set_cookie(
                _cookie_name,
                '',
                expires=0,
                max_age=0,
                path='/',
                secure=_secure,
                httponly=True,
                samesite=_samesite,
            )
            resp.set_cookie(
                _retry_cookie,
                '1',
                max_age=180,
                path='/',
                secure=_secure,
                httponly=True,
                samesite=_samesite,
            )
            return resp

        _help_prod = f"""
<p style="background:#eef6ff;border:1px solid #bcd;padding:12px;border-radius:8px">
  <b>Production tip:</b> Close this tab. Open
  <a href="{url_for('login')}"><code>/login</code></a> only
  (do not refresh the long <code>getAToken?code=…</code> URL).
  Ensure App Setting <code>SECRET_KEY</code> is set and stable across restarts.
  Optional: set <code>SESSION_REDIS_URL</code> so OAuth state survives worker recycles.
</p>"""
        _help_local = """
<ol>
<li>Always open <b>http://localhost:5000</b> (not 127.0.0.1) unless both URIs are in Entra.</li>
<li>Entra redirect URIs must include <code>http://localhost:5000/getAToken</code>.</li>
<li>.env: <code>SESSION_COOKIE_SECURE=false</code>, stable <code>SECRET_KEY</code>.</li>
<li>Use a fresh Incognito window, then open <a href="/login">/login</a>.</li>
<li><a href="/debug/session-test">/debug/session-test</a> — <code>session_working</code> must stay true.</li>
</ol>"""
        html = f"""<!doctype html><html><body style="font-family:Segoe UI,sans-serif;max-width:740px;margin:40px auto;padding:0 16px;line-height:1.45">
<h2>Sign-in session lost (OAuth state mismatch)</h2>
<p>Azure AD returned, but the server session no longer held the CSRF <code>state</code>
(orphan cookie, recycled worker, pruned session file, or stale <code>getAToken</code> URL).</p>
<pre style="background:#f4f4f4;padding:12px;overflow:auto">request_state = {request.args.get('state')!r}
session_state = {session.get('state')!r}
cookies = {list(request.cookies.keys())!r}
host = {request.host!r}
redirect_uri = {redirect_uri!r}
cookie_secure = {app.config.get('SESSION_COOKIE_SECURE')!r}
session_dir = {app.config.get('SESSION_FILE_DIR')!r}
session_backend = {_session_backend!r}
</pre>
{_help_prod if _is_azure else _help_local}
<p><a href="{url_for('login')}" style="display:inline-block;margin-top:8px;padding:10px 16px;background:#0b5fff;color:#fff;text-decoration:none;border-radius:6px">Try sign-in again</a>
 · <a href="/debug/env">/debug/env</a>
 · <a href="/debug/session-test">/debug/session-test</a></p>
</body></html>"""
        resp = app.make_response((html, 400))
        resp.set_cookie(
            _cookie_name,
            '',
            expires=0,
            max_age=0,
            path='/',
            secure=_secure,
            httponly=True,
            samesite=_samesite,
        )
        # Allow a future mismatch to auto-retry once again after user acts.
        resp.set_cookie(
            _retry_cookie,
            '',
            expires=0,
            max_age=0,
            path='/',
            secure=_secure,
            httponly=True,
            samesite=_samesite,
        )
        return resp

    # Check for errors from Azure AD
    if "error" in request.args:
        error_description = request.args.get("error_description", "Unknown error")
        flash(f'Authentication failed: {error_description}', 'error')
        return redirect(url_for("login"))

    # Exchange authorization code for Power BI access token
    if request.args.get('code'):
        # Drop any leftover step-up markers from older builds
        session.pop("oauth_purpose", None)
        session.pop("oauth_next", None)
        # Bind MSAL to the session token_cache so refresh tokens persist.
        cca, cache = _msal_for_request()

        print("\n🔐 ACQUIRING TOKEN WITH SCOPES:")
        for scope in SCOPE:
            print(f"   - {scope}")
        print(f"   Using redirect_uri={redirect_uri}")

        result = cca.acquire_token_by_authorization_code(
            request.args['code'],
            scopes=SCOPE,
            redirect_uri=redirect_uri,
        )

        if "error" in result:
            print(f"\n❌ TOKEN ACQUISITION FAILED:")
            print(f"   Error: {result.get('error')}")
            print(f"   Description: {result.get('error_description')}")
            print(f"   Correlation ID: {result.get('correlation_id')}")
            flash(f'Authentication failed: {result.get("error_description")}', 'error')
            return redirect(url_for("login"))

        print("\n✅ TOKEN ACQUIRED SUCCESSFULLY")
        print(f"   Scopes in result: {result.get('scope', 'N/A')}")
        print(f"   Token type: {result.get('token_type', 'N/A')}")
        print(f"   Expires in: {result.get('expires_in', 'N/A')} seconds")

        # Replace identity in-place — do NOT session.clear() (drops FS session
        # continuity / cookie mapping and can loop login on localhost).
        old_user_id = (session.get('user') or {}).get('oid')
        keep_keys = {"state", "oauth_redirect_uri"}
        for _k in list(session.keys()):
            if _k not in keep_keys:
                session.pop(_k, None)
        session.pop('state', None)

        # Non-permanent cookie → expires when browser is fully closed.
        # Absolute 12h bound enforced via login_at + login_required.
        session.permanent = False
        # Keep only fields we use — full id_token_claims can be large
        _claims = result.get("id_token_claims") or {}
        session["user"] = {
            "oid": _claims.get("oid"),
            "name": _claims.get("name"),
            "preferred_username": _claims.get("preferred_username")
                or _claims.get("upn")
                or _claims.get("email"),
            "email": _claims.get("email") or _claims.get("preferred_username"),
            "tid": _claims.get("tid"),
        }
        session["access_token"] = result.get("access_token")  # Power BI token
        session["login_at"] = datetime.now(timezone.utc).isoformat()
        session.modified = True
        _save_cache(cache)
        print(f"   Session after login keys: {list(session.keys())}")
        print(f"   User: {session['user'].get('preferred_username')}")
        if old_user_id and old_user_id != session['user'].get('oid'):
            print(f"   Previous oid was {old_user_id} (replaced)")

        # Get the new user ID and clear their old cache if any
        new_user_id = session.get('user', {}).get('oid')
        if new_user_id:
            clear_user_cache(new_user_id)
            print(f"\n👤 NEW LOGIN: {session.get('user', {}).get('name')} ({session.get('user', {}).get('preferred_username')})")
            print(f"   User ID: {new_user_id}")

        # Also clear old user cache if different user
        if old_user_id and old_user_id != new_user_id:
            clear_user_cache(old_user_id)

        # Optional one-shot post-login landing (scroll-to-enter). Not auto-zoom.
        session['show_login_landing'] = True
        resp = redirect(url_for("index"))
        # Clear one-shot OAuth retry marker after a successful sign-in.
        resp.set_cookie(
            'pbi_oauth_retry',
            '',
            expires=0,
            max_age=0,
            path='/',
            secure=bool(app.config.get('SESSION_COOKIE_SECURE')),
            httponly=True,
            samesite=app.config.get('SESSION_COOKIE_SAMESITE') or 'Lax',
        )
        return resp

    flash('No authorization code received', 'error')
    return redirect(url_for("login"))


@app.route('/logout')
def logout():
    """Logout and clear session"""
    session.clear()

    # Redirect to Azure AD logout endpoint
    logout_url = f"{AUTHORITY}/oauth2/v2.0/logout?post_logout_redirect_uri={url_for('login', _external=True)}"
    return redirect(logout_url)


@app.route('/api/debug/token')
@login_required
@debug_only
def debug_token():
    """Debug endpoint to check user's token and scopes"""
    try:
        import jwt
        token = get_user_powerbi_token()

        # Decode token without verification (just to inspect)
        decoded = jwt.decode(token, options={"verify_signature": False})

        return jsonify({
            'success': True,
            'user': session.get('user', {}),
            'scopes': decoded.get('scp', 'No scopes found'),
            'roles': decoded.get('roles', 'No roles found'),
            'aud': decoded.get('aud', 'No audience found'),
            'app_displayname': decoded.get('app_displayname', 'No app name found')
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


def _jwt_seconds_left(token):
    """Return seconds until JWT exp, or None if unreadable."""
    try:
        import base64
        import json
        import time as _t
        parts = (token or "").split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        data = json.loads(base64.b64decode(payload))
        exp = float(data.get("exp") or 0)
        return exp - _t.time()
    except Exception:
        return None


def get_user_powerbi_token():
    """Get the user's Power BI access token from session or refresh it if needed.

    Prefer the in-session JWT when it still has >5 minutes left so workspace
    loads do not hit MSAL/AAD on every folder-meta request.
    """
    # 1) Reuse valid session token (fast path — no AAD round-trip)
    if "access_token" in session:
        left = _jwt_seconds_left(session["access_token"])
        if left is not None and left > 300:
            # Quiet reuse — avoid log spam on every workspace select
            return session["access_token"]
        if left is not None and left <= 300:
            print(f"⚠️ Session Power BI token expiring in {int(left)}s — refreshing…")
            session.pop("access_token", None)

    # 2) MSAL silent refresh only when needed (session-bound cache)
    cca, cache = _msal_for_request()
    accounts = cca.get_accounts(
        username=session.get("user", {}).get("preferred_username")
    )
    if not accounts:
        accounts = cca.get_accounts()

    if accounts:
        print("🔄 Acquiring Power BI token silently (MSAL)…")
        result = cca.acquire_token_silent(SCOPE, account=accounts[0])
        if result and "access_token" in result and "error" not in result:
            print("✅ Power BI token acquired/refreshed")
            session["access_token"] = result["access_token"]
            _save_cache(cache)
            return result["access_token"]
        if result and "error" in result:
            print(f"⚠️ Token refresh error: {result.get('error_description', result.get('error'))}")

    print("❌ No valid Power BI token — user needs to re-authenticate")
    return None


# Decommissioned Reports blueprint — registered here because it imports
# login_required / get_user_powerbi_token / _jwt_seconds_left from this module
# at import time, so app.py must define them first.
from routes.decommission import decommission_bp  # noqa: E402
app.register_blueprint(decommission_bp)

# Similarity Analysis blueprint — same reasoning as above.
from routes.similarity import similarity_bp  # noqa: E402
app.register_blueprint(similarity_bp)


def get_user_fabric_token():
    """
    Fabric API token (audience api.fabric.microsoft.com).

    Reuse session JWT when still valid (>5 min) so Report Catalog workspace
    loads do not call MSAL on every request.
    """
    # 1) Reuse valid Fabric token in session
    if "fabric_access_token" in session:
        left = _jwt_seconds_left(session["fabric_access_token"])
        if left is not None and left > 300:
            return session["fabric_access_token"]
        if left is not None and left <= 300:
            session.pop("fabric_access_token", None)

    # 2) Silent acquire with Fabric scope (session-bound MSAL cache)
    cca, cache = _msal_for_request()
    accounts = cca.get_accounts(
        username=session.get("user", {}).get("preferred_username")
    )
    if not accounts:
        accounts = cca.get_accounts()

    if accounts:
        result = cca.acquire_token_silent(FABRIC_SCOPE, account=accounts[0])
        if result and "access_token" in result and "error" not in result:
            print("✅ Fabric token acquired (silent)")
            session["fabric_access_token"] = result["access_token"]
            _save_cache(cache)
            return result["access_token"]

    # 3) OBO from Power BI token (often fails if assertion audience mismatch — keep as fallback)
    pbi_token = get_user_powerbi_token()
    if pbi_token:
        try:
            result = cca.acquire_token_on_behalf_of(
                user_assertion=pbi_token,
                scopes=FABRIC_SCOPE
            )
            if result and "access_token" in result and "error" not in result:
                print("✅ Fabric token acquired (OBO)")
                session["fabric_access_token"] = result["access_token"]
                _save_cache(cache)
                return result["access_token"]
            else:
                error = result.get('error', 'unknown') if result else 'no result'
                error_desc = result.get('error_description', '') if result else ''
                print(f"⚠️ Fabric OBO failed: {error} - {str(error_desc)[:120]}")
        except Exception as e:
            print(f"⚠️ Fabric OBO error: {e}")

    print("⚠️ Could not acquire Fabric API token")
    return None


def get_user_powerbi_headers():
    """Get HTTP headers for Power BI API requests using user's delegated token"""
    token = get_user_powerbi_token()

    if not token:
        raise Exception("User not authenticated or token expired. Please log in again.")

    # Debug: Decode token to check scopes (for troubleshooting)
    try:
        import base64
        import json
        # JWT tokens have 3 parts separated by dots
        parts = token.split('.')
        if len(parts) == 3:
            # Decode the payload (second part)
            # Add padding if needed
            payload = parts[1]
            payload += '=' * (4 - len(payload) % 4)
            decoded = base64.b64decode(payload)
            token_data = json.loads(decoded)

            print("\n🔍 TOKEN DEBUG INFO:")
            print(f"   Token Audience (aud): {token_data.get('aud', 'N/A')}")
            print(f"   Token Scopes (scp): {token_data.get('scp', 'N/A')}")
            print(f"   Token Roles (roles): {token_data.get('roles', 'N/A')}")
            print(f"   Token Issuer: {token_data.get('iss', 'N/A')}")
            print(f"   User UPN: {token_data.get('upn', 'N/A')}")
            print()
    except Exception as e:
        print(f"⚠️  Could not decode token for debugging: {e}")

    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }


@app.route('/health')
def health():
    """Health check endpoint for Docker and monitoring"""
    catalog_status = None
    if CATALOG_AVAILABLE and catalog_service is not None:
        try:
            catalog_status = catalog_service.status()
        except Exception as exc:
            catalog_status = {'error': str(exc)}
    return jsonify({
        'status': 'healthy',
        'service': 'powerbi-documentation',
        # Prefer CI-provided build ID if present; fallback to short git hash placeholder
        'version': os.getenv('BUILD_ID', os.getenv('COMMIT_SHA', 'unknown')),
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'cache_fix': 'enabled',  # Indicates cache headers are active
        'catalog': catalog_status,
    }), 200


@app.route('/api/auth-status')
@login_required
def auth_status():
    """Test endpoint to check authentication status and token validity"""
    user_info = session.get('user', {})
    has_token = 'access_token' in session

    token_info = "No token"
    if has_token:
        token = session.get('access_token', '')
        token_info = f"Token exists (length: {len(token)})"

    return jsonify({
        'authenticated': True,
        'user_name': user_info.get('name', 'Unknown'),
        'user_email': user_info.get('preferred_username', 'Unknown'),
        'user_id': user_info.get('oid', 'Unknown'),
        'has_access_token': has_token,
        'token_info': token_info,
        'session_keys': list(session.keys())
    })


@app.route('/api/test-sso')
@login_required
@debug_only
def test_sso():
    """Test endpoint to verify if SSO-based workspace access is working"""
    import requests

    result = {
        'user_info': {
            'name': session.get('user', {}).get('name', 'Unknown'),
            'email': session.get('user', {}).get('preferred_username', 'Unknown'),
            'id': session.get('user', {}).get('oid', 'Unknown')
        },
        'has_session_token': 'access_token' in session,
        'test_results': {}
    }

    # Test 1: Try to get workspaces with user token
    try:
        headers = get_user_powerbi_headers()
        response = requests.get('https://api.powerbi.com/v1.0/myorg/groups', headers=headers)

        if response.status_code == 200:
            workspaces = response.json().get('value', [])
            result['test_results']['user_token'] = {
                'status': 'SUCCESS',
                'status_code': 200,
                'workspace_count': len(workspaces),
                'workspaces': [{'id': w['id'], 'name': w['name']} for w in workspaces[:10]]  # First 10
            }
        else:
            result['test_results']['user_token'] = {
                'status': 'FAILED',
                'status_code': response.status_code,
                'error': response.text[:500]
            }
    except Exception as e:
        result['test_results']['user_token'] = {
            'status': 'ERROR',
            'error': str(e)
        }

    # Test 2: Try to get workspaces with service principal (for comparison)
    try:
        sp_workspaces = powerbi.get_workspaces()
        result['test_results']['service_principal'] = {
            'status': 'SUCCESS',
            'workspace_count': len(sp_workspaces),
            'workspaces': [{'id': w['id'], 'name': w['name']} for w in sp_workspaces[:10]]  # First 10
        }
    except Exception as e:
        result['test_results']['service_principal'] = {
            'status': 'ERROR',
            'error': str(e)
        }

    # Determine which method is being used
    user_count = result['test_results'].get('user_token', {}).get('workspace_count', 0)
    sp_count = result['test_results'].get('service_principal', {}).get('workspace_count', 0)

    result['conclusion'] = {
        'sso_working': result['test_results'].get('user_token', {}).get('status') == 'SUCCESS',
        'user_workspace_count': user_count,
        'service_principal_workspace_count': sp_count,
        'recommendation': ''
    }

    if result['conclusion']['sso_working']:
        result['conclusion']['recommendation'] = '✅ SSO is working! User-delegated token successfully fetched workspaces.'
    else:
        result['conclusion']['recommendation'] = '❌ SSO is NOT working. User-delegated token failed. Check Azure AD permissions.'

    return jsonify(result)


@app.route('/')
@login_required
def index():
    """Home page - Dashboard Overview"""
    # Optional scroll-to-enter landing after SSO (user scrolls / clicks — no auto zoom)
    show_login_landing = bool(session.pop('show_login_landing', False))
    return render_template('home.html', show_login_landing=show_login_landing)


@app.route('/documentation')
@login_required
def documentation():
    """Documentation page - Report documentation generation"""
    can_archive = False
    try:
        from features.report_archive_service import user_can_archive
        email = session.get('user', {}).get('preferred_username') or ''
        can_archive = user_can_archive(email)
    except Exception:
        can_archive = False
    return render_template('index.html', can_archive_reports=can_archive)


@app.route('/semantic-models')
@login_required
def semantic_models_page():
    """Semantic Models page - Analyze and health-check semantic models"""
    return render_template('semantic_models.html')


@app.route('/impact')
@login_required
def impact_explorer_page():
    """
    Table impact explorer (EDW → report blast radius).
    Serves embedded Impact UI backed by precomputed catalog JSON.
    """
    return render_template('impact.html')


# =============================================================================
# CATALOG FAST PATH APIs (precomputed SharePoint/local metadata)
# Live Power BI APIs remain the fallback when catalog is unavailable.
# =============================================================================

def _user_allowed_workspace_ids():
    """
    Workspace IDs the current SSO user can access (from live /groups, cached).
    Returns None if undetermined (caller may skip filtering only for admin tools).
    Returns set() on hard failure after auth.
    """
    try:
        user_id = session.get('user', {}).get('oid', 'unknown')
        cache_key = f"workspaces_{user_id}"
        current_time = time.time()
        if cache_key in workspaces_cache and workspaces_cache[cache_key].get('data') and \
           (current_time - workspaces_cache[cache_key].get('timestamp', 0)) < CACHE_DURATION:
            return {w.get('id') for w in workspaces_cache[cache_key]['data'] if w.get('id')}

        # Lightweight live fetch (same as get_workspaces core)
        headers = get_user_powerbi_headers()
        resp = requests.get("https://api.powerbi.com/v1.0/myorg/groups", headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"⚠️ allowed workspaces fetch HTTP {resp.status_code}")
            return set()
        workspaces = resp.json().get('value', [])
        workspaces_cache[cache_key] = {'data': workspaces, 'timestamp': current_time}
        return {w.get('id') for w in workspaces if w.get('id')}
    except Exception as exc:
        print(f"⚠️ _user_allowed_workspace_ids: {exc}")
        return set()


@app.route('/api/catalog/status')
@login_required
def api_catalog_status():
    """Catalog availability, mode, freshness — for UI banners and ops."""
    if not CATALOG_AVAILABLE or catalog_service is None:
        return jsonify({
            'success': True,
            'enabled': False,
            'mode': 'off',
            'message': 'Catalog service not loaded; using live APIs only.',
        })
    try:
        st = catalog_service.status()
        st['success'] = True
        return jsonify(st)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _perform_catalog_refresh() -> dict:
    """
    Shared body for interactive and machine catalog refresh.
    Force re-download of catalog artifacts from SharePoint into server memory/disk mirror.
    Also clears per-user /api/reports response cache so Report Catalog picks up new ops.
    """
    if not CATALOG_AVAILABLE or catalog_service is None:
        return {'success': False, 'error': 'Catalog not available', '_http': 503}

    catalog_service.invalidate()
    # Prefer thin packs first (fast UI), then heavy files for report/model APIs
    home = catalog_service.get_json('ui_home_index.json', force_refresh=True)
    tables = catalog_service.get_json('ui_impact_tables.json', force_refresh=True)
    try:
        impact_reports = catalog_service.get_json('ui_impact_reports.json', force_refresh=True)
    except Exception:
        impact_reports = None
    summary = catalog_service.get_summary(force_refresh=True)
    cat = catalog_service.get_workspace_catalog(force_refresh=True)
    impact = catalog_service.get_impact_index(force_refresh=True)
    # Rebuild report→sources pack if SharePoint has old pack without it
    if impact and (not impact_reports or not isinstance((impact_reports or {}).get('rows'), list)):
        try:
            catalog_service._ensure_thin_impact_pack(impact)
            impact_reports = catalog_service.get_json('ui_impact_reports.json')
        except Exception as exc:
            print(f"⚠️ thin impact reports pack rebuild: {exc}")
    # Ops snapshot used for last refresh / views — reload so Catalog is not stuck on old ops
    try:
        catalog_service.get_json('refresh_snapshot.json', force_refresh=True)
    except Exception:
        pass
    try:
        catalog_service.get_json('usage_snapshot.json', force_refresh=True)
    except Exception:
        pass

    # Ensure Home KPI detail lists + report directory exist (older SP packs may lack them)
    home_has_details = isinstance(home, dict) and isinstance(home.get('detailLists'), dict)
    report_dir = None
    try:
        report_dir = catalog_service.get_json('ui_report_directory.json')
    except Exception:
        report_dir = None
    need_home_rebuild = cat and (
        not home_has_details
        or not report_dir
        or not isinstance((report_dir or {}).get('rows'), list)
    )
    if need_home_rebuild:
        try:
            catalog_service._ensure_thin_home_pack(cat)
            home = catalog_service.get_json('ui_home_index.json')
            home_has_details = isinstance(home, dict) and isinstance(home.get('detailLists'), dict)
            report_dir = catalog_service.get_json('ui_report_directory.json')
        except Exception as exc:
            print(f"⚠️ thin home/report-directory pack rebuild after refresh: {exc}")

    # Drop in-process /api/reports shells + folder tree so next load is fresh
    cleared_reports = 0
    try:
        global reports_cache, workspace_folders_cache
        cleared_reports = len(reports_cache)
        reports_cache = {}
        workspace_folders_cache = {}
    except Exception:
        pass

    ops_at = None
    if isinstance(cat, dict):
        ops_at = cat.get('opsEnrichedAt') or cat.get('generatedAt')
    return {
        'success': True,
        'ui_home_index': bool(home),
        'ui_home_detailLists': home_has_details,
        'ui_report_directory': bool(report_dir and (report_dir.get('rows') is not None)),
        'ui_impact_tables': bool(tables),
        'ui_impact_reports': bool(impact_reports),
        'workspace_catalog': bool(cat),
        'impact_index': bool(impact),
        'summary': bool(summary),
        'opsEnrichedAt': ops_at,
        'generatedAt': (cat or {}).get('generatedAt') if isinstance(cat, dict) else None,
        'clearedReportsCacheEntries': cleared_reports,
        'status': catalog_service.status(),
    }


@app.route('/api/catalog/refresh', methods=['POST'])
@login_required
def api_catalog_refresh():
    """
    Force re-download of catalog artifacts from SharePoint into server memory/disk mirror.
    Also clears per-user /api/reports response cache so Report Catalog picks up new ops.
    """
    try:
        body = _perform_catalog_refresh()
        code = int(body.pop('_http', 200)) if isinstance(body, dict) else 200
        return jsonify(body), code
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/catalog/refresh-internal', methods=['POST'])
def api_catalog_refresh_internal():
    """
    Machine-callable catalog refresh after ops/fresh SharePoint publish.
    Auth: header X-Catalog-Refresh-Key (or Authorization: Bearer) must match
    App Setting CATALOG_REFRESH_SECRET. No user SSO session required.
    """
    secret = (os.getenv('CATALOG_REFRESH_SECRET') or '').strip()
    if not secret:
        return jsonify({
            'success': False,
            'error': 'CATALOG_REFRESH_SECRET is not configured on the app',
        }), 503

    provided = (
        (request.headers.get('X-Catalog-Refresh-Key') or '').strip()
        or (request.headers.get('X-Api-Key') or '').strip()
    )
    auth_h = (request.headers.get('Authorization') or '').strip()
    if auth_h.lower().startswith('bearer '):
        provided = provided or auth_h[7:].strip()
    if not provided or provided != secret:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        body = _perform_catalog_refresh()
        code = int(body.pop('_http', 200)) if isinstance(body, dict) else 200
        body['triggeredBy'] = 'internal'
        print(f"🔄 catalog refresh-internal OK opsEnrichedAt={body.get('opsEnrichedAt')}")
        return jsonify(body), code
    except Exception as e:
        print(f"❌ catalog refresh-internal failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/catalog/data/<path:name>')
@login_required
def api_catalog_data(name):
    """
    Serve *small* catalog JSON only (summary, ops_summary, ui packs).

    Large files (workspace_catalog, impact_index) are server-side only —
    use /api/catalog/impact/* and workspace report APIs instead.
    Browser never downloads SharePoint blobs.
    """
    if not CATALOG_AVAILABLE or catalog_service is None:
        return jsonify({'error': 'Catalog not available'}), 503
    safe = os.path.basename(name)
    if not safe.endswith('.json') or '..' in safe:
        return jsonify({'error': 'Only .json filenames allowed'}), 400
    # Hard block large artifacts from leaving the server
    try:
        from catalog_service import catalog_config as _ccfg
        blocked = getattr(_ccfg, 'BROWSER_BLOCKED_CATALOG_FILES', set())
        allowed = getattr(_ccfg, 'BROWSER_ALLOWED_CATALOG_FILES', None)
    except Exception:
        blocked = {
            'workspace_catalog.json', 'impact_index.json',
            'inventory.json', 'refresh_snapshot.json',
        }
        allowed = None
    if safe in blocked or (allowed is not None and safe not in allowed):
        return jsonify({
            'error': f'{safe} is server-side only and cannot be downloaded by the browser',
            'hint': (
                'Use thin APIs: /api/home-summary, /api/catalog/impact/tables, '
                '/api/catalog/impact/lookup, /api/catalog/impact/table?key=..., '
                '/api/reports?workspace_id=...'
            ),
        }), 403
    force = request.args.get('refresh') in ('1', 'true', 'yes')
    try:
        data = catalog_service.get_json(safe, force_refresh=force)
        if data is None:
            return jsonify({
                'error': f'File not available: {safe}',
                'hint': 'Run: python run_catalog_extract.py --fresh -v  (publishes to SharePoint latest/)',
                'status': catalog_service.status(),
            }), 404
        resp = jsonify(data)
        resp.headers['X-Data-Source'] = 'server-cache'
        return resp
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/catalog/impact/tables')
@login_required
def api_catalog_impact_tables():
    """
    Thin table list for Impact Explorer (no nested datasets).
    Strips bulky searchText field — browser builds search locally.
    Enables short browser/proxy cache so revisits are near-instant.
    """
    if not CATALOG_AVAILABLE or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    try:
        force = request.args.get('refresh') in ('1', 'true', 'yes')
        rows_in = catalog_service.impact_table_rows(force_refresh=force)
        # Compact rows for wire size (searchText alone is multi-MB)
        rows = []
        for r in rows_in or []:
            rows.append({
                'k': r.get('tableKey'),
                't': r.get('table'),
                'st': r.get('sourceType') or 'Unknown',
                'sv': r.get('server') or '',
                'db': r.get('database') or '',
                'sc': r.get('schema') or '',
                'mn': r.get('modelTableNames') or [],
                'rc': int(r.get('reportCount') or 0),
                'dc': int(r.get('datasetCount') or 0),
                'wc': int(r.get('workspaceCount') or 0),
            })
        summary = catalog_service.get_summary() or {}
        pack = catalog_service.get_json('ui_impact_tables.json') or {}
        payload = {
            'success': True,
            'v': 2,  # compact schema version
            'count': len(rows),
            'rows': rows,
            'generatedAt': pack.get('generatedAt') or summary.get('generatedAt'),
            'stats': summary.get('stats') or {},
            'source': 'server-thin',
        }
        resp = jsonify(payload)
        # Same user/session: allow brief cache so switching back to Impact is fast
        if not force:
            resp.headers['Cache-Control'] = 'private, max-age=120'
        else:
            resp.headers['Cache-Control'] = 'no-store'
        resp.headers['X-Data-Source'] = 'server-thin'
        return resp
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/catalog/impact/table')
@login_required
def api_catalog_impact_table_detail():
    """One table's full impact (drawer) — server reads index, browser gets one object."""
    if not CATALOG_AVAILABLE or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    key = (request.args.get('key') or request.args.get('table') or '').strip()
    if not key:
        return jsonify({'success': False, 'error': 'key (tableKey) required'}), 400
    try:
        entry = catalog_service.impact_table_detail(key)
        if not entry:
            return jsonify({'success': False, 'error': f'No impact entry for {key}'}), 404

        # Snapshot tenant-wide summary before ACL (grid uses this; drawer may be smaller)
        entry = dict(entry)
        tenant_summary = dict(entry.get('impactSummary') or {})
        entry['tenantImpactSummary'] = tenant_summary

        allowed = _user_allowed_workspace_ids()
        acl_applied = False
        if allowed is not None and len(allowed) > 0:
            acl_applied = True
            filtered_datasets = []
            for d in entry.get('datasets') or []:
                if not isinstance(d, dict):
                    continue
                d2 = dict(d)
                # Keep reports in workspaces the user can open
                reps = []
                for r in d2.get('reports') or []:
                    if not isinstance(r, dict):
                        continue
                    rid_ws = r.get('workspaceId') or d2.get('workspaceId') or ''
                    if rid_ws in allowed:
                        reps.append(r)
                d2['reports'] = reps
                # Keep dataset if its home workspace is allowed OR any remaining report is
                ds_ws = d2.get('workspaceId') or ''
                if ds_ws in allowed or reps:
                    # If dataset home is outside ACL but reports inside, still show
                    filtered_datasets.append(d2)
            entry['datasets'] = filtered_datasets

            report_ids = set()
            workspace_ids = set()
            for d in filtered_datasets:
                if d.get('workspaceId'):
                    workspace_ids.add(d['workspaceId'])
                for r in d.get('reports') or []:
                    if r.get('reportId'):
                        report_ids.add(r['reportId'])
                    if r.get('workspaceId'):
                        workspace_ids.add(r['workspaceId'])
            entry['impactSummary'] = {
                **tenant_summary,
                'datasetCount': len(filtered_datasets),
                'reportCount': len(report_ids),
                'workspaceCount': len(workspace_ids),
            }

        entry['aclApplied'] = acl_applied
        return jsonify({'success': True, 'table': entry})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/catalog/reports/search')
@login_required
def api_catalog_reports_search():
    """
    Reverse search: report name → workspace(s).
    Thin catalog directory (not full workspace_catalog in browser).
    ?q=wanek&limit=40
    """
    if not CATALOG_AVAILABLE or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    try:
        q = (request.args.get('q') or request.args.get('query') or '').strip()
        try:
            limit = int(request.args.get('limit') or 50)
        except Exception:
            limit = 50
        allowed = _user_allowed_workspace_ids()
        result = catalog_service.search_reports(
            query=q,
            allowed_workspace_ids=allowed if allowed is not None else None,
            limit=limit,
        )
        # Compact wire format
        rows = []
        for r in result.get('rows') or []:
            rows.append({
                'id': r.get('reportId'),
                'n': r.get('reportName') or '',
                'wid': r.get('workspaceId') or '',
                'wn': r.get('workspaceName') or '',
                'did': r.get('datasetId') or '',
            })
        payload = {
            'success': True,
            'query': result.get('query') or q,
            'count': len(rows),
            'total': result.get('total'),
            'capped': bool(result.get('capped')),
            'rows': rows,
            'source': 'ui_report_directory',
        }
        resp = jsonify(payload)
        resp.headers['Cache-Control'] = 'private, max-age=60'
        return resp
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/catalog/impact/reports')
@login_required
def api_catalog_impact_reports():
    """
    Thin report list for Impact Explorer «Report sources» tab (report → source counts).
    No nested source lists on the wire — use /impact/report for drawer detail.
    """
    if not CATALOG_AVAILABLE or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    try:
        force = request.args.get('refresh') in ('1', 'true', 'yes')
        allowed = _user_allowed_workspace_ids()
        rows_in = catalog_service.impact_report_rows(
            force_refresh=force,
            allowed_workspace_ids=allowed if allowed is not None else None,
        )
        rows = []
        for r in rows_in or []:
            rows.append({
                'id': r.get('reportId'),
                'n': r.get('reportName') or '',
                'wid': r.get('workspaceId') or '',
                'wn': r.get('workspaceName') or '',
                'rt': r.get('reportType') or '',
                'tc': int(r.get('tableCount') or 0),
                'dc': int(r.get('datasetCount') or 0),
                'st': r.get('sourceTypes') or [],
            })
        pack = catalog_service.get_json('ui_impact_reports.json') or {}
        payload = {
            'success': True,
            'v': 1,
            'count': len(rows),
            'rows': rows,
            'generatedAt': pack.get('generatedAt'),
            'source': 'server-thin',
        }
        resp = jsonify(payload)
        if not force:
            resp.headers['Cache-Control'] = 'private, max-age=120'
        else:
            resp.headers['Cache-Control'] = 'no-store'
        resp.headers['X-Data-Source'] = 'server-thin'
        return resp
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/catalog/impact/report')
@login_required
def api_catalog_impact_report_detail():
    """
    All sources for one report (SQL / Excel / SharePoint / model tables / …).
    Drawer payload for Report sources tab.
    """
    if not CATALOG_AVAILABLE or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    report_id = (request.args.get('report_id') or request.args.get('id') or '').strip()
    if not report_id:
        return jsonify({'success': False, 'error': 'report_id required'}), 400
    try:
        allowed = _user_allowed_workspace_ids()
        detail = catalog_service.impact_report_detail(
            report_id,
            allowed_workspace_ids=allowed if allowed is not None else None,
        )
        if not detail:
            return jsonify({'success': False, 'error': f'No sources for report {report_id}'}), 404
        return jsonify({'success': True, 'report': detail})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/catalog/impact/fields')
@login_required
def api_catalog_impact_fields():
    """
    Thin field/metric usage list for Impact Explorer «Field usage» tab.
    Built weekly (--fresh) — see catalog_service/field_usage_index.py.
    """
    if not CATALOG_AVAILABLE or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    try:
        force = request.args.get('refresh') in ('1', 'true', 'yes')
        allowed = _user_allowed_workspace_ids()
        data = catalog_service.field_usage_rows(
            force_refresh=force,
            allowed_workspace_ids=allowed if allowed is not None else None,
        )
        payload = {
            'success': True,
            'v': 1,
            'count': len(data.get('rows') or []),
            'rows': data.get('rows') or [],
            'generatedAt': data.get('generatedAt'),
            'reportsProcessed': data.get('reportsProcessed'),
            'reportsSkippedUnchanged': data.get('reportsSkippedUnchanged'),
            'reportsFailed': data.get('reportsFailed'),
            'source': 'server-thin',
        }
        resp = jsonify(payload)
        resp.headers['Cache-Control'] = 'no-store' if force else 'private, max-age=120'
        resp.headers['X-Data-Source'] = 'server-thin'
        return resp
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/catalog/impact/field-detail')
@login_required
def api_catalog_impact_field_detail():
    """All reports using one field/metric (drawer) for Field usage tab."""
    if not CATALOG_AVAILABLE or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    field_key = (request.args.get('field_key') or request.args.get('key') or '').strip()
    if not field_key:
        return jsonify({'success': False, 'error': 'field_key required'}), 400
    try:
        allowed = _user_allowed_workspace_ids()
        detail = catalog_service.field_usage_detail(
            field_key,
            allowed_workspace_ids=allowed if allowed is not None else None,
        )
        if not detail:
            return jsonify({'success': False, 'error': f'No usage found for field {field_key}'}), 404
        return jsonify({'success': True, 'field': detail})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/catalog/impact/lookup')
@login_required
def api_catalog_impact_lookup():
    """Lookup table → datasets → reports blast radius from impact index (server-side)."""
    if not CATALOG_AVAILABLE or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    table = (request.args.get('table') or '').strip()
    if not table:
        return jsonify({'success': False, 'error': 'table query param required'}), 400
    try:
        hits = catalog_service.lookup_table(table)
        allowed = _user_allowed_workspace_ids()
        if allowed is not None and len(allowed) > 0:
            filtered = []
            for entry in hits:
                datasets = [d for d in (entry.get('datasets') or []) if d.get('workspaceId') in allowed]
                if datasets:
                    e2 = dict(entry)
                    e2['datasets'] = datasets
                    filtered.append(e2)
            hits = filtered
        return jsonify({'success': True, 'table': table, 'count': len(hits), 'results': hits})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/catalog/impact/model-details')
@login_required
def api_catalog_impact_model_details():
    """
    Thin semantic-model payload for Impact Explorer popup.
    Catalog-only (no full workspace_catalog in browser). Optional focus_table
    highlights the impact table the user came from.
    """
    if not CATALOG_AVAILABLE or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    dataset_id = (request.args.get('dataset_id') or '').strip()
    workspace_id = (request.args.get('workspace_id') or '').strip()
    focus_table = (request.args.get('focus_table') or request.args.get('table') or '').strip()
    model_table = (request.args.get('model_table') or '').strip()
    report_name = (request.args.get('report_name') or '').strip()
    report_id = (request.args.get('report_id') or '').strip()
    if not dataset_id:
        return jsonify({'success': False, 'error': 'dataset_id required'}), 400
    try:
        allowed = _user_allowed_workspace_ids()
        if allowed is not None and len(allowed) > 0 and workspace_id and workspace_id not in allowed:
            return jsonify({'success': False, 'error': 'Access denied for workspace'}), 403

        details = catalog_service.impact_model_details(
            dataset_id=dataset_id,
            workspace_id=workspace_id,
            focus_table=focus_table,
            model_table_name=model_table,
        )
        if not details:
            return jsonify({
                'success': False,
                'error': 'Dataset not found in catalog. Run a fresh extract if this model is new.',
            }), 404

        # ACL: if no workspace_id was passed, still enforce when we resolved one
        ws_resolved = details.get('workspaceId') or workspace_id
        if allowed is not None and len(allowed) > 0 and ws_resolved and ws_resolved not in allowed:
            return jsonify({'success': False, 'error': 'Access denied for workspace'}), 403

        payload = {
            'success': True,
            'source': 'catalog',
            'reportName': report_name or None,
            'reportId': report_id or None,
            **details,
        }
        resp = jsonify(payload)
        resp.headers['Cache-Control'] = 'private, max-age=60'
        resp.headers['X-Data-Source'] = 'catalog-impact-model'
        return resp
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/home-summary')
@login_required
def api_home_summary():
    """
    Fast Home dashboard: one response for all accessible workspaces.
    Prefers precomputed SharePoint/local catalog (report/inactive/orphaned counts).
    Falls back to workspace list only if catalog unavailable (UI may still use live paths).
    """
    try:
        allowed = _user_allowed_workspace_ids()
        # allowed may be empty set on failure — still try catalog unfiltered only if None
        if CATALOG_AVAILABLE and catalog_service is not None and catalog_service.is_available():
            summary = catalog_service.build_home_summary(
                allowed_workspace_ids=allowed if allowed is not None else None,
                inactive_days=30,
            )
            if summary:
                print(
                    f"⚡ HOME SUMMARY from catalog: ws={summary.get('workspaceCount')} "
                    f"reports={summary.get('totalReports')} "
                    f"inactive={summary.get('inactiveReports')} "
                    f"orphaned={summary.get('orphanedReports')} "
                    f"zeroViews={summary.get('zeroViewsReports')} "
                    f"opsEnrichedAt={summary.get('opsEnrichedAt')}"
                )
                return jsonify(summary)

        # Catalog miss — return minimal payload so UI can fall back
        return jsonify({
            'success': True,
            'source': 'none',
            'fallback': True,
            'message': 'Catalog not available; use live home loaders',
            'workspaceCount': len(allowed or []),
            'totalReports': None,
            'inactiveReports': None,
            'orphanedReports': None,
            'zeroViewsReports': None,
            'workspaces': [],
        })
    except Exception as e:
        print(f"❌ home-summary error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/home-summary/details')
@login_required
def api_home_summary_details():
    """
    Row lists for Home KPI tabs.
    ?metric=workspaces|reports|inactive|orphaned|zero_views
    """
    try:
        metric = (request.args.get('metric') or 'workspaces').strip().lower()
        try:
            limit = int(request.args.get('limit') or 5000)
        except Exception:
            limit = 5000
        limit = max(1, min(limit, 20000))
        allowed = _user_allowed_workspace_ids()
        if not CATALOG_AVAILABLE or catalog_service is None or not catalog_service.is_available():
            return jsonify({
                'success': False,
                'error': 'Catalog not available',
                'metric': metric,
                'rows': [],
            }), 503
        details = catalog_service.build_home_details(
            metric=metric,
            allowed_workspace_ids=allowed if allowed is not None else None,
            inactive_days=30,
            limit=limit,
        )
        if not details:
            return jsonify({'success': False, 'error': 'No details', 'metric': metric, 'rows': []}), 404
        if details.get('success') is False:
            return jsonify(details), 400
        return jsonify(details)
    except Exception as e:
        print(f"❌ home-summary/details error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/catalog/impact/top')
@login_required
def api_catalog_impact_top():
    """Top blast-radius tables from precomputed impact index."""
    if not CATALOG_AVAILABLE or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    try:
        n = min(int(request.args.get('n', 50)), 500)
        rows = catalog_service.impact_top(n=n)
        return jsonify({'success': True, 'count': len(rows), 'tables': rows,
                        'generatedAt': (catalog_service.get_impact_index() or {}).get('generatedAt')})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _resolve_person_identity(*values):
    """
    Normalize creator/modifier identity from Power BI API variants.

    Handles:
      - plain UPN/email/display strings (users AND team/DL mailboxes)
      - nested user objects (userPrincipalName, emailAddress, displayName, …)
      - skips empty / Unknown / bare GUIDs (keeps GUID only as last resort)

    Returns '' when nothing usable is found (UI maps that to N/A).
    """
    def _looks_like_guid(s: str) -> bool:
        s = (s or '').strip()
        if len(s) != 36 or s.count('-') != 4:
            return False
        hex_parts = s.replace('-', '')
        return len(hex_parts) == 32 and all(c in '0123456789abcdefABCDEF' for c in hex_parts)

    def _from_dict(d):
        if not isinstance(d, dict):
            return ''
        # Nested shapes sometimes appear from admin / Graph-ish payloads
        for nest_key in ('user', 'principal', 'identity', 'account'):
            nested = d.get(nest_key)
            if isinstance(nested, dict):
                got = _from_dict(nested)
                if got:
                    return got
        for key in (
            'userPrincipalName', 'emailAddress', 'email', 'mail',
            'principalName', 'upn', 'displayName', 'name',
            'identifier', 'id', 'objectId',
        ):
            raw = d.get(key)
            if raw is None:
                continue
            s = str(raw).strip()
            if not s:
                continue
            if s.lower() in {'unknown', 'n/a', 'none', 'null', '-', '—', 'undefined'}:
                continue
            if _looks_like_guid(s) and '@' not in s:
                continue  # prefer a human label later; GUID last-resort below
            return s
        # Last resort: any non-empty GUID id so row isn't blank
        for key in ('id', 'objectId', 'identifier'):
            raw = d.get(key)
            if raw and _looks_like_guid(str(raw)):
                return str(raw).strip()
        return ''

    guid_fallback = ''
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            got = _from_dict(value)
            if got:
                return got
            continue
        s = str(value).strip()
        if not s:
            continue
        if s.lower() in {'unknown', 'n/a', 'none', 'null', '-', '—', 'undefined'}:
            continue
        if _looks_like_guid(s) and '@' not in s:
            if not guid_fallback:
                guid_fallback = s
            continue
        return s
    return guid_fallback


def _pick_person(*values):
    """First non-empty resolved person/team identity."""
    for v in values:
        got = _resolve_person_identity(v)
        if got:
            return got
    return ''


def _pick_datetime(*values):
    """First non-empty datetime-like string from API variants."""
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in {'unknown', 'n/a', 'none', 'null', '-', '—'}:
            return s
    return ''


@app.route('/api/reports-metadata/<workspace_id>')
@login_required
def get_reports_metadata(workspace_id):
    """
    Creator / modifier / dates for reports in a workspace.

    Multi-source (does not break existing UI contract):
      1) Admin Scanner (createdBy / modifiedBy when tenant returns them)
      2) Groups REST /reports overlay — often has people + dates when Scanner
         omits team mailbox / DL / group principals
      3) Catalog fill for remaining gaps (after extract preserves owner fields)

    Response shape unchanged:
      { success, workspace_id, reports: [{ report_id, report_name, created_by,
        created_date_time, modified_by, modified_date_time, created_by_id,
        modified_by_id }], cached }
    Empty identity fields are '' (UI still shows N/A).
    """
    try:
        from scanner_connector import PowerBIScanner
        import time

        # Check cache first (5-minute TTL)
        cache_key = f"metadata_{workspace_id}"
        if cache_key in reports_cache:
            cached_data, timestamp = reports_cache[cache_key]
            if time.time() - timestamp < 300:  # 5 minutes
                print(f"📦 Using cached metadata for workspace {workspace_id}")
                return jsonify({
                    'success': True,
                    'workspace_id': workspace_id,
                    'reports': cached_data,
                    'cached': True
                })

        t0 = time.time()
        print(f"\n🔍 Fetching report metadata (REST → catalog → optional scanner) for workspace: {workspace_id}")
        by_id = {}

        def _ensure_row(rid, name=''):
            row = by_id.get(rid)
            if not row:
                row = {
                    'report_id': rid,
                    'report_name': name or '',
                    'created_by': '',
                    'created_date_time': '',
                    'modified_by': '',
                    'modified_date_time': '',
                    'created_by_id': '',
                    'modified_by_id': '',
                }
                by_id[rid] = row
            elif name and not row.get('report_name'):
                row['report_name'] = name
            return row

        def _merge_people(row, created_vals, modified_vals, created_dt_vals, modified_dt_vals,
                          created_id_vals=(), modified_id_vals=()):
            # Prefer first usable value already on row, then new sources
            row['created_by'] = _pick_person(row.get('created_by'), *created_vals)
            row['modified_by'] = _pick_person(row.get('modified_by'), *modified_vals)
            row['created_date_time'] = _pick_datetime(row.get('created_date_time'), *created_dt_vals)
            row['modified_date_time'] = _pick_datetime(row.get('modified_date_time'), *modified_dt_vals)
            if not row.get('created_by_id'):
                for v in created_id_vals:
                    if v:
                        row['created_by_id'] = str(v)
                        break
            if not row.get('modified_by_id'):
                for v in modified_id_vals:
                    if v:
                        row['modified_by_id'] = str(v)
                        break

        def _people_coverage():
            if not by_id:
                return 0.0
            hit = sum(
                1 for r in by_id.values()
                if r.get('created_by') or r.get('modified_by') or r.get('modified_date_time')
            )
            return hit / max(1, len(by_id))

        # ---- 1) Groups REST FIRST (fast; usually has createdBy/modifiedBy + dates) ----
        # Old order ran Admin Scanner first → multi-minute silent hang while UI spinners spin.
        rest_count = 0
        rest_filled = 0
        try:
            print(f"   🌐 REST /groups/.../reports starting… t+{time.time()-t0:.1f}s")
            headers = get_user_powerbi_headers()
            url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports"
            resp = requests.get(url, headers=headers, timeout=45)
            if resp.status_code == 200:
                for report in resp.json().get('value') or []:
                    rid = report.get('id')
                    if not rid:
                        continue
                    name = report.get('name') or ''
                    if _is_excluded_report_name(name):
                        continue
                    row = _ensure_row(rid, name)
                    before_c, before_m = row.get('created_by'), row.get('modified_by')
                    _merge_people(
                        row,
                        created_vals=(
                            report.get('createdBy'),
                            report.get('createdByUser'),
                            report.get('createdByUserPrincipalName'),
                        ),
                        modified_vals=(
                            report.get('modifiedBy'),
                            report.get('modifiedByUser'),
                            report.get('modifiedByUserPrincipalName'),
                        ),
                        created_dt_vals=(
                            report.get('createdDateTime'),
                            report.get('createdDate'),
                        ),
                        modified_dt_vals=(
                            report.get('modifiedDateTime'),
                            report.get('modifiedDate'),
                        ),
                        created_id_vals=(report.get('createdById'),),
                        modified_id_vals=(report.get('modifiedById'),),
                    )
                    rest_count += 1
                    if (row.get('created_by') and not before_c) or (row.get('modified_by') and not before_m):
                        rest_filled += 1
                print(
                    f"   🌐 REST done rows={rest_count} people_filled={rest_filled} "
                    f"coverage={_people_coverage():.0%} t+{time.time()-t0:.1f}s"
                )
            else:
                print(f"   ⚠️ REST reports HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"   ⚠️ REST metadata overlay failed: {e}")

        # ---- 2) Catalog fill (fast when SharePoint/cache warm) ----
        catalog_filled = 0
        if CATALOG_AVAILABLE and catalog_service is not None:
            try:
                print(f"   📦 Catalog gap-fill starting… t+{time.time()-t0:.1f}s")
                pack = catalog_service.get_workspace_reports(workspace_id)
                if pack:
                    for report in pack.get('reports') or []:
                        rid = report.get('id')
                        if not rid:
                            continue
                        row = by_id.get(rid) or _ensure_row(rid, report.get('name') or '')
                        before = (
                            row.get('created_by'), row.get('modified_by'),
                            row.get('created_date_time'), row.get('modified_date_time'),
                        )
                        _merge_people(
                            row,
                            created_vals=(report.get('createdBy'), report.get('created_by')),
                            modified_vals=(report.get('modifiedBy'), report.get('modified_by')),
                            created_dt_vals=(
                                report.get('createdDateTime'),
                                report.get('created_date_time'),
                                report.get('created_date'),
                            ),
                            modified_dt_vals=(
                                report.get('modifiedDateTime'),
                                report.get('modified_date_time'),
                                report.get('modified_date'),
                            ),
                            created_id_vals=(report.get('createdById'), report.get('created_by_id')),
                            modified_id_vals=(report.get('modifiedById'), report.get('modified_by_id')),
                        )
                        after = (
                            row.get('created_by'), row.get('modified_by'),
                            row.get('created_date_time'), row.get('modified_date_time'),
                        )
                        if after != before and any(after):
                            catalog_filled += 1
                print(
                    f"   📦 Catalog fill updates={catalog_filled} "
                    f"coverage={_people_coverage():.0%} t+{time.time()-t0:.1f}s"
                )
            except Exception as e:
                print(f"   ⚠️ Catalog metadata fill failed: {e}")

        # ---- 3) Scanner ONLY if coverage still poor (slow Admin scan — was causing 3–4 min UI hang) ----
        # Default skip when REST already covered most rows. Force with ?force_scanner=1
        force_scanner = str(request.args.get('force_scanner', '')).lower() in ('1', 'true', 'yes')
        coverage = _people_coverage()
        scanner_count = 0
        need_scanner = force_scanner or (not by_id) or (coverage < 0.35 and rest_count == 0)
        if need_scanner:
            try:
                print(
                    f"   🛰️ Scanner fallback (coverage={coverage:.0%} force={force_scanner}) "
                    f"t+{time.time()-t0:.1f}s — may take a while…"
                )
                scanner = PowerBIScanner()
                scan_result = scanner.run_scan(workspace_id=workspace_id) or {}
                for workspace in scan_result.get('workspaces') or []:
                    if workspace.get('id') != workspace_id:
                        continue
                    for report in workspace.get('reports') or []:
                        rid = report.get('id')
                        if not rid:
                            continue
                        row = _ensure_row(rid, report.get('name') or '')
                        _merge_people(
                            row,
                            created_vals=(
                                report.get('createdBy'),
                                report.get('createdByUser'),
                                report.get('createdByUserPrincipalName'),
                            ),
                            modified_vals=(
                                report.get('modifiedBy'),
                                report.get('modifiedByUser'),
                                report.get('modifiedByUserPrincipalName'),
                            ),
                            created_dt_vals=(
                                report.get('createdDateTime'),
                                report.get('createdDate'),
                            ),
                            modified_dt_vals=(
                                report.get('modifiedDateTime'),
                                report.get('modifiedDate'),
                            ),
                            created_id_vals=(report.get('createdById'),),
                            modified_id_vals=(report.get('modifiedById'),),
                        )
                        scanner_count += 1
                    break
                print(f"   🛰️ Scanner rows={scanner_count} t+{time.time()-t0:.1f}s")
            except Exception as e:
                print(f"   ⚠️ Scanner metadata failed: {e}")
        else:
            print(
                f"   ⏭️ Skipping Scanner (coverage={coverage:.0%} rest_rows={rest_count}) "
                f"— UI stays fast; pass force_scanner=1 if needed"
            )

        print(f"   ⏱️ metadata sources done in {time.time()-t0:.1f}s rows={len(by_id)}")

        if not by_id:
            return jsonify({
                'success': False,
                'error': 'No report metadata from Scanner, REST, or catalog'
            }), 500

        # Stable list; keep empty string for missing people (UI → N/A)
        reports_metadata = sorted(
            by_id.values(),
            key=lambda r: (r.get('report_name') or '').lower(),
        )
        with_people = sum(
            1 for r in reports_metadata
            if r.get('created_by') or r.get('modified_by')
        )
        print(
            f"✅ Metadata for {len(reports_metadata)} reports "
            f"({with_people} with created/modified identity)"
        )

        # Cache the results
        reports_cache[cache_key] = (reports_metadata, time.time())

        return jsonify({
            'success': True,
            'workspace_id': workspace_id,
            'reports': reports_metadata,
            'cached': False
        })

    except Exception as e:
        print(f"❌ Error fetching report metadata: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/lineage')
@login_required
def lineage():
    """Report Lineage page - View dataset lineage for reports"""
    return render_template('lineage.html')


@app.route('/api/dataset/lineage')
@login_required
def get_dataset_lineage():
    """
    API endpoint to get semantic model lineage

    Query Parameters:
        workspace_id: Power BI workspace GUID
        dataset_id: Dataset GUID

    Returns:
        JSON with dataset lineage including tables, M expressions, DAX measures
    """
    try:
        from features.semantic_model_lineage import SemanticModelLineage
        from scanner_connector import PowerBIScanner

        workspace_id = request.args.get('workspace_id')
        dataset_id = request.args.get('dataset_id')

        if not workspace_id or not dataset_id:
            return jsonify({
                'success': False,
                'error': 'workspace_id and dataset_id are required'
            }), 400

        # Get user token from session
        user_token = session.get('access_token')
        if not user_token:
            return jsonify({
                'success': False,
                'error': 'Not authenticated'
            }), 401

        # Initialize Scanner API with service principal token
        scanner_service = PowerBIScanner()
        scanner_service.access_token = scanner_service.get_access_token()

        # Create analyzer and get lineage
        analyzer = SemanticModelLineage(scanner_service)
        result = analyzer.get_dataset_lineage(workspace_id, dataset_id)

        return jsonify(result)

    except Exception as e:
        print(f"❌ Error in dataset lineage: {str(e)}")
        import traceback
        traceback.print_exc()

        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/orphaned-reports')
@login_required
def orphaned_reports_page():
    """Unowned Reports page — reports with no active owner signal."""
    return render_template('orphaned_reports.html')


# Decommissioned Reports routes/helpers moved to routes/decommission.py (blueprint).
# See decommission_bp registration below (after login_required / get_user_powerbi_token
# / _jwt_seconds_left are defined).

# Similarity Analysis routes/helpers moved to routes/similarity.py (blueprint).
# See similarity_bp registration below (after login_required is defined).

@app.route('/api/workspace-summary/<workspace_id>')
@login_required
def get_workspace_summary(workspace_id):
    """
    Get workspace summary including total reports and inactive reports count
    Inactive reports = reports with datasets not refreshed in 30+ days
    Results are cached for 15 minutes per workspace per user
    """
    from datetime import datetime, timezone, timedelta
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        # Get user info for cache key
        user_id = session.get('user', {}).get('oid', 'unknown')
        cache_key = f"workspace_summary_{workspace_id}_{user_id}"

        # Check cache first (15 minute cache for better performance)
        current_time = time.time()
        SUMMARY_CACHE_DURATION = 900  # 15 minutes
        if cache_key in workspace_cache:
            cache_entry = workspace_cache[cache_key]
            if (current_time - cache_entry['timestamp']) < SUMMARY_CACHE_DURATION:
                print(f"✅ Cache hit for workspace summary: {workspace_id}")
                return jsonify(cache_entry['data'])

        print(f"📊 Fetching workspace summary for: {workspace_id}")

        # Get all reports in workspace (fast API call)
        base_url = "https://api.powerbi.com/v1.0/myorg"
        reports_url = f"{base_url}/groups/{workspace_id}/reports"

        headers = get_user_powerbi_headers()
        reports_response = requests.get(reports_url, headers=headers)
        reports_response.raise_for_status()

        reports = reports_response.json().get('value', [])
        total_reports = len(reports)

        print(f"   Found {total_reports} reports")

        # Group reports by dataset ID
        dataset_to_reports = {}
        for report in reports:
            dataset_id = report.get('datasetId')
            if dataset_id:
                if dataset_id not in dataset_to_reports:
                    dataset_to_reports[dataset_id] = []
                dataset_to_reports[dataset_id].append(report['id'])

        print(f"   Found {len(dataset_to_reports)} unique datasets")

        # Create a session for connection pooling (reuse TCP connections)
        session_obj = requests.Session()
        session_obj.headers.update(headers)

        # Function to check if a dataset is inactive
        def check_dataset_inactive(dataset_id):
            """
            Check if dataset is inactive based on:
            - ONLY datasets with >30 days since last successful refresh

            Returns False (not inactive) for:
            - Datasets with no refresh history (likely DirectQuery/Live)
            - Datasets refreshed within 30 days
            - Live connection datasets (API 415)
            - API errors (403, 404, etc.)
            """
            try:
                from powerbi_connector import pick_best_refresh_from_history

                # Pull a few history rows so in-progress (null endTime) doesn't hide last completed
                refresh_url = f"{base_url}/groups/{workspace_id}/datasets/{dataset_id}/refreshes?$top=5"
                refresh_response = session_obj.get(refresh_url, timeout=5)

                # API 415 means non-model dataset (live/DirectQuery)
                if refresh_response.status_code == 415:
                    print(f"      ℹ️  Dataset {dataset_id[:8]}... is live/DirectQuery (API 415) - NOT inactive")
                    return False, dataset_id

                # Permission/access errors - don't count as inactive
                if refresh_response.status_code in [403, 404]:
                    print(f"      ℹ️  Dataset {dataset_id[:8]}... access error (Status: {refresh_response.status_code}) - NOT inactive")
                    return False, dataset_id

                if refresh_response.status_code == 200:
                    refresh_history = refresh_response.json().get('value', [])
                    picked = pick_best_refresh_from_history(refresh_history)
                    end_time_str = picked.get('last_refreshed')

                    if end_time_str:
                        try:
                            # Handle ISO 8601 format with Z suffix
                            if end_time_str.endswith('Z'):
                                end_time_str = end_time_str[:-1] + '+00:00'

                            end_time = datetime.fromisoformat(end_time_str)

                            # Ensure timezone-aware comparison
                            if end_time.tzinfo is None:
                                end_time = end_time.replace(tzinfo=timezone.utc)

                            now = datetime.now(timezone.utc)
                            days_since_refresh = (now - end_time).days

                            # ONLY count as inactive if >30 days since last completed refresh
                            if days_since_refresh >= 30:
                                print(f"      ⚠️  Dataset {dataset_id[:8]}... is INACTIVE - {days_since_refresh} days since last refresh")
                                return True, dataset_id
                            else:
                                return False, dataset_id
                        except Exception:
                            # Can't parse date - don't count as inactive
                            print(f"      ℹ️  Dataset {dataset_id[:8]}... date parse error - treating as active")
                            return False, dataset_id
                    else:
                        # No usable timestamp - don't count as inactive (DQ/Live or never refreshed)
                        print(f"      ℹ️  Dataset {dataset_id[:8]}... no usable refresh timestamp - NOT inactive")
                        return False, dataset_id
                else:
                    # Other API errors - don't count as inactive
                    print(f"      ℹ️  Dataset {dataset_id[:8]}... API error (Status: {refresh_response.status_code}) - NOT inactive")
                    return False, dataset_id

            except Exception as e:
                # On error - don't count as inactive
                print(f"      ℹ️  Dataset {dataset_id[:8]}... exception: {str(e)} - treating as active")
                return False, dataset_id

            return False, dataset_id

        # Check datasets in parallel (max 20 concurrent workers for faster processing)
        inactive_dataset_ids = set()

        if dataset_to_reports:
            print(f"   Checking {len(dataset_to_reports)} datasets for inactivity...")

            # ⚡ PERFORMANCE OPTIMIZATION: Parallel processing for refresh history checks
            # Use ThreadPoolExecutor with more workers and process in smaller batches
            with ThreadPoolExecutor(max_workers=20) as executor:
                # Submit all dataset checks at once
                future_to_dataset = {
                    executor.submit(check_dataset_inactive, dataset_id): dataset_id
                    for dataset_id in dataset_to_reports.keys()
                }

                # Collect results as they complete (don't wait for all)
                for future in as_completed(future_to_dataset):
                    try:
                        is_inactive, dataset_id = future.result(timeout=10)
                        if is_inactive:
                            inactive_dataset_ids.add(dataset_id)
                    except Exception as e:
                        # Silently mark as inactive on error to avoid blocking
                        dataset_id = future_to_dataset.get(future)
                        if dataset_id:
                            inactive_dataset_ids.add(dataset_id)

        # Close the session to free up connections
        session_obj.close()

        # Count inactive reports (all reports linked to inactive datasets)
        inactive_reports_count = 0
        for dataset_id in inactive_dataset_ids:
            inactive_reports_count += len(dataset_to_reports.get(dataset_id, []))

        print(f"   ✅ Summary: {total_reports} total, {inactive_reports_count} inactive")

        # Build response
        summary_data = {
            'success': True,
            'workspace_id': workspace_id,
            'total_reports': total_reports,
            'inactive_reports': inactive_reports_count
        }

        # Cache the result (15 minutes)
        workspace_cache[cache_key] = {
            'data': summary_data,
            'timestamp': current_time
        }

        return jsonify(summary_data)

    except requests.exceptions.RequestException as e:
        print(f"❌ API Error in workspace summary: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"   Status: {e.response.status_code}")
            print(f"   Response: {e.response.text[:200]}")
        return jsonify({
            'success': False,
            'error': str(e),
            'workspace_id': workspace_id,
            'total_reports': 0,
            'inactive_reports': 'N/A'
        }), 500

    except Exception as e:
        print(f"❌ Error in workspace summary: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e),
            'workspace_id': workspace_id,
            'total_reports': 0,
            'inactive_reports': 'N/A'
        }), 500


@app.route('/api/me/capabilities')
@login_required
def api_me_capabilities():
    """UI feature flags for the signed-in user (e.g. archive download button)."""
    try:
        from features.report_archive_service import user_can_archive
        email = session.get('user', {}).get('preferred_username') or ''
        return jsonify({
            'success': True,
            'canArchiveReports': user_can_archive(email),
            'email': email,
        })
    except Exception as e:
        return jsonify({'success': False, 'canArchiveReports': False, 'error': str(e)}), 500


@app.route('/api/reports/archive-to-sharepoint', methods=['POST'])
@login_required
def api_archive_report_to_sharepoint():
    """
    Export a Power BI report (.pbix/.rdl) and upload to SharePoint
    Report Decommission Activity / <latest dated folder> / Workspace / [Folder] /.

    Restricted to Central Analytics team UPNs (see report_archive_service).
    Does not alter catalog fast-path, crash test, or generate flows.
    """
    try:
        from features.report_archive_service import (
            archive_report_to_sharepoint,
            user_can_archive,
        )

        email = session.get('user', {}).get('preferred_username') or ''
        if not user_can_archive(email):
            return jsonify({
                'success': False,
                'error': 'Not authorized to archive reports to SharePoint.',
            }), 403

        data = request.get_json(silent=True) or {}
        workspace_id = (data.get('workspace_id') or request.args.get('workspace_id') or '').strip()
        report_id = (data.get('report_id') or request.args.get('report_id') or '').strip()
        report_name = (data.get('report_name') or data.get('name') or 'Report').strip()
        workspace_name = (data.get('workspace_name') or data.get('workspaceName') or '').strip()
        folder_name = (data.get('folder_name') or data.get('folderName') or '').strip() or None
        folder_id = (data.get('folder_id') or data.get('folderId') or '').strip() or None

        if not workspace_id or not report_id:
            return jsonify({
                'success': False,
                'error': 'workspace_id and report_id are required',
            }), 400

        # Resolve workspace display name if missing
        if not workspace_name:
            try:
                if CATALOG_AVAILABLE and catalog_service is not None:
                    allowed = _user_allowed_workspace_ids()
                    pack = catalog_service.get_workspace_reports(
                        workspace_id, allowed_workspace_ids=allowed
                    )
                    if pack:
                        workspace_name = (
                            (pack.get('workspace') or {}).get('name')
                            or pack.get('workspace_name')
                            or ''
                        )
            except Exception:
                pass
        if not workspace_name:
            workspace_name = workspace_id[:8]

        # Prefer user delegated token (same access as UI). Also acquire SP token as
        # fallback — REST Export sometimes 500s on large PBIX with one principal
        # and succeeds with the other (Service UI download uses a different path).
        token = None
        sp_token = None
        try:
            token = get_user_powerbi_token()
        except Exception:
            token = None
        try:
            from scanner_connector import PowerBIScanner
            sc = PowerBIScanner()
            sp_token = sc.get_access_token()
        except Exception as ex:
            print(f"   ⚠️ SP token for export fallback unavailable: {ex}")
            sp_token = None
        if not token:
            token = sp_token
        if not token:
            return jsonify({
                'success': False,
                'error': 'Unable to obtain Power BI token for Export',
            }), 401

        print(f"\n📦 ARCHIVE TO SHAREPOINT")
        print(f"   User: {email}")
        print(f"   Workspace: {workspace_name} ({workspace_id[:8]}…)")
        print(f"   Report: {report_name} ({report_id[:8]}…)")
        print(f"   PBI folder: {folder_name or '(root)'}")
        print(f"   Tokens: user={'yes' if token and token != sp_token else 'no'} sp={'yes' if sp_token else 'no'}")

        result = archive_report_to_sharepoint(
            access_token=token,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            report_id=report_id,
            report_name=report_name,
            folder_name=folder_name,
            folder_id=folder_id,
            fallback_token=sp_token if (sp_token and sp_token != token) else None,
        )
        if result.get('success'):
            print(f"   ✅ ARCHIVE OK → {result.get('remotePath')}")
            return jsonify(result), 200

        err = result.get('error') or 'Archive failed'
        stage = result.get('stage') or 'unknown'
        print(f"   ❌ ARCHIVE FAILED stage={stage}: {err}")
        status = 400
        sc = result.get('status_code')
        if sc in (401, 403):
            status = int(sc)
        elif sc == 404:
            status = 404
        return jsonify(result), status
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"   ❌ ARCHIVE EXCEPTION: {e}")
        return jsonify({'success': False, 'error': str(e), 'stage': 'exception'}), 500


@app.route('/api/reports/crash-test/<report_id>', methods=['GET', 'POST'])
@login_required
def crash_test_report(report_id):
    """
    Run crash test analysis on a specific report with optional visual field bindings

    Accepts both GET (legacy) and POST (with visual field bindings from frontend)
    Returns health score and detected issues with deep root cause analysis
    """
    try:
        # Handle both GET and POST requests
        if request.method == 'POST':
            data = request.get_json() or {}
            workspace_id = data.get('workspace_id')
            dataset_id = data.get('dataset_id')
            mode = data.get('mode', 'standard')
            visual_field_bindings = data.get('visual_field_bindings')  # ⭐ NEW!
        else:
            # Legacy GET support
            workspace_id = request.args.get('workspace_id')
            dataset_id = request.args.get('dataset_id')
            mode = request.args.get('mode', 'standard')
            visual_field_bindings = None

        if not workspace_id or not dataset_id:
            return jsonify({
                'success': False,
                'error': 'Missing required parameters: workspace_id and dataset_id'
            }), 400

        print(f"\n🔬 HYBRID CRASH TEST REQUEST")
        print(f"   Report: {report_id}")
        print(f"   Workspace: {workspace_id}")
        print(f"   Dataset: {dataset_id}")
        print(f"   Mode: {mode}")
        if visual_field_bindings:
            print(f"   ⭐ Visual Field Bindings: {len(visual_field_bindings)} visuals extracted from frontend")
        else:
            print(f"   ℹ️  No visual field bindings provided (legacy mode)")

        # Import crash test analyzer
        from crash_test_analyzer import CrashTestAnalyzer
        import os

        # Get service principal credentials for Enhanced Mode
        client_id = os.getenv('CLIENT_ID')
        client_secret = os.getenv('CLIENT_SECRET')
        tenant_id = os.getenv('TENANT_ID')

        # Fetch report metadata (modifiedBy, modifiedDateTime) from Scanner API
        print(f"   📋 Fetching report metadata from Scanner API...")
        from scanner_connector import PowerBIScanner
        report_metadata = {'modified_by': 'N/A', 'modified_date': None}

        try:
            scanner = PowerBIScanner()
            scan_result = scanner.run_scan(workspace_id=workspace_id)

            if scan_result and "workspaces" in scan_result:
                for workspace in scan_result.get('workspaces', []):
                    if workspace.get('id') == workspace_id:
                        for report in workspace.get('reports', []):
                            if report.get('id') == report_id:
                                # Scanner API returns modifiedBy as email/UPN directly
                                modified_by = report.get('modifiedBy', '')
                                modified_date = report.get('modifiedDateTime')

                                print(f"      🔍 Scanner API report found")
                                print(f"      📋 modifiedBy: '{modified_by}'")
                                print(f"      📋 modifiedDateTime: '{modified_date}'")

                                # If modifiedBy is empty, try createdBy as fallback
                                if not modified_by or modified_by == '':
                                    modified_by = report.get('createdBy', 'N/A')
                                    print(f"      ⚠️  modifiedBy empty, using createdBy: '{modified_by}'")

                                report_metadata = {
                                    'modified_by': modified_by,
                                    'modified_date': modified_date
                                }
                                break
                        break
                print(f"      ✅ Report metadata extracted: {report_metadata}")
            else:
                print(f"      ⚠️  Scanner API returned no data")
        except Exception as e:
            print(f"      ⚠️  Could not fetch report metadata from Scanner API: {e}")

        # Prefer the signed-in user's token so Playwright can render the report
        # the same way Service does (GenerateToken often fails for service principals).
        user_token = None
        try:
            user_token = get_user_powerbi_token()
        except Exception as token_err:
            print(f"   ⚠️  Could not get user Power BI token: {token_err}")

        analyzer = CrashTestAnalyzer(
            workspace_id=workspace_id,
            report_id=report_id,
            dataset_id=dataset_id,
            access_token=user_token,
            client_id=client_id,
            client_secret=client_secret,
            tenant_id=tenant_id,
            user_token=user_token
        )

        # Set report metadata on analyzer
        analyzer.report_metadata = report_metadata
        print(f"   ✅ Report metadata set: {report_metadata}")

        # ⭐ NEW: Pass visual field bindings to analyzer if available
        if visual_field_bindings:
            analyzer.set_visual_field_bindings(visual_field_bindings)

        # Run crash test (with visual analysis, lineage, and version history if Enhanced Mode)
        include_visual_analysis = (mode == 'enhanced')
        include_lineage_analysis = (mode == 'enhanced')
        include_version_history = (mode == 'enhanced')

        # ⭐ Enable XMLA schema analysis if we have visual bindings
        use_xmla_schema = (visual_field_bindings is not None and len(visual_field_bindings) > 0)

        print(f"   🚀 Running HYBRID deep-dive crash test...")
        print(f"      Visual Analysis: {include_visual_analysis}")
        print(f"      Lineage Analysis: {include_lineage_analysis}")
        print(f"      Version History: {include_version_history}")
        print(f"      XMLA Schema Analysis: {use_xmla_schema}")

        results = analyzer.run_crash_test(
            include_visual_analysis=include_visual_analysis,
            include_lineage_analysis=include_lineage_analysis,
            include_version_history=include_version_history,
            use_xmla_schema=use_xmla_schema  # ⭐ NEW!
        )

        # Format response
        health_score = results.get('health_score', 0)
        issues = results.get('issues', [])
        warnings = results.get('warnings', [])
        lineage_analysis = results.get('lineage_analysis', {})
        root_cause_analysis = results.get('root_cause_analysis', [])
        change_impact_summary = results.get('change_impact_summary', {})

        # Get refresh history (robust: walk past in-progress null endTime)
        print(f"   📊 Fetching refresh history...")
        from powerbi_connector import resolve_dataset_refresh_info, pick_best_refresh_from_history
        powerbi = PowerBIConnector(user_token=session.get('access_token'))
        refresh_history = powerbi.get_refresh_history(workspace_id, dataset_id, top=5)

        refresh_info = None
        if refresh_history and len(refresh_history) > 0:
            picked = pick_best_refresh_from_history(refresh_history)
            # Prefer the raw history row used for timestamp; fall back to newest
            source_idx = picked.get('source_index') if picked.get('source_index') is not None else 0
            source_idx = max(0, min(source_idx, len(refresh_history) - 1))
            last_refresh = refresh_history[source_idx] or refresh_history[0]
            refresh_info = {
                'status': picked.get('last_refresh_status') or last_refresh.get('status', 'Unknown'),
                'refreshType': last_refresh.get('refreshType', 'Unknown'),
                'startTime': last_refresh.get('startTime', ''),
                'endTime': picked.get('last_refreshed') or last_refresh.get('endTime', ''),
                'serviceExceptionJson': last_refresh.get('serviceExceptionJson', ''),
                'note': picked.get('refresh_note'),
            }
            print(f"      ✓ Last refresh: {refresh_info['status']} at {refresh_info['endTime']}")
        else:
            # Try full resolver for DirectQuery/Live labeling
            try:
                resolved = resolve_dataset_refresh_info(
                    headers=get_user_powerbi_headers(),
                    workspace_id=workspace_id,
                    dataset_id=dataset_id,
                    history_top=5,
                )
                if resolved.get('refresh_type') in ('directquery', 'live') or (
                    resolved.get('last_refresh_status') and 'directquery' in str(resolved.get('last_refresh_status')).lower()
                ):
                    refresh_info = {
                        'status': 'DirectQuery/Live',
                        'refreshType': 'DirectQuery/Live',
                        'startTime': '',
                        'endTime': '',
                        'serviceExceptionJson': '',
                        'note': resolved.get('refresh_note'),
                    }
                    print(f"      ℹ️ DirectQuery/Live dataset")
                else:
                    print(f"      ⚠ No refresh history available")
            except Exception:
                print(f"      ⚠ No refresh history available")

        # Calculate score breakdown
        critical_count = len([i for i in issues if i.get('severity') == 'Critical'])
        high_count = len([i for i in issues if i.get('severity') == 'High'])
        medium_count = len([i for i in issues if i.get('severity') == 'Medium'])
        warning_count = len(warnings)

        score_breakdown = {
            'starting_score': 100,
            'critical_issues': critical_count,
            'critical_deduction': critical_count * 20,
            'high_issues': high_count,
            'high_deduction': high_count * 10,
            'medium_issues': medium_count,
            'medium_deduction': medium_count * 5,
            'warnings': warning_count,
            'warning_deduction': warning_count * 2,
            'final_score': health_score
        }

        print(f"   ✅ Deep-dive crash test complete: {health_score}/100")
        print(f"   Issues: {len(issues)}, Warnings: {len(warnings)}")
        print(f"   Root Causes: {len(root_cause_analysis)}")
        if lineage_analysis:
            print(f"   Lineage: {lineage_analysis.get('affected_tables_count', 0)} tables analyzed")
        if change_impact_summary:
            breaking_changes = change_impact_summary.get('breaking_changes', [])
            print(f"   Breaking Changes: {len(breaking_changes)}")

        return jsonify({
            'success': True,
            'health_score': health_score,
            'status': 'excellent' if health_score >= 90 else 'good' if health_score >= 70 else 'fair' if health_score >= 50 else 'poor',
            'issues': issues,
            'warnings': warnings,
            'mode': mode,
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'refresh_info': refresh_info,
            'score_breakdown': score_breakdown,
            'lineage_analysis': lineage_analysis,
            'root_cause_analysis': root_cause_analysis,  # NEW!
            'change_impact_summary': change_impact_summary,  # NEW!
            'visual_analysis_performed': results.get('visual_analysis_performed', False),
            'visual_analysis': results.get('visual_analysis') or {},
        })

    except Exception as e:
        print(f"   ❌ Crash test error: {e}")
        print(f"   Error type: {type(e).__name__}")
        import traceback
        traceback.print_exc()

        # Get full traceback as string for debugging
        import sys
        exc_info = sys.exc_info()
        tb_lines = traceback.format_exception(*exc_info)
        full_traceback = ''.join(tb_lines)

        print(f"\n{'='*80}")
        print(f"FULL TRACEBACK:")
        print(full_traceback)
        print(f"{'='*80}\n")

        return jsonify({
            'success': False,
            'error': str(e),
            'error_type': type(e).__name__,
            'traceback': full_traceback if os.getenv('FLASK_ENV') == 'development' else 'Enable debug mode to see traceback'
        }), 500


@app.route('/api/report-details/<report_id>')
@login_required
def get_report_details(report_id):
    """API endpoint to get detailed information for a specific report (lazy loading)"""
    try:
        workspace_id = request.args.get('workspace_id')
        dataset_id = request.args.get('dataset_id')

        if not workspace_id or not dataset_id:
            return jsonify({
                'success': False,
                'error': 'workspace_id and dataset_id are required'
            }), 400

        print(f"\n🔍 Fetching report details for report {report_id} in workspace {workspace_id}")
        print(f"   Dataset ID: {dataset_id}")

        # ✅ FIX: Use user-delegated token instead of service principal
        user_token = session.get('access_token')
        if user_token:
            powerbi.set_user_token(user_token)
            print(f"   🔑 Using user-delegated token for API calls")
        else:
            print(f"   ⚠️ No user token found, falling back to service principal")

        details = {}

        try:
            # Robust refresh resolution (history fallback, DirectQuery/Live, schedule)
            print(f"   → Resolving dataset refresh info...")
            refresh_info = powerbi.resolve_dataset_refresh(
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                history_top=5,
            )

            details['refresh_type'] = refresh_info.get('refresh_type')
            details['refresh_note'] = refresh_info.get('refresh_note')
            details['last_refreshed'] = refresh_info.get('last_refreshed')
            details['last_refresh_status'] = refresh_info.get('last_refresh_status')

            schedule_text = refresh_info.get('refresh_schedule')
            if refresh_info.get('schedule_days') or refresh_info.get('schedule_times'):
                details['refresh_schedule'] = {
                    'enabled': bool(refresh_info.get('schedule_days') or refresh_info.get('schedule_times')),
                    'days': refresh_info.get('schedule_days') or [],
                    'times': refresh_info.get('schedule_times') or [],
                    'display': schedule_text,
                }
            elif schedule_text:
                details['refresh_schedule'] = schedule_text

            print(
                f"      ✓ Last refresh: {details.get('last_refreshed')} "
                f"({details.get('last_refresh_status')}) "
                f"type={details.get('refresh_type')}"
            )

            # Get dataset details
            print(f"   → Fetching dataset info...")
            dataset_info = powerbi.get_dataset_info(workspace_id, dataset_id)
            if dataset_info:
                configured_by = dataset_info.get('configuredBy', '')
                if configured_by:
                    details['last_accessed_by'] = configured_by

                created_date = dataset_info.get('createdDate', '')
                if created_date:
                    details['last_accessed'] = created_date
                print(f"      ✓ Dataset info retrieved")
            else:
                print(f"      ⚠ No dataset info available")

        except Exception as e:
            print(f"❌ Error getting report details: {str(e)}")
            import traceback
            traceback.print_exc()

        print(f"✅ Report details fetch complete. Found {len(details)} detail fields\n")
        return jsonify({
            'success': True,
            'report_id': report_id,
            'details': details
        })

    except Exception as e:
        print(f"❌ Error fetching report details: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


def scan_report_visual_columns(workspace_id, report_id, access_token, fabric_token=None):
    """
    Scan the report definition to extract all columns used in visuals across all pages.

    Uses two approaches:
    1. Fabric API getDefinition (PBIR-Legacy report.json) - most comprehensive (requires Fabric token)
    2. Fallback: Power BI Export API to download .pbix and parse report layout (uses Power BI token)

    Args:
        workspace_id: Power BI workspace ID
        report_id: Power BI report ID
        access_token: Bearer token for Power BI API (api.powerbi.com)
        fabric_token: Bearer token for Fabric API (api.fabric.microsoft.com), optional

    Returns:
        dict: {table_name: set(column_names)} of columns used in report visuals
    """
    # --- COMMENTED OUT: Visual scan methods disabled (returning 403/timeout) ---
    # All 4 methods (Fabric getDefinition, PBI token getDefinition, Export API, Pages API)
    # are currently returning 403 InsufficientScopes or timing out.
    # Returning empty dict; column usage is handled by the measure dependency fallback.
    return {}

    # import json
    # import base64
    #
    # used_columns = {}
    #
    # print(f"\n{'='*70}")
    # print(f"📊 SCANNING REPORT VISUALS FOR COLUMN USAGE")
    # print(f"   Report ID: {report_id}")
    # print(f"{'='*70}\n")
    #
    # # METHOD 1: Try Fabric API getDefinition (requires Fabric-scoped token)
    # try:
    #     if not fabric_token:
    #         print(f"   🔍 METHOD 1: Skipping Fabric API getDefinition (no Fabric token available)")
    #     else:
    #         print(f"   🔍 METHOD 1: Trying Fabric API getDefinition...")
    #
    #         fabric_headers = {
    #             'Authorization': f'Bearer {fabric_token}',
    #             'Content-Type': 'application/json'
    #         }
    #
    #         definition_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/reports/{report_id}/getDefinition"
    #
    #         response = requests.post(definition_url, headers=fabric_headers, json={}, timeout=30)
    #
    #         if response.status_code == 200:
    #             definition = response.json()
    #             parts = definition.get('definition', {}).get('parts', [])
    #
    #             print(f"      ✓ Got report definition with {len(parts)} parts")
    #
    #             for part in parts:
    #                 path = part.get('path', '')
    #                 payload = part.get('payload', '')
    #
    #                 # PBIR-Legacy: report.json contains all visuals
    #                 # PBIR: visual.json files under definition/pages/*/visuals/*/
    #                 if path == 'report.json' or 'visual.json' in path or path.endswith('.json'):
    #                     try:
    #                         decoded = base64.b64decode(payload).decode('utf-8')
    #                         part_json = json.loads(decoded)
    #                         if path == 'report.json':
    #                             _extract_columns_from_report_json(part_json, used_columns)
    #                         elif 'visual.json' in path:
    #                             # PBIR visual file — extract directly
    #                             _extract_columns_from_visual_config(part_json, used_columns, path)
    #                         else:
    #                             # Other JSON — try both approaches
    #                             _extract_columns_from_report_json(part_json, used_columns)
    #                     except Exception as e:
    #                         print(f"      ⚠️  Error parsing {path}: {e}")
    #
    #             if used_columns:
    #                 print(f"      ✅ Fabric API: Found columns in {len(used_columns)} tables")
    #                 return used_columns
    #         elif response.status_code == 202:
    #             # Long-running operation - try to follow it
    #             print(f"      ⏳ LRO triggered, checking operation status...")
    #             operation_url = response.headers.get('Location', '')
    #             retry_after = int(response.headers.get('Retry-After', '5'))
    #
    #             import time
    #             for attempt in range(3):
    #                 time.sleep(retry_after)
    #                 op_response = requests.get(operation_url, headers=fabric_headers, timeout=30)
    #                 if op_response.status_code == 200:
    #                     definition = op_response.json()
    #                     parts = definition.get('definition', {}).get('parts', [])
    #                     for part in parts:
    #                         path = part.get('path', '')
    #                         payload = part.get('payload', '')
    #                         if path == 'report.json' or 'visual.json' in path or path.endswith('.json'):
    #                             try:
    #                                 decoded = base64.b64decode(payload).decode('utf-8')
    #                                 part_json = json.loads(decoded)
    #                                 if path == 'report.json':
    #                                     _extract_columns_from_report_json(part_json, used_columns)
    #                                 elif 'visual.json' in path:
    #                                     _extract_columns_from_visual_config(part_json, used_columns, path)
    #                                 else:
    #                                     _extract_columns_from_report_json(part_json, used_columns)
    #                             except Exception as e:
    #                                 print(f"      ⚠️  Error parsing {path}: {e}")
    #                     break
    #
    #             if used_columns:
    #                 print(f"      ✅ Fabric API (LRO): Found columns in {len(used_columns)} tables")
    #                 return used_columns
    #         else:
    #             print(f"      ⚠️  Fabric API returned {response.status_code}: {response.text[:200]}")
    # except Exception as e:
    #     print(f"      ⚠️  Fabric API error: {e}")

    # # METHOD 2: Try Fabric getDefinition with Power BI token (often has broader access)
    # if not used_columns:
    #     try:
    #         print(f"\n   🔍 METHOD 2: Trying Fabric getDefinition with Power BI token...")
    #
    #         pbi_fabric_headers = {
    #             'Authorization': f'Bearer {access_token}',
    #             'Content-Type': 'application/json'
    #         }
    #
    #         # Try with PBIR-Legacy format explicitly
    #         for fmt_param in ['?format=PBIR-Legacy', '']:
    #             def_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/reports/{report_id}/getDefinition{fmt_param}"
    #             response = requests.post(def_url, headers=pbi_fabric_headers, json={}, timeout=30)
    #
    #             if response.status_code == 200:
    #                 definition = response.json()
    #                 parts = definition.get('definition', {}).get('parts', [])
    #                 print(f"      ✓ Got report definition with {len(parts)} parts (PBI token, fmt='{fmt_param}')")
    #
    #                 for part in parts:
    #                     path = part.get('path', '')
    #                     payload = part.get('payload', '')
    #                     if 'report' in path.lower() or path.endswith('.json'):
    #                         try:
    #                             decoded = base64.b64decode(payload).decode('utf-8')
    #                             report_json = json.loads(decoded)
    #                             _extract_columns_from_report_json(report_json, used_columns)
    #                         except Exception as e:
    #                             print(f"      ⚠️  Error parsing {path}: {e}")
    #
    #                 if used_columns:
    #                     print(f"      ✅ Fabric API (PBI token): Found columns in {len(used_columns)} tables")
    #                     return used_columns
    #             elif response.status_code == 202:
    #                 print(f"      ⏳ LRO triggered with PBI token, checking operation status...")
    #                 operation_url = response.headers.get('Location', '')
    #                 retry_after = int(response.headers.get('Retry-After', '5'))
    #                 import time
    #                 for _ in range(3):
    #                     time.sleep(retry_after)
    #                     op_response = requests.get(operation_url, headers=pbi_fabric_headers, timeout=30)
    #                     if op_response.status_code == 200:
    #                         definition = op_response.json()
    #                         parts = definition.get('definition', {}).get('parts', [])
    #                         for part in parts:
    #                             path = part.get('path', '')
    #                             payload = part.get('payload', '')
    #                             if 'report' in path.lower() or path.endswith('.json'):
    #                                 try:
    #                                     decoded = base64.b64decode(payload).decode('utf-8')
    #                                     report_json = json.loads(decoded)
    #                                     _extract_columns_from_report_json(report_json, used_columns)
    #                                 except Exception as e:
    #                                     print(f"      ⚠️  Error parsing {path}: {e}")
    #                         break
    #                 if used_columns:
    #                     print(f"      ✅ Fabric API (PBI token, LRO): Found columns in {len(used_columns)} tables")
    #                     return used_columns
    #             else:
    #                 print(f"      ⚠️  Fabric API (PBI token, fmt='{fmt_param}') returned {response.status_code}")
    #     except Exception as e:
    #         print(f"      ⚠️  Fabric API (PBI token) error: {e}")
    #
    # # METHOD 3: Try Power BI Export API to download .pbix
    # if not used_columns:
    #     try:
    #         print(f"\n   🔍 METHOD 3: Trying Power BI Export/Download report...")
    #
    #         export_url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}/Export"
    #         response = requests.get(export_url, headers={
    #             'Authorization': f'Bearer {access_token}'
    #         }, timeout=60)
    #
    #         if response.status_code == 200:
    #             print(f"      ✓ Downloaded report ({len(response.content)} bytes)")
    #
    #             import zipfile
    #             import io
    #
    #             try:
    #                 with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
    #                     # Look for Report/Layout or report.json
    #                     for name in zf.namelist():
    #                         if 'Layout' in name or name == 'Report/Layout':
    #                             with zf.open(name) as f:
    #                                 layout_data = f.read()
    #                                 try:
    #                                     layout_json = json.loads(layout_data.decode('utf-16-le'))
    #                                 except:
    #                                     layout_json = json.loads(layout_data.decode('utf-8'))
    #                                 _extract_columns_from_report_json(layout_json, used_columns)
    #                                 print(f"      ✓ Parsed Layout from .pbix")
    #                                 break
    #                         elif name == 'report.json':
    #                             with zf.open(name) as f:
    #                                 report_json = json.loads(f.read().decode('utf-8'))
    #                                 _extract_columns_from_report_json(report_json, used_columns)
    #                                 print(f"      ✓ Parsed report.json from .pbix")
    #                                 break
    #             except zipfile.BadZipFile:
    #                 print(f"      ⚠️  Downloaded file is not a valid zip/pbix")
    #         else:
    #             print(f"      ⚠️  Export API returned {response.status_code}")
    #     except Exception as e:
    #         print(f"      ⚠️  Export API error: {e}")
    #
    # # METHOD 4: Use Power BI ExportToFile async API (works when direct Export is blocked)
    # if not used_columns:
    #     try:
    #         print(f"\n   🔍 METHOD 4: Trying Power BI Pages API for report structure...")
    #
    #         # Try getting pages which IS available with standard token
    #         pages_url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}/pages"
    #         pages_resp = requests.get(pages_url, headers={
    #             'Authorization': f'Bearer {access_token}'
    #         }, timeout=15)
    #
    #         if pages_resp.status_code == 200:
    #             pages = pages_resp.json().get('value', [])
    #             print(f"      ✓ Report has {len(pages)} pages: {[p.get('displayName', p.get('name')) for p in pages]}")
    #             # Pages API confirms the report is accessible but doesn't give visual field info
    #             # Log this for debugging — the pages themselves confirm connectivity
    #         else:
    #             print(f"      ⚠️  Pages API returned {pages_resp.status_code}")
    #     except Exception as e:
    #         print(f"      ⚠️  Pages API error: {e}")
    #
    # total_cols = sum(len(v) for v in used_columns.values())
    # print(f"\n   📊 Report visual scan complete: {len(used_columns)} tables, {total_cols} columns")
    # for table, cols in list(used_columns.items())[:5]:
    #     print(f"      📋 {table}: {list(cols)[:5]}{'...' if len(cols) > 5 else ''}")
    #
    # return used_columns


def _extract_columns_from_report_json(report_json, used_columns):
    """
    Parse a Power BI report.json / Layout JSON to extract all columns used in visuals.

    Extracts from:
    - Visual projections (queryRef: "TableName.ColumnName")
    - prototypeQuery Select (Entity + Property)
    - Filters (column references)
    - Sort expressions
    """
    import re
    import json

    # Convert to string for regex-based extraction as well
    json_str = json.dumps(report_json) if isinstance(report_json, dict) else str(report_json)

    # Pattern 1: queryRef values like "TableName.ColumnName"
    query_ref_pattern = r'"queryRef"\s*:\s*"([^"]+)"'
    for match in re.finditer(query_ref_pattern, json_str):
        ref = match.group(1)
        if '.' in ref:
            parts = ref.split('.', 1)
            table_name = parts[0]
            column_name = parts[1]
            if table_name not in used_columns:
                used_columns[table_name] = set()
            used_columns[table_name].add(column_name)

    # Pattern 2: Entity + Property pairs from prototypeQuery Select
    # "Entity":"TableName" ... "Property":"ColumnName"
    entity_prop_pattern = r'"Entity"\s*:\s*"([^"]+)"[^}]*?"Property"\s*:\s*"([^"]+)"'
    for match in re.finditer(entity_prop_pattern, json_str):
        table_name = match.group(1)
        column_name = match.group(2)
        if table_name not in used_columns:
            used_columns[table_name] = set()
        used_columns[table_name].add(column_name)

    # Pattern 3: NativeReferenceName patterns like "TableName.ColumnName"
    native_ref_pattern = r'"NativeReferenceName"\s*:\s*"([^"]+)"'
    for match in re.finditer(native_ref_pattern, json_str):
        ref = match.group(1)
        # Some NativeReferenceName are just column names, skip those
        # Only process if it looks like Table.Column
        if '.' in ref:
            parts = ref.split('.', 1)
            table_name = parts[0]
            column_name = parts[1]
            if table_name not in used_columns:
                used_columns[table_name] = set()
            used_columns[table_name].add(column_name)

    # Pattern 4: Filter column references
    # "Column":{"Expression":{"SourceRef":{"Entity":"Table"}},"Property":"Column"}
    filter_col_pattern = r'"SourceRef"\s*:\s*\{\s*"Entity"\s*:\s*"([^"]+)"\s*\}[^}]*?"Property"\s*:\s*"([^"]+)"'
    for match in re.finditer(filter_col_pattern, json_str):
        table_name = match.group(1)
        column_name = match.group(2)
        if table_name not in used_columns:
            used_columns[table_name] = set()
        used_columns[table_name].add(column_name)

    # Pattern 5: HierarchyLevel references (matrix visuals, drilldowns)
    # "Level":"ColumnName" near "Entity":"TableName"
    level_pattern = r'"Level"\s*:\s*"([^"]+)"'
    for match in re.finditer(level_pattern, json_str):
        level_name = match.group(1)
        # Look backwards in the string for the nearest Entity reference
        start_pos = max(0, match.start() - 500)
        context = json_str[start_pos:match.start()]
        entity_match = re.search(r'"Entity"\s*:\s*"([^"]+)"', context)
        if entity_match:
            table_name = entity_match.group(1)
            if table_name not in used_columns:
                used_columns[table_name] = set()
            used_columns[table_name].add(level_name)

    # Pattern 6: DAX expression references in visual configs (Table[Column] pattern)
    dax_col_pattern = r"(?:'([^']+)'|([A-Za-z_][A-Za-z0-9_]*))\[([^\]]+)\]"
    # Only search in DAX-like contexts (measure expressions, calculated fields)
    dax_contexts = re.findall(r'"expression"\s*:\s*"([^"]*\[.*?\][^"]*)"', json_str, re.IGNORECASE)
    for dax_expr in dax_contexts:
        matches = re.findall(dax_col_pattern, dax_expr)
        for m in matches:
            tbl = m[0] if m[0] else m[1]
            col = m[2]
            if tbl and col:
                if tbl not in used_columns:
                    used_columns[tbl] = set()
                used_columns[tbl].add(col)

    # Also try to parse the structured sections if available
    try:
        sections = report_json.get('sections', [])
        for section in sections:
            page_name = section.get('displayName', section.get('name', 'Unknown'))
            containers = section.get('visualContainers', [])

            for container in containers:
                config_str = container.get('config', '{}')
                try:
                    config = json.loads(config_str) if isinstance(config_str, str) else config_str
                    _extract_columns_from_visual_config(config, used_columns, page_name)
                except json.JSONDecodeError:
                    pass

                # Also check filters on the container
                filters_str = container.get('filters', '[]')
                try:
                    filters = json.loads(filters_str) if isinstance(filters_str, str) else filters_str
                    _extract_columns_from_filters(filters, used_columns)
                except json.JSONDecodeError:
                    pass

            # Page-level filters
            page_filters_str = section.get('filters', '[]')
            try:
                page_filters = json.loads(page_filters_str) if isinstance(page_filters_str, str) else page_filters_str
                _extract_columns_from_filters(page_filters, used_columns)
            except json.JSONDecodeError:
                pass
    except (AttributeError, TypeError):
        pass  # report_json may not have structured sections

    # Report-level filters
    try:
        report_filters_str = report_json.get('filters', '[]')
        report_filters = json.loads(report_filters_str) if isinstance(report_filters_str, str) else report_filters_str
        _extract_columns_from_filters(report_filters, used_columns)
    except (AttributeError, TypeError, json.JSONDecodeError):
        pass


def _extract_columns_from_visual_config(config, used_columns, page_name=""):
    """Extract column references from a visual's config JSON"""

    single_visual = config.get('singleVisual', {})
    if not single_visual:
        return

    visual_type = single_visual.get('visualType', 'unknown')

    # Extract from projections
    projections = single_visual.get('projections', {})
    for role, fields in projections.items():
        if isinstance(fields, list):
            for field in fields:
                query_ref = field.get('queryRef', '')
                if '.' in query_ref:
                    parts = query_ref.split('.', 1)
                    table_name = parts[0]
                    column_name = parts[1]
                    if table_name not in used_columns:
                        used_columns[table_name] = set()
                    used_columns[table_name].add(column_name)
                    print(f"         ✓ Page '{page_name}' visual '{visual_type}' → {table_name}[{column_name}]")

    # Extract from prototypeQuery
    proto_query = single_visual.get('prototypeQuery', {})
    selects = proto_query.get('Select', [])
    from_clauses = proto_query.get('From', [])

    # Build alias-to-entity mapping
    alias_map = {}
    for frm in from_clauses:
        alias = frm.get('Name', '')
        entity = frm.get('Entity', '')
        if alias and entity:
            alias_map[alias] = entity

    for sel in selects:
        # Column references
        col_ref = sel.get('Column', {})
        if col_ref:
            expr = col_ref.get('Expression', {})
            source_ref = expr.get('SourceRef', {})
            source_alias = source_ref.get('Source', '')
            prop = col_ref.get('Property', '')
            table_name = alias_map.get(source_alias, source_alias)
            if table_name and prop:
                if table_name not in used_columns:
                    used_columns[table_name] = set()
                used_columns[table_name].add(prop)

        # Measure references
        measure_ref = sel.get('Measure', {})
        if measure_ref:
            expr = measure_ref.get('Expression', {})
            source_ref = expr.get('SourceRef', {})
            source_alias = source_ref.get('Source', '')
            prop = measure_ref.get('Property', '')
            table_name = alias_map.get(source_alias, source_alias)
            if table_name and prop:
                if table_name not in used_columns:
                    used_columns[table_name] = set()
                used_columns[table_name].add(prop)

        # Aggregation references
        agg_ref = sel.get('Aggregation', {})
        if agg_ref:
            agg_expr = agg_ref.get('Expression', {})
            col_inner = agg_expr.get('Column', {})
            if col_inner:
                inner_expr = col_inner.get('Expression', {})
                source_ref = inner_expr.get('SourceRef', {})
                source_alias = source_ref.get('Source', '')
                prop = col_inner.get('Property', '')
                table_name = alias_map.get(source_alias, source_alias)
                if table_name and prop:
                    if table_name not in used_columns:
                        used_columns[table_name] = set()
                    used_columns[table_name].add(prop)

        # HierarchyLevel references (common in matrix visuals)
        # {"HierarchyLevel":{"Expression":{"Hierarchy":{"Expression":{"SourceRef":{"Source":"t"}},"Hierarchy":"HierName"}},"Level":"ColName"}}
        hier_level = sel.get('HierarchyLevel', {})
        if hier_level:
            level_name = hier_level.get('Level', '')
            hier_expr = hier_level.get('Expression', {})
            hier_ref = hier_expr.get('Hierarchy', {})
            if hier_ref:
                inner_expr = hier_ref.get('Expression', {})
                source_ref = inner_expr.get('SourceRef', {})
                source_alias = source_ref.get('Source', '')
                table_name = alias_map.get(source_alias, source_alias)
                if table_name and level_name:
                    if table_name not in used_columns:
                        used_columns[table_name] = set()
                    used_columns[table_name].add(level_name)

        # Hierarchy references (the hierarchy itself — mark the hierarchy name as a reference)
        hier_ref = sel.get('Hierarchy', {})
        if hier_ref:
            inner_expr = hier_ref.get('Expression', {})
            source_ref = inner_expr.get('SourceRef', {})
            source_alias = source_ref.get('Source', '')
            hier_name = hier_ref.get('Hierarchy', '')
            table_name = alias_map.get(source_alias, source_alias)
            if table_name and hier_name:
                if table_name not in used_columns:
                    used_columns[table_name] = set()
                used_columns[table_name].add(hier_name)


def _extract_columns_from_filters(filters, used_columns):
    """Extract column references from filter definitions, including hierarchy and nested filters"""
    if not isinstance(filters, list):
        return

    def _extract_col_from_expr(expr):
        """Recursively extract column refs from a filter expression dict"""
        if not isinstance(expr, dict):
            return
        # Column reference
        col = expr.get('Column', {})
        if col:
            entity_expr = col.get('Expression', {})
            source_ref = entity_expr.get('SourceRef', {})
            table_name = source_ref.get('Entity', '')
            column_name = col.get('Property', '')
            if table_name and column_name:
                if table_name not in used_columns:
                    used_columns[table_name] = set()
                used_columns[table_name].add(column_name)

        # HierarchyLevel filter
        hier_level = expr.get('HierarchyLevel', {})
        if hier_level:
            level_name = hier_level.get('Level', '')
            hier_expr = hier_level.get('Expression', {})
            hier_ref = hier_expr.get('Hierarchy', {})
            if hier_ref:
                inner_expr = hier_ref.get('Expression', {})
                source_ref = inner_expr.get('SourceRef', {})
                table_name = source_ref.get('Entity', '')
                if table_name and level_name:
                    if table_name not in used_columns:
                        used_columns[table_name] = set()
                    used_columns[table_name].add(level_name)

        # Measure reference in filters
        measure = expr.get('Measure', {})
        if measure:
            m_expr = measure.get('Expression', {})
            source_ref = m_expr.get('SourceRef', {})
            table_name = source_ref.get('Entity', '')
            measure_name = measure.get('Property', '')
            if table_name and measure_name:
                if table_name not in used_columns:
                    used_columns[table_name] = set()
                used_columns[table_name].add(measure_name)

        # Recurse into nested expressions (And, Or, Not, Comparison, etc.)
        for key in ('Left', 'Right', 'Expression', 'Condition', 'And', 'Or', 'Not'):
            nested = expr.get(key)
            if isinstance(nested, dict):
                _extract_col_from_expr(nested)
            elif isinstance(nested, list):
                for item in nested:
                    _extract_col_from_expr(item)

    for f in filters:
        try:
            expr = f.get('expression', {})
            _extract_col_from_expr(expr)

            # Also check the 'filter' key (some formats use this)
            filter_inner = f.get('filter', {})
            if filter_inner:
                where = filter_inner.get('Where', [])
                for w in (where if isinstance(where, list) else [where]):
                    if isinstance(w, dict):
                        cond = w.get('Condition', {})
                        _extract_col_from_expr(cond)
        except (AttributeError, TypeError):
            pass


@app.route('/api/lineage')
@login_required
def get_lineage():
    """API endpoint to get query and table lineage for a specific report"""
    try:
        import requests
        import re
        workspace_id = request.args.get('workspace_id')
        report_id = request.args.get('report_id')

        if not workspace_id or not report_id:
            return jsonify({
                'success': False,
                'error': 'workspace_id and report_id are required'
            }), 400

        # Get user token from session
        user_token = session.get('access_token')

        if not user_token:
            return jsonify({
                'success': False,
                'error': 'Not authenticated'
            }), 401

        headers = {
            'Authorization': f'Bearer {user_token}',
            'Content-Type': 'application/json'
        }

        # Step 1: Get report details to find dataset ID
        report_url = f'https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}'
        report_response = requests.get(report_url, headers=headers)

        if report_response.status_code != 200:
            return jsonify({
                'success': False,
                'error': f'Failed to fetch report: {report_response.status_code}'
            }), report_response.status_code

        report_data = report_response.json()
        dataset_id = report_data.get('datasetId')

        if not dataset_id:
            return jsonify({
                'success': True,
                'queries': []
            })

        # Step 2: Get all workspaces to create workspace name mapping for Dataflows
        workspace_map = {}
        try:
            # Get workspaces from session cache or API
            workspaces_cache_key = f"workspaces_{session.get('user_id')}"
            cached_workspaces = session.get(workspaces_cache_key)

            if cached_workspaces:
                workspace_map = {ws['id']: ws['name'] for ws in cached_workspaces}
            else:
                # Fetch workspaces if not cached
                workspaces_url = 'https://api.powerbi.com/v1.0/myorg/groups'
                workspaces_response = requests.get(workspaces_url, headers=headers)
                if workspaces_response.status_code == 200:
                    workspaces_data = workspaces_response.json()
                    workspace_map = {ws['id']: ws['name'] for ws in workspaces_data.get('value', [])}
        except Exception as e:
            print(f"   ⚠️ Could not fetch workspace names for Dataflow resolution: {e}")

        # Step 3: Use Scanner API to get dataset expressions and table information
        try:
            import time as time_module
            from concurrent.futures import ThreadPoolExecutor
            from scanner_connector import PowerBIScanner

            lineage_start_time = time_module.time()

            # Check scanner model cache (keyed by dataset_id to avoid redundant scans)
            model_cache_key = f"{workspace_id}_{dataset_id}"
            current_time = time_module.time()
            cached_model = scanner_cache.get(model_cache_key)

            if cached_model and cached_model.get('data') and \
               (current_time - cached_model.get('timestamp', 0)) < SCANNER_CACHE_DURATION:
                print(f"   ⚡ Using cached dataset model (age: {int(current_time - cached_model['timestamp'])}s)")
                model = cached_model['data']
            else:
                scanner = PowerBIScanner()
                print(f"   🔍 Fetching dataset model for dataset {dataset_id}...")
                model = scanner.get_dataset_model(dataset_id, workspace_id=workspace_id)
                # Cache the model result
                scanner_cache[model_cache_key] = {
                    'data': model,
                    'timestamp': time_module.time()
                }
                print(f"   💾 Dataset model cached (key: {model_cache_key})")

            scanner_elapsed = time_module.time() - lineage_start_time
            print(f"   ⏱️ Scanner step took {scanner_elapsed:.1f}s")

            queries = []

            # Get all tables from the model - extract table names (strings) not dict objects
            model_tables = model.get('tables', [])
            all_table_names = set()
            for t in model_tables:
                if isinstance(t, dict):
                    table_name = t.get('name') or t.get('table')
                    if table_name:
                        all_table_names.add(table_name)
                elif isinstance(t, str) and t:
                    all_table_names.add(t)

            # Get columns dictionary from the model
            all_columns = model.get('columns', {})

            print(f"   📊 Dataset contains {len(all_table_names)} total tables/queries")
            print(f"   🔍 Available tables: {list(all_table_names)}")
            print(f"   📋 Column data structure keys: {list(all_columns.keys())[:5] if all_columns else 'None'}")
            if all_columns:
                first_table = list(all_columns.keys())[0] if all_columns else None
                if first_table:
                    print(f"   📋 Sample columns for '{first_table}': {all_columns[first_table][:2] if all_columns[first_table] else 'None'}")
            print(f"   🔍 Processing {len(model.get('expressions', []))} M expressions...")

            # Build column usage map from multiple sources
            # Format: {table_name: {column_name: set_of_sources}}
            # Each source label describes HOW the column is used
            column_usage = {}
            import re
            dax_pattern = r"(?:'([^']+)'|(\w+))\[([^\]]+)\]"

            def _mark_used(tbl, col_nm, source_label):
                """Helper to add a usage source for a column"""
                if tbl and col_nm:
                    if tbl not in column_usage:
                        column_usage[tbl] = {}
                    if col_nm not in column_usage[tbl]:
                        column_usage[tbl][col_nm] = set()
                    column_usage[tbl][col_nm].add(source_label)

            # SOURCE 1: Scanner API's built-in isReferenced flag on columns
            scanner_ref_count = 0
            for table_name, table_cols in all_columns.items():
                for col in table_cols:
                    if col.get('isReferenced') is True:
                        _mark_used(table_name, col.get('name'), 'Scanner API')
                        scanner_ref_count += 1
            print(f"   ✅ Scanner isReferenced: {scanner_ref_count} columns flagged across {len(column_usage)} tables")

            # SOURCE 2: Parse DAX measure expressions for Table[Column] references
            measures = model.get('measures', [])
            measure_col_count = 0
            for measure in measures:
                measure_expr = measure.get('expression', '')
                if measure_expr:
                    matches = re.findall(dax_pattern, measure_expr)
                    for match in matches:
                        table_name = match[0] if match[0] else match[1]
                        column_name = match[2]
                        _mark_used(table_name, column_name, 'DAX Measure')
                        measure_col_count += 1
            print(f"   ✅ Measures: {measure_col_count} column refs")

            # SOURCE 3: Parse calculated column expressions for Table[Column] references
            calc_col_count = 0
            for table_name, table_cols in all_columns.items():
                for col in table_cols:
                    calc_expr = col.get('expression', '')
                    if calc_expr and col.get('columnType') == 'Calculated':
                        matches = re.findall(dax_pattern, calc_expr)
                        for match in matches:
                            ref_table = match[0] if match[0] else match[1]
                            ref_col = match[2]
                            _mark_used(ref_table, ref_col, 'Calculated Column')
                            calc_col_count += 1
                        # Also mark the calculated column itself as used
                        _mark_used(table_name, col.get('name'), 'Calculated Column')
            print(f"   ✅ Calculated columns: {calc_col_count} column refs")

            # SOURCE 4: Extract columns from SQL SELECT clauses in M expressions
            sql_col_count = 0
            for expr_data in model.get('expressions', []):
                expr_table = expr_data.get('table', '')
                expression = expr_data.get('expression', '')
                if expression and ('Sql.Database' in expression or 'Query=' in expression):
                    # Extract the SQL query from the M expression
                    sql_query_pattern = r'Query\s*=\s*"([^"]*)"'
                    sql_matches = re.findall(sql_query_pattern, expression, re.IGNORECASE)
                    for sql_query in sql_matches:
                        sql_query = sql_query.replace('""', '"').replace('#(lf)', '\n').replace('#(cr)', '\r').replace('#(tab)', '\t')
                        sql_columns = extract_sql_column_names(sql_query)
                        for col in sql_columns:
                            _mark_used(expr_table, col, 'SQL Query')
                            sql_col_count += 1
            print(f"   ✅ SQL SELECT columns: {sql_col_count} column refs")

            # SOURCE 5: Query dataset model via DAX for column metadata (SortByColumn, IsHidden)
            dax_meta_count = 0
            try:
                meta_url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
                meta_headers = {
                    'Authorization': f'Bearer {user_token}',
                    'Content-Type': 'application/json'
                }
                # INFO.VIEW.COLUMNS() returns column metadata including IsHidden, SortByColumn
                meta_body = {
                    "queries": [{"query": "EVALUATE INFO.VIEW.COLUMNS()"}],
                    "serializerSettings": {"includeNulls": False}
                }
                meta_resp = requests.post(meta_url, headers=meta_headers, json=meta_body, timeout=15)
                if meta_resp.status_code == 200:
                    meta_result = meta_resp.json()
                    if 'results' in meta_result and meta_result['results']:
                        meta_table = meta_result['results'][0].get('tables', [{}])[0]
                        meta_rows = meta_table.get('rows', [])
                        for row in meta_rows:
                            tbl = row.get('[TableName]', row.get('TableName', ''))
                            col = row.get('[ColumnName]', row.get('ColumnName', ''))
                            is_hidden = row.get('[IsHidden]', row.get('IsHidden', True))
                            sort_by = row.get('[SortByColumn]', row.get('SortByColumn', ''))
                            if not tbl or not col:
                                continue
                            # Skip auto-generated date tables
                            if tbl.startswith('LocalDateTable_') or tbl.startswith('DateTableTemplate_'):
                                continue
                            # Columns that are NOT hidden are visible in the model → likely used
                            if is_hidden is False:
                                _mark_used(tbl, col, 'Model (Visible)')
                                dax_meta_count += 1
                            # Columns used as SortByColumn are definitely used
                            if sort_by:
                                _mark_used(tbl, sort_by, 'Sort By Column')
                                dax_meta_count += 1
                    print(f"   ✅ DAX column metadata: {dax_meta_count} columns from INFO.VIEW.COLUMNS()")
                else:
                    print(f"   ⚠️  INFO.VIEW.COLUMNS() returned {meta_resp.status_code} — skipping")
            except Exception as meta_err:
                print(f"   ⚠️  DAX column metadata error: {meta_err}")

            # SOURCE 6: Parse M expressions for column operations (SelectColumns, RenameColumns, etc.)
            m_col_count = 0
            for expr_data in model.get('expressions', []):
                expr_table = expr_data.get('table', '')
                expression = expr_data.get('expression', '')
                if expression:
                    m_cols = extract_m_expression_columns(expression, expr_table)
                    for col in m_cols:
                        _mark_used(expr_table, col, 'M Expression')
                        m_col_count += 1
            print(f"   ✅ M expression columns: {m_col_count} column refs")

            # Run XMLA column usage and Report visual scan IN PARALLEL for performance
            parallel_start = time_module.time()
            print(f"\n   ⚡ Running XMLA + Visual scan in parallel...")

            # IMPORTANT: Acquire tokens BEFORE spawning threads
            # Flask session/request context is NOT available inside ThreadPoolExecutor threads
            try:
                fabric_token_for_scan = get_user_fabric_token()
            except Exception as e:
                print(f"   ⚠️  Could not acquire Fabric token: {e}")
                fabric_token_for_scan = None

            # Capture values from request context before entering threads
            _ws_id = workspace_id
            _rpt_id = report_id
            _usr_token = user_token

            visual_result = {}

            def _run_visual_scan():
                try:
                    return scan_report_visual_columns(_ws_id, _rpt_id, _usr_token, fabric_token=fabric_token_for_scan)
                except Exception as e:
                    print(f"   ⚠️  Visual scan failed: {e}")
                    import traceback
                    traceback.print_exc()
                    return {}

            with ThreadPoolExecutor(max_workers=1) as executor:
                visual_future = executor.submit(_run_visual_scan)
                try:
                    visual_result = visual_future.result(timeout=120)
                except TimeoutError:
                    print(f"   ⚠️  Visual scan timed out after 120s — continuing without visual column data")
                    visual_result = {}
                except Exception as e:
                    print(f"   ⚠️  Visual scan future error: {e}")
                    visual_result = {}

            parallel_elapsed = time_module.time() - parallel_start
            print(f"   ⏱️ Visual scan took {parallel_elapsed:.1f}s")

            # Merge visual scan results
            if visual_result:
                vis_col_count = sum(len(v) for v in visual_result.values())
                print(f"   ✅ Visual scan found {vis_col_count} columns across {len(visual_result)} tables")
                for table, columns in visual_result.items():
                    for col in columns:
                        _mark_used(table, col, 'Report Visual')
                # Log sample visual scan tables for debugging
                for tbl, cols in list(visual_result.items())[:3]:
                    sample_cols = list(cols)[:5]
                    print(f"      📋 Visual: {tbl} → {sample_cols}{'...' if len(cols) > 5 else ''}")
            else:
                print(f"   ⚠️  Visual scan returned no results — using measure dependency fallback")
                print(f"      Fabric token available: {fabric_token_for_scan is not None}")

                # FALLBACK: When visual scan fails, mark columns that are referenced
                # by measures as "Report (via Measure)" since measures exist to be used
                # in visuals. Also mark all non-hidden columns from the model.
                fallback_count = 0
                measures = model.get('measures', [])
                dax_pattern_fb = r"(?:'([^']+)'|(\w+))\[([^\]]+)\]"
                for measure in measures:
                    m_expr = measure.get('expression', '')
                    m_table = measure.get('table', '')
                    if m_expr:
                        matches = re.findall(dax_pattern_fb, m_expr)
                        for match in matches:
                            ref_table = match[0] if match[0] else match[1]
                            ref_col = match[2]
                            _mark_used(ref_table, ref_col, 'Report (via Measure)')
                            fallback_count += 1
                    # Mark the measure itself as used in its table
                    if m_table and measure.get('name'):
                        _mark_used(m_table, measure.get('name'), 'Report (via Measure)')
                        fallback_count += 1
                print(f"   ✅ Measure fallback added {fallback_count} column refs")

            total_usage = sum(len(v) for v in column_usage.values())
            print(f"   ✅ Column usage map after all sources: {total_usage} columns across {len(column_usage)} tables")

            # Process each expression (M query)
            expr_start = time_module.time()
            for expr_data in model.get('expressions', []):
                query_name = expr_data.get('table', 'Unknown Query')
                expression = expr_data.get('expression', '')

                # Skip tables with dummy/null M expressions (these are measure-only tables)
                if not expression or expression.strip().lower() in ['null', '#"null"', ''] or len(expression.strip()) < 20:
                    print(f"   ⏭️  Skipping table '{query_name}' - has dummy M expression (probably measure-only table)")
                    continue

                # Parse the M expression to find table references
                tables_used = parse_m_expression_for_tables(expression, all_table_names)

                # Exclude self-references
                tables_used = [t for t in tables_used if t != query_name]

                # Determine query type and source type from the M expression
                query_type, source_type = analyze_m_expression(expression)

                # For Expression type, extract just the source line
                display_expression = expression
                if source_type == 'Expression':
                    display_expression = extract_source_line(expression)

                # Extract server/source name
                server_name = extract_server_name(expression, source_type, workspace_map)

                # Build column info from the Power BI model
                query_columns = all_columns.get(query_name, [])
                column_info = [{
                    'name': col.get('name'),
                    'dataType': col.get('dataType', 'Unknown'),
                    'isReferenced': False,
                    'usedIn': '',
                    'columnType': col.get('columnType', 'Data'),
                    'expression': col.get('expression', '') if col.get('columnType') == 'Calculated' else ''
                } for col in query_columns if col.get('name')]

                # Add measures from this table
                table_measures = [m for m in model.get('measures', []) if m.get('table') == query_name]
                measure_info = [{
                    'name': m.get('name'),
                    'dataType': 'Measure',
                    'isReferenced': False,
                    'usedIn': '',
                    'columnType': 'Measure',
                    'expression': m.get('expression', ''),
                    'description': m.get('description', '')
                } for m in table_measures]

                # Build the tablesWithColumns structure
                # Use the Power BI Model table name (query_name), not SQL source table names
                tables_with_columns = [{
                    'tableName': query_name,  # Power BI Model table name
                    'sqlSourceTables': tables_used,  # SQL source tables used by this query
                    'columns': column_info + measure_info  # Include both columns and measures
                }]

                # Extract SQL query from M expression for display
                sql_query = None
                if source_type == 'SQL Server':
                    # Extract SQL query from M expression
                    sql_query_pattern = r'Query\s*=\s*"([^"]*(?:""[^"]*)*)"'
                    sql_matches = re.findall(sql_query_pattern, expression, re.IGNORECASE)
                    if sql_matches:
                        # Clean up the SQL query (remove M escape sequences)
                        sql_query = sql_matches[0].replace('""', '"').replace('#(lf)', '\n').replace('#(cr)', '\r').replace('#(tab)', '\t')

                queries.append({
                    'queryName': query_name,
                    'tables': tables_used,
                    'tablesWithColumns': tables_with_columns,  # NEW: table-to-columns mapping
                    'queryType': query_type,
                    'sourceType': source_type,
                    'expression': display_expression,  # Source line for Expression, full for others
                    'serverName': server_name,  # Server/source identifier
                    'sqlQuery': sql_query  # NEW: Full SQL query for SQL Server sources
                })

            # Also add calculated tables (tables without M expressions)
            # These include DAX-calculated tables (created with CALENDAR, CALENDARAUTO, etc.)
            tables_with_expressions = set([q['queryName'] for q in queries])

            # Build a map of DAX table expressions from the model
            # These come from the Scanner API's partition/source extraction
            dax_table_expressions = {}
            for expr_data in model.get('expressions', []):
                if expr_data.get('expressionType') == 'DAX':
                    table_name = expr_data.get('table', '')
                    expression = expr_data.get('expression', '')
                    if table_name and expression:
                        dax_table_expressions[table_name] = expression

            print(f"   📊 Found DAX table expressions for {len(dax_table_expressions)} calculated table(s)")
            if dax_table_expressions:
                print(f"      Tables with DAX expressions: {list(dax_table_expressions.keys())}")

            print(f"   🔍 Checking for calculated tables...")
            print(f"      Total tables in model: {len(all_table_names)}")
            print(f"      Tables with M expressions: {len(tables_with_expressions)}")
            print(f"      Tables to add as calculated: {len(all_table_names - tables_with_expressions)}")
            if all_table_names - tables_with_expressions:
                print(f"      Calculated table names: {list(all_table_names - tables_with_expressions)[:10]}")  # Show first 10

            for table_name in all_table_names:
                if table_name not in tables_with_expressions:
                    # Check if this table has a DAX expression (e.g., Calendar table)
                    dax_expr = dax_table_expressions.get(table_name, '')

                    # Determine if it's a Calendar/Date table
                    source_type = 'Calculated Table'
                    query_type = 'Calculated Table'
                    if dax_expr:
                        dax_upper = dax_expr.upper()
                        if 'CALENDAR' in dax_upper:
                            query_type = 'Calendar Table (DAX)'
                            source_type = 'DAX Function'
                        elif 'GENERATE' in dax_upper or 'ADDCOLUMNS' in dax_upper:
                            query_type = 'DAX Calculated Table'
                            source_type = 'DAX Expression'

                    # Build column info from the Power BI model
                    query_columns = all_columns.get(table_name, [])
                    column_info = [{
                        'name': col.get('name'),
                        'dataType': col.get('dataType', 'Unknown'),
                        'isReferenced': False,
                        'usedIn': '',
                        'columnType': col.get('columnType', 'Data'),
                        'expression': col.get('expression', '') if col.get('columnType') == 'Calculated' else ''
                    } for col in query_columns if col.get('name')]

                    # Add measures from this table
                    table_measures = [m for m in model.get('measures', []) if m.get('table') == table_name]
                    measure_info = [{
                        'name': m.get('name'),
                        'dataType': 'Measure',
                        'isReferenced': False,
                        'usedIn': '',
                        'columnType': 'Measure',
                        'expression': m.get('expression', ''),
                        'description': m.get('description', '')
                    } for m in table_measures]

                    # Build the tablesWithColumns structure
                    tables_with_columns = [{
                        'tableName': table_name,  # Power BI Model table name
                        'sqlSourceTables': [],  # No SQL sources for calculated tables
                        'columns': column_info + measure_info  # Include both columns and measures
                    }]

                    queries.append({
                        'queryName': table_name,
                        'tables': [],
                        'tablesWithColumns': tables_with_columns,  # NOW INCLUDES COLUMNS!
                        'queryType': query_type,
                        'sourceType': source_type,
                        'expression': dax_expr,  # DAX expression for Calendar/Calculated tables
                        'serverName': 'N/A',
                        'sqlQuery': ''
                    })

            expr_elapsed = time_module.time() - expr_start
            print(f"   ⏱️ Expression processing took {expr_elapsed:.1f}s — {len(queries)} queries/tables, {sum(len(q['tables']) for q in queries)} dependencies")

            # Get relationships from the model
            relationships = model.get('relationships', [])
            print(f"   🔗 Scanner returned {len(relationships)} relationships")

            # ⚡ PERFORMANCE OPTIMIZATION: XMLA only used as fallback when Scanner lacks data
            # If Scanner API didn't return relationships, try XMLA endpoint as fallback
            if not relationships:
                print(f"   ⚡ XMLA fallback for relationships...")
                try:
                    from xmla_connector import XMLAConnector
                    xmla = XMLAConnector(workspace_id, dataset_id, user_token)
                    xmla_result = xmla.get_model_metadata()

                    if xmla_result.get('relationships'):
                        relationships = xmla_result['relationships']
                        print(f"   ✅ XMLA fallback: {len(relationships)} relationships")
                    else:
                        print(f"   ℹ️  XMLA fallback: no relationships found")
                except ImportError:
                    print(f"   ⚠️  XMLA connector not available")
                except Exception as xmla_error:
                    print(f"   ⚠️  XMLA fallback error: {xmla_error}")

            print(f"   📋 Final: {len(relationships)} relationships")

            # Extract table schema for diagram visualization
            table_schemas = {}
            columns_dict = model.get('columns', {})

            for table_name, columns in columns_dict.items():
                if columns:
                    table_schemas[table_name] = {
                        'name': table_name,
                        'columns': [
                            {
                                'name': col.get('name'),
                                'dataType': col.get('dataType', 'Unknown')
                            } for col in columns if col.get('name')
                        ]
                    }

            print(f"   📊 Extracted schema for {len(table_schemas)} tables")

            # Create a mapping from internal table names to user-friendly query names
            # This helps replace technical names like "LocalDateTable_xxx" with actual query names
            table_name_mapping = {}

            # First, add all tables with M expressions
            for expr_data in model.get('expressions', []):
                query_name = expr_data.get('table', '')
                if query_name:
                    # The 'table' field is the internal table name in the model
                    table_name_mapping[query_name] = query_name

            # Also add all tables from the columns dictionary (this includes auto-generated tables)
            for table_name in columns_dict.keys():
                if table_name not in table_name_mapping:
                    # For LocalDateTable_xxx, create a friendly name
                    if table_name.startswith('LocalDateTable_'):
                        # Extract the GUID and create a short friendly name
                        friendly_name = f"Date Table (Auto)"
                        table_name_mapping[table_name] = friendly_name
                    elif table_name.startswith('DateTableTemplate_'):
                        friendly_name = f"Date Table Template"
                        table_name_mapping[table_name] = friendly_name
                    else:
                        # For other tables, use the name as-is
                        table_name_mapping[table_name] = table_name

            print(f"   📝 Created table name mapping for {len(table_name_mapping)} tables (including auto-generated)")

            # Apply the mapping to relationships and filter out auto-generated date tables
            if relationships:
                filtered_relationships = []
                seen_pairs = set()  # Track unique relationship pairs to avoid duplicates

                for rel in relationships:
                    original_from = rel.get('fromTable', '')
                    original_to = rel.get('toTable', '')

                    # Skip relationships involving LocalDateTable or DateTableTemplate
                    if (original_from.startswith('LocalDateTable_') or
                        original_from.startswith('DateTableTemplate_') or
                        original_to.startswith('LocalDateTable_') or
                        original_to.startswith('DateTableTemplate_')):
                        continue

                    # Replace with friendly names if available, otherwise keep original
                    if original_from in table_name_mapping:
                        rel['fromTable'] = table_name_mapping[original_from]
                    if original_to in table_name_mapping:
                        rel['toTable'] = table_name_mapping[original_to]

                    # Create a unique key for this relationship pair (sorted to catch bidirectional duplicates)
                    from_table = rel.get('fromTable', '')
                    to_table = rel.get('toTable', '')
                    from_col = rel.get('fromColumn', '')
                    to_col = rel.get('toColumn', '')

                    # Create normalized pair (alphabetically sorted to catch A→B and B→A as same)
                    pair_key = tuple(sorted([
                        f"{from_table}.{from_col}",
                        f"{to_table}.{to_col}"
                    ]))

                    # Skip if we've already seen this relationship pair
                    if pair_key in seen_pairs:
                        continue

                    seen_pairs.add(pair_key)
                    filtered_relationships.append(rel)

                relationships = filtered_relationships
                print(f"   🔄 Filtered to {len(relationships)} unique relationships")

            # Mark columns used in relationships as "Used"
            if relationships:
                for rel in relationships:
                    from_table = rel.get('fromTable', '')
                    from_col = rel.get('fromColumn', '')
                    to_table = rel.get('toTable', '')
                    to_col = rel.get('toColumn', '')
                    _mark_used(from_table, from_col, 'Relationship')
                    _mark_used(to_table, to_col, 'Relationship')

            # Additional heuristic: Only mark calculated columns not already detected
            heuristic_count = 0

            for table_name, table_columns in all_columns.items():
                for col in table_columns:
                    col_type = col.get('columnType', '')

                    # Calculated columns are always actively used (they were created for a reason)
                    if col_type == 'Calculated':
                        if col.get('name') not in column_usage.get(table_name, {}):
                            _mark_used(table_name, col.get('name'), 'Calculated Column')
                            heuristic_count += 1

            print(f"   ✅ Heuristics added {heuristic_count} calculated columns")

            # ===== FINAL PASS: Apply column_usage to all queries' column info =====
            # Build a case-insensitive lookup for column_usage (merging sets)
            column_usage_lower = {}
            for tbl, cols in column_usage.items():
                tbl_lower = tbl.lower()
                if tbl_lower not in column_usage_lower:
                    column_usage_lower[tbl_lower] = {}
                for col_name_key, sources in cols.items():
                    col_key_lower = col_name_key.lower()
                    if col_key_lower not in column_usage_lower[tbl_lower]:
                        column_usage_lower[tbl_lower][col_key_lower] = set()
                    column_usage_lower[tbl_lower][col_key_lower].update(sources)

            used_count = 0
            total_count = 0
            for query in queries:
                query_name = query.get('queryName', '')
                for twc in query.get('tablesWithColumns', []):
                    twc_table = twc.get('tableName', '')
                    for col in twc.get('columns', []):
                        col_name = col.get('name', '')
                        col_name_lower = col_name.lower()
                        total_count += 1

                        # Collect all usage sources across matching strategies
                        all_sources = set()

                        # 1. Exact match by query_name
                        if query_name in column_usage:
                            sources = column_usage[query_name].get(col_name)
                            if sources:
                                all_sources.update(sources)

                        # 2. Exact match by twc tableName
                        if twc_table and twc_table in column_usage:
                            sources = column_usage[twc_table].get(col_name)
                            if sources:
                                all_sources.update(sources)

                        # 3. Case-insensitive match by query_name
                        tbl_cols = column_usage_lower.get(query_name.lower(), {})
                        sources = tbl_cols.get(col_name_lower)
                        if sources:
                            all_sources.update(sources)

                        # 4. Case-insensitive match by twc tableName
                        if twc_table:
                            tbl_cols = column_usage_lower.get(twc_table.lower(), {})
                            sources = tbl_cols.get(col_name_lower)
                            if sources:
                                all_sources.update(sources)

                        # 5. Direct check against Scanner model's isReferenced flag
                        if not all_sources:
                            model_cols = all_columns.get(query_name, [])
                            for mc in model_cols:
                                if mc.get('name', '').lower() == col_name_lower and mc.get('isReferenced') is True:
                                    all_sources.add('Scanner API')
                                    break

                        # 6. Partial/suffix table name matching (handles schema.table vs table)
                        if not all_sources:
                            query_name_lower = query_name.lower()
                            twc_table_lower = twc_table.lower() if twc_table else ''
                            for usage_tbl, usage_cols in column_usage_lower.items():
                                if col_name_lower in usage_cols:
                                    matched = False
                                    if (query_name_lower.endswith('.' + usage_tbl) or
                                        usage_tbl.endswith('.' + query_name_lower) or
                                        (twc_table_lower and (twc_table_lower.endswith('.' + usage_tbl) or
                                         usage_tbl.endswith('.' + twc_table_lower)))):
                                        matched = True
                                    if not matched:
                                        usage_suffix = usage_tbl.rsplit('.', 1)[-1]
                                        query_suffix = query_name_lower.rsplit('.', 1)[-1]
                                        twc_suffix = twc_table_lower.rsplit('.', 1)[-1] if twc_table_lower else ''
                                        if usage_suffix and (usage_suffix == query_suffix or usage_suffix == twc_suffix):
                                            matched = True
                                    if matched:
                                        all_sources.update(usage_cols[col_name_lower])
                                        break

                        is_used = len(all_sources) > 0
                        col['isReferenced'] = is_used
                        col['usedIn'] = ', '.join(sorted(all_sources)) if all_sources else ''
                        if is_used:
                            used_count += 1

            total_elapsed = time_module.time() - lineage_start_time
            # Count columns with usedIn populated
            used_in_count = sum(1 for q in queries for twc in q.get('tablesWithColumns', []) for c in twc.get('columns', []) if c.get('usedIn'))
            print(f"   ✅ Final: {used_count}/{total_count} columns marked as Used, {used_in_count} with usedIn labels — Total lineage time: {total_elapsed:.1f}s")

            # NEW: Detect and categorize datasets (Primary, Composite, DirectQuery, Live)
            datasets_info = []
            directquery_as_models = []

            try:
                print(f"\n   🔍 Detecting dataset types and composite models...")

                # Ensure we have a scanner instance
                if 'scanner' not in locals():
                    scanner = PowerBIScanner()

                # Get full dataset metadata from Scanner API
                scan_data = scanner.run_scan(workspace_id=workspace_id)

                if scan_data and "workspaces" in scan_data:
                    for ws in scan_data["workspaces"]:
                        # Find all datasets related to this report
                        for dataset in ws.get("datasets", []):
                            if dataset.get("id") == dataset_id:
                                # This is the primary dataset
                                dataset_name = dataset.get('name', 'Unknown Dataset')
                                upstream_datasets = dataset.get('upstreamDatasets', [])
                                datasources = dataset.get('datasources', [])
                                tables_count = len(dataset.get('tables', []))

                                print(f"   📊 Primary Dataset: {dataset_name}")
                                print(f"      - Tables: {tables_count}")
                                print(f"      - Datasources: {len(datasources)}")
                                print(f"      - Upstream Datasets: {len(upstream_datasets)}")

                                # Check for DirectQuery to Analysis Services
                                has_as_live = False
                                for ds in datasources:
                                    ds_type = ds.get('datasourceType', '')
                                    connection_details = ds.get('connectionDetails', {})

                                    # Check for Analysis Services connection
                                    if ds_type in ['AnalysisServices', 'AnalysisServicesDatabase']:
                                        server = connection_details.get('server', 'Unknown Server')
                                        database = connection_details.get('database', 'Unknown Database')

                                        directquery_as_models.append({
                                            'type': 'DirectQuery to Analysis Services',
                                            'server': server,
                                            'database': database,
                                            'connection_mode': ds.get('connectionMode', 'DirectQuery')
                                        })
                                        has_as_live = True
                                        print(f"      🔗 DirectQuery AS: {server}/{database}")

                                # Add primary dataset to list
                                dataset_type = 'Composite Model' if upstream_datasets else 'Import'
                                if has_as_live:
                                    dataset_type = 'DirectQuery/Live'

                                datasets_info.append({
                                    'id': dataset_id,
                                    'name': dataset_name,
                                    'isPrimary': True,
                                    'type': dataset_type,
                                    'tables': tables_count,
                                    'configuredBy': dataset.get('configuredBy', 'Unknown')
                                })

                                # Add upstream datasets (composite model)
                                if upstream_datasets:
                                    print(f"   🔗 Composite Model detected with {len(upstream_datasets)} upstream dataset(s)")
                                    for upstream in upstream_datasets:
                                        upstream_id = upstream.get('targetDatasetId')
                                        upstream_workspace_id = upstream.get('targetWorkspaceId', workspace_id)

                                        # Try to find upstream dataset details
                                        upstream_name = upstream_id  # Fallback to ID
                                        upstream_tables = 0

                                        for upstream_ws in scan_data.get("workspaces", []):
                                            if upstream_ws.get('id') == upstream_workspace_id:
                                                for upstream_ds in upstream_ws.get('datasets', []):
                                                    if upstream_ds.get('id') == upstream_id:
                                                        upstream_name = upstream_ds.get('name', upstream_id)
                                                        upstream_tables = len(upstream_ds.get('tables', []))
                                                        break

                                        datasets_info.append({
                                            'id': upstream_id,
                                            'name': upstream_name,
                                            'isPrimary': False,
                                            'type': 'Composite (Upstream)',
                                            'tables': upstream_tables,
                                            'configuredBy': 'Unknown'
                                        })
                                        print(f"      → Upstream: {upstream_name} ({upstream_tables} tables)")

                                break

                print(f"   ✅ Dataset categorization complete: {len(datasets_info)} dataset(s), {len(directquery_as_models)} DirectQuery AS model(s)")

            except Exception as dataset_error:
                print(f"   ⚠️ Error detecting datasets: {str(dataset_error)}")
                import traceback
                traceback.print_exc()
                # Continue without dataset info - don't fail the entire request

            return jsonify({
                'success': True,
                'queries': queries,
                'relationships': relationships,
                'tableSchemas': table_schemas,
                'datasets': datasets_info,
                'directQueryASModels': directquery_as_models
            })

        except ImportError:
            print("   ⚠️ Scanner connector not available")
            return jsonify({
                'success': False,
                'error': 'Scanner API not available. Admin permissions required.'
            }), 500
        except Exception as scan_error:
            print(f"   ⚠️ Scanner API error: {str(scan_error)}")
            import traceback
            traceback.print_exc()
            return jsonify({
                'success': False,
                'error': f'Scanner API error: {str(scan_error)}'
            }), 500

    except Exception as e:
        print(f"❌ Error fetching lineage: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/report/visual-lineage/<report_id>')
@login_required
def get_visual_lineage(report_id):
    """
    Get visual-level lineage for a report showing Pages -> Visuals -> Data Points -> Tables -> Data Sources

    Returns a hierarchical structure showing:
    - Report pages
    - Visuals on each page
    - Fields (columns/measures) used in each visual
    - Source tables for those fields
    - Upstream data sources
    """
    try:
        from scanner_connector import PowerBIScanner
        import re

        workspace_id = request.args.get('workspace_id')

        if not workspace_id:
            return jsonify({
                'success': False,
                'error': 'workspace_id is required'
            }), 400

        print(f"\n📊 ===============================================")
        print(f"📊 VISUAL LINEAGE REQUEST")
        print(f"📊 ===============================================")
        print(f"   Workspace ID: {workspace_id}")
        print(f"   Report ID: {report_id}")

        # Initialize Scanner API
        scanner = PowerBIScanner()
        scanner.access_token = scanner.get_access_token()

        # Run scan to get visual metadata
        print("   📊 Running Scanner API scan to get visual metadata...")
        scan_data = scanner.run_scan(workspace_id=workspace_id)

        if not scan_data or "workspaces" not in scan_data:
            return jsonify({
                'success': False,
                'error': 'Failed to scan workspace'
            }), 500

        # Find the report in scan results
        report_data = None
        dataset_id = None
        dataset_metadata = {}

        for ws in scan_data["workspaces"]:
            # Build dataset metadata map
            for dataset in ws.get("datasets", []):
                ds_id = dataset.get("id")
                if ds_id:
                    dataset_metadata[ds_id] = {
                        'id': ds_id,
                        'name': dataset.get('name', 'Unknown Dataset'),
                        'tables': dataset.get('tables', []),
                        'datasources': dataset.get('datasources', [])
                    }

            # Find the report
            for report in ws.get("reports", []):
                if report.get("id") == report_id:
                    report_data = report
                    dataset_id = report.get('datasetId')
                    break

            if report_data:
                break

        if not report_data:
            return jsonify({
                'success': False,
                'error': 'Report not found in workspace'
            }), 404

        # Get dataset metadata
        dataset_info = dataset_metadata.get(dataset_id, {})
        tables = dataset_info.get('tables', [])
        datasources = dataset_info.get('datasources', [])

        # Build table-to-datasource mapping AND extract SQL source tables
        table_to_datasource = {}
        table_to_sql_sources = {}  # Map Power Query tables to their SQL source tables
        column_to_sql_source = {}  # NEW: Map individual columns to their exact SQL source table

        for table in tables:
            table_name = table.get('name')
            if table_name:
                # Extract datasource from table source expression
                source = table.get('source', [])
                if source and len(source) > 0:
                    expr = source[0].get('expression', '')
                    # Try to extract datasource info from M query
                    datasource_info = extract_datasource_from_expression(expr, datasources)
                    table_to_datasource[table_name] = datasource_info

                    # NEW: Parse SQL query to extract column-to-table mapping
                    import re

                    # Extract SQL query from M expression
                    # Pattern: Query="SELECT ..."
                    query_match = re.search(r'Query\s*=\s*"([^"]+)"', expr, re.IGNORECASE | re.DOTALL)
                    if query_match:
                        sql_query = query_match.group(1)

                        # Normalize SQL for easier parsing
                        sql_query = sql_query.replace('#(lf)', '\n').replace('#(tab)', '\t')

                        print(f"\n      🔍 Parsing SQL for {table_name}:")
                        print(f"         SQL Preview: {sql_query[:200]}...")

                        # Parse SELECT clause to extract column mappings
                        import re

                        # Normalize SQL for easier parsing
                        sql_normalized = sql_query.replace('#(lf)', ' ').replace('#(tab)', ' ')

                        # Extract table aliases from FROM/JOIN clauses FIRST
                        # Pattern: FROM schema.table alias or JOIN schema.table alias
                        # Also handle: FROM schema.table AS alias
                        alias_patterns = [
                            r'(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)\s+(?:AS\s+)?([a-zA-Z_][a-zA-Z0-9_]*)',
                            r'(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)\s*\n'  # No alias case
                        ]

                        # Build alias-to-table mapping
                        alias_to_table = {}
                        sql_tables_found = set()

                        for pattern in alias_patterns:
                            matches = re.findall(pattern, sql_normalized, re.IGNORECASE)
                            for match in matches:
                                if len(match) == 2:
                                    full_table, alias = match
                                    if alias and alias.strip() and alias.upper() not in ['ON', 'WHERE', 'SELECT', 'AND', 'OR']:
                                        alias_to_table[alias.lower()] = full_table
                                        sql_tables_found.add(full_table)
                                        print(f"         🏷️  Alias: {alias} → {full_table}")
                                elif len(match) == 1:
                                    sql_tables_found.add(match[0])

                        table_to_sql_sources[table_name] = list(sql_tables_found)

                        # Now parse column mappings with multiple patterns
                        # Pattern 1: alias.column AS [name]
                        pattern1 = r'([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s+\[([^\]]+)\]'
                        matches1 = re.findall(pattern1, sql_normalized, re.IGNORECASE)

                        for alias_or_table, sql_column, pbi_column in matches1:
                            # Resolve alias to actual table
                            if alias_or_table.lower() in alias_to_table:
                                source_table = alias_to_table[alias_or_table.lower()]
                                key = (table_name, pbi_column)
                                column_to_sql_source[key] = source_table
                                print(f"         ✓ {pbi_column} → {source_table}.{sql_column}")
                            else:
                                # Might be schema.table.column or just table.column - need to check
                                # Try to find in sql_tables_found
                                matching_tables = [t for t in sql_tables_found if t.endswith(f'.{alias_or_table}') or t == alias_or_table]
                                if matching_tables:
                                    key = (table_name, pbi_column)
                                    column_to_sql_source[key] = matching_tables[0]
                                    print(f"         ✓ {pbi_column} → {matching_tables[0]}.{sql_column}")

                        # Pattern 2: schema.table.column AS [name] (less common but possible)
                        pattern2 = r'([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s+\[([^\]]+)\]'
                        matches2 = re.findall(pattern2, sql_normalized, re.IGNORECASE)

                        for schema, table, sql_column, pbi_column in matches2:
                            source_table = f"{schema}.{table}"
                            key = (table_name, pbi_column)
                            column_to_sql_source[key] = source_table
                            print(f"         ✓ {pbi_column} → {source_table}.{sql_column}")

                        # Pattern 3: column AS [name] (no table prefix - try to infer)
                        pattern3 = r'(?<![.\w])([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s+\[([^\]]+)\]'
                        matches3 = re.findall(pattern3, sql_normalized, re.IGNORECASE)

                        for sql_column, pbi_column in matches3:
                            # Only process if not already mapped
                            key = (table_name, pbi_column)
                            if key not in column_to_sql_source:
                                if len(sql_tables_found) == 1:
                                    # Only one table, safe to assume
                                    column_to_sql_source[key] = list(sql_tables_found)[0]
                                    print(f"         ✓ {pbi_column} → {list(sql_tables_found)[0]}.{sql_column} (single table)")
                                else:
                                    # Multiple tables with unprefixed column - cannot determine exact source
                                    # Don't add to column_to_sql_source - let it fall back to showing all tables
                                    print(f"         ⚠️  {pbi_column} → {sql_column} (unprefixed - cannot determine specific table)")

                        print(f"         📊 Mapped {len([k for k in column_to_sql_source if k[0] == table_name])} columns to specific tables")

        # Build column/measure to table mapping
        field_to_table = {}
        for table in tables:
            table_name = table.get('name')

            # Map columns
            for col in table.get('columns', []):
                col_name = col.get('name')
                if col_name:
                    field_to_table[col_name] = {
                        'table': table_name,
                        'type': 'column',
                        'dataType': col.get('dataType', 'Unknown')
                    }

            # Map measures
            for measure in table.get('measures', []):
                measure_name = measure.get('name')
                if measure_name:
                    field_to_table[measure_name] = {
                        'table': table_name,
                        'type': 'measure',
                        'expression': measure.get('expression', '')
                    }

        # Process visual data
        pages_data = []
        pages = report_data.get('pages', [])
        extraction_method = 'scanner'
        visual_fallback_error = None

        print(f"   ✅ Found {len(pages)} pages in report from Scanner API")

        # FALLBACK: If Scanner API doesn't return visual data, use Playwright extractor
        if not pages or len(pages) == 0:
            print(f"   ⚠️  Scanner API did not return visual metadata for this report")
            print(f"   🔄 FALLBACK: Attempting to extract visuals using Playwright...")
            extraction_method = 'playwright'

            try:
                from visual_metadata_extractor import VisualMetadataExtractor
                import asyncio
                import pickle
                import os
                from datetime import datetime

                # Check cache first
                cache_dir = '.visual_cache'
                os.makedirs(cache_dir, exist_ok=True)
                cache_file = os.path.join(cache_dir, f"{report_id}.pkl")

                visual_result = None

                # Try cache (valid for 24 hours)
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file, 'rb') as f:
                            cached_data = pickle.load(f)
                            cached_time = datetime.fromisoformat(cached_data.get('cached_at', '2000-01-01'))
                            age_hours = (datetime.now() - cached_time).total_seconds() / 3600

                            if age_hours < 24:
                                print(f"      ✅ Using cached visual data (age: {age_hours:.1f}h)")
                                visual_result = cached_data
                    except Exception as e:
                        print(f"      ⚠️  Cache read failed: {e}")

                # If no cache, extract visuals using Playwright
                if not visual_result:
                    print(f"      🌐 Launching browser to extract visuals...")

                    # Get user's SSO access token
                    user_token = session.get('access_token')

                    print(f"      🔐 Using user's SSO token for extraction...")

                    # Initialize extractor with user's token
                    extractor = VisualMetadataExtractor(user_token=user_token)

                    # Run async extraction
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    visual_result = loop.run_until_complete(
                        extractor.extract_visuals(workspace_id, report_id, timeout=90)
                    )
                    loop.close()

                    # Cache the result
                    if visual_result.get('success'):
                        visual_result['cached_at'] = datetime.now().isoformat()
                        with open(cache_file, 'wb') as f:
                            pickle.dump(visual_result, f)
                        print(f"      ✅ Visual data cached")

                # Process Playwright results
                if visual_result and visual_result.get('success'):
                    pages = visual_result.get('pages', [])
                    print(f"      ✅ Playwright extracted {len(pages)} page(s)")
                else:
                    visual_fallback_error = (visual_result.get('error') if visual_result else None) or 'Playwright extraction returned no result'
                    print(f"      ❌ Playwright extraction failed: {visual_fallback_error}")

            except Exception as e:
                visual_fallback_error = str(e)
                print(f"      ❌ Error during Playwright extraction: {str(e)}")
                import traceback
                traceback.print_exc()

        for page in pages:
            page_name = page.get('displayName', 'Unnamed Page')
            page_ordinal = page.get('ordinal', 0)
            visuals = page.get('visuals', [])

            visuals_data = []

            for visual in visuals:
                visual_name = visual.get('name', 'Unnamed Visual')
                visual_title = visual.get('title', '').strip()  # Remove whitespace
                visual_type = visual.get('visualType') or visual.get('type', 'unknown')

                # If no title is set, use a readable format instead of showing object name
                if not visual_title:
                    # Use visual type as fallback (more readable than "VisualContainer1")
                    visual_title = visual_type.replace('Visual', '').replace('Chart', ' Chart').strip() or 'Unnamed Visual'

                # Extract fields used in the visual
                fields_used = []

                # METHOD 1: Check if visual has 'fields' array (Playwright format)
                playwright_fields = visual.get('fields', [])
                if playwright_fields:
                    print(f"      📊 Processing visual '{visual_title or visual_name}' with {len(playwright_fields)} fields (Playwright format)")

                    for field_obj in playwright_fields:
                        # Handle both formats: string or dict
                        if isinstance(field_obj, str):
                            field_name = field_obj
                            field_display_name = field_obj
                            field_type_hint = 'unknown'
                            playwright_table = None
                        elif isinstance(field_obj, dict):
                            field_name = field_obj.get('name', '')
                            field_display_name = field_obj.get('displayName', field_name)
                            field_type_hint = field_obj.get('type', 'unknown')  # 'Column', 'Measure', etc.
                            playwright_table = field_obj.get('table', '')  # CRITICAL: Get table from Playwright!
                        else:
                            continue

                        # PRIORITY 1: Use table name from Playwright (most accurate!)
                        if playwright_table:
                            table_name = playwright_table
                            datasource = table_to_datasource.get(table_name, {})

                            # Try to get more metadata from dataset if available
                            field_info = field_to_table.get(field_name, {})

                            # Determine field type
                            if field_type_hint in ['Measure', 'Aggregation']:
                                field_type = 'measure'
                            elif field_type_hint == 'Column':
                                field_type = 'column'
                            elif field_info:
                                field_type = field_info.get('type', 'column')
                            else:
                                field_type = 'column'

                            # NEW: Precise column-to-source-table mapping
                            # Check if we have a specific SQL source table for this column
                            column_key = (table_name, field_name)
                            specific_sql_source = column_to_sql_source.get(column_key)

                            if specific_sql_source:
                                # We know the EXACT source table for this column!
                                display_table_name = specific_sql_source
                                print(f"         ✅ Precise mapping: {field_name} → {specific_sql_source}")
                            else:
                                # Fall back to showing all SQL sources for this Power Query table
                                sql_sources = table_to_sql_sources.get(table_name, [])
                                if sql_sources:
                                    if len(sql_sources) == 1:
                                        # Only one source table, use it
                                        display_table_name = sql_sources[0]
                                    else:
                                        # Multiple sources, can't determine which one
                                        display_table_name = ', '.join(sql_sources)
                                else:
                                    # No SQL sources found, use Power Query table name
                                    display_table_name = table_name

                            fields_used.append({
                                'field_name': field_name,
                                'field_type': field_type,
                                'table_name': display_table_name,  # Precise SQL source or Power Query table
                                'model_table_name': table_name,  # Power BI model table (e.g., Query1)
                                'data_type': field_info.get('dataType', 'Unknown') if field_info else 'Unknown',
                                'datasource_type': datasource.get('type', 'Unknown'),
                                'datasource_server': datasource.get('server', 'N/A'),
                                'datasource_database': datasource.get('database', 'N/A'),
                                'expression': field_info.get('expression', '') if field_info and field_type == 'measure' else ''
                            })
                        else:
                            # FALLBACK: Try to find this field in our dataset metadata
                            field_info = field_to_table.get(field_name, {})

                            if field_info:
                                table_name = field_info.get('table')
                                datasource = table_to_datasource.get(table_name, {})
                                field_type = field_info.get('type', 'column')

                                fields_used.append({
                                    'field_name': field_name,
                                    'field_type': field_type,
                                    'table_name': table_name,
                                    'data_type': field_info.get('dataType'),
                                    'datasource_type': datasource.get('type', 'Unknown'),
                                    'datasource_server': datasource.get('server', 'N/A'),
                                    'datasource_database': datasource.get('database', 'N/A'),
                                    'expression': field_info.get('expression', '') if field_type == 'measure' else ''
                                })
                            else:
                                # Field not found in metadata, add with limited info
                                fields_used.append({
                                    'field_name': field_name,
                                    'field_type': 'unknown',
                                    'table_name': 'N/A',
                                    'data_type': 'Unknown',
                                    'datasource_type': 'Unknown',
                                    'datasource_server': 'N/A',
                                    'datasource_database': 'N/A'
                                })

                # METHOD 2: Parse visual config JSON (Scanner API format)
                else:
                    visual_config = visual.get('config', '')

                    if visual_config:
                        # Extract table and field references from visual config JSON
                        # Pattern: "TableName.FieldName" or references in projections
                        field_pattern = r'"(?:Entity|Table)"\s*:\s*"([^"]+)"[^}]*?"(?:Property|Column|Name)"\s*:\s*"([^"]+)"'
                        matches = re.findall(field_pattern, visual_config)

                        for table_ref, field_ref in matches:
                            field_key = field_ref
                            field_info = field_to_table.get(field_key, {})

                            if field_info:
                                table_name = field_info.get('table')
                                datasource = table_to_datasource.get(table_name, {})
                                field_type = field_info.get('type', 'column')

                                fields_used.append({
                                    'field_name': field_ref,
                                    'field_type': field_type,
                                    'table_name': table_name,
                                    'data_type': field_info.get('dataType'),
                                    'datasource_type': datasource.get('type', 'Unknown'),
                                    'datasource_server': datasource.get('server', 'N/A'),
                                    'datasource_database': datasource.get('database', 'N/A'),
                                    'expression': field_info.get('expression', '') if field_type == 'measure' else ''
                                })

                # Remove duplicates
                unique_fields = []
                seen = set()
                for field in fields_used:
                    key = (field['field_name'], field['table_name'])
                    if key not in seen:
                        seen.add(key)
                        unique_fields.append(field)

                visuals_data.append({
                    'name': visual_name,
                    'title': visual_title,  # Now uses actual title or readable type name
                    'type': visual_type,
                    'fields_count': len(unique_fields),
                    'fields': unique_fields
                })

            pages_data.append({
                'name': page_name,
                'ordinal': page_ordinal,
                'visuals_count': len(visuals_data),
                'visuals': visuals_data
            })

        # Sort pages by ordinal
        pages_data.sort(key=lambda x: x['ordinal'])

        print(f"   ✅ Processed {len(pages_data)} pages with visual lineage data")
        print(f"📊 ===============================================\n")

        response_payload = {
            'success': True,
            'report_id': report_id,
            'report_name': report_data.get('name', 'Unknown Report'),
            'dataset_id': dataset_id,
            'dataset_name': dataset_info.get('name', 'Unknown Dataset'),
            'pages_count': len(pages_data),
            'pages': pages_data,
            'extraction_method': extraction_method
        }

        # Surface the real reason when no pages could be extracted so the
        # frontend can show an actionable message instead of a generic one.
        if not pages_data and visual_fallback_error:
            response_payload['error'] = visual_fallback_error

        return jsonify(response_payload)

    except Exception as e:
        print(f"❌ Error getting visual lineage: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


def extract_datasource_from_expression(expression, datasources):
    """Extract datasource information from M query expression"""
    import re

    datasource_info = {
        'type': 'Unknown',
        'server': 'N/A',
        'database': 'N/A',
        'url': 'N/A'
    }

    if not expression:
        return datasource_info

    # Try to match SQL Server
    sql_match = re.search(r'Sql\.Database\("([^"]+)",\s*"([^"]+)"', expression, re.IGNORECASE)
    if sql_match:
        datasource_info['type'] = 'SQL Server'
        datasource_info['server'] = sql_match.group(1)
        datasource_info['database'] = sql_match.group(2)
        return datasource_info

    # Try to match SharePoint/Web
    web_match = re.search(r'Web\.Contents\("([^"]+)"', expression, re.IGNORECASE)
    if web_match:
        datasource_info['type'] = 'Web/SharePoint'
        datasource_info['url'] = web_match.group(1)
        return datasource_info

    # Try to match Excel file
    excel_match = re.search(r'Excel\.Workbook\(', expression, re.IGNORECASE)
    if excel_match:
        datasource_info['type'] = 'Excel'
        return datasource_info

    # Fallback: check datasources array
    if datasources and len(datasources) > 0:
        ds = datasources[0]
        connection = ds.get('connectionDetails', {})
        datasource_info['type'] = ds.get('datasourceType', 'Unknown')
        datasource_info['server'] = connection.get('server', 'N/A')
        datasource_info['database'] = connection.get('database', 'N/A')
        datasource_info['url'] = connection.get('url', 'N/A')

    return datasource_info


@app.route('/api/lineage/debug')
@login_required
def get_lineage_debug():
    """Debug endpoint to view raw dataset model and expressions"""
    try:
        import requests
        workspace_id = request.args.get('workspace_id')
        report_id = request.args.get('report_id')

        if not workspace_id or not report_id:
            return jsonify({
                'success': False,
                'error': 'workspace_id and report_id are required'
            }), 400

        # Get user token from session
        user_token = session.get('access_token')

        if not user_token:
            return jsonify({
                'success': False,
                'error': 'Not authenticated'
            }), 401

        headers = {
            'Authorization': f'Bearer {user_token}',
            'Content-Type': 'application/json'
        }

        # Get report details to find dataset ID
        report_url = f'https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}'
        report_response = requests.get(report_url, headers=headers)

        if report_response.status_code != 200:
            return jsonify({
                'success': False,
                'error': f'Failed to fetch report: {report_response.status_code}'
            }), report_response.status_code

        report_data = report_response.json()
        dataset_id = report_data.get('datasetId')

        if not dataset_id:
            return jsonify({
                'success': True,
                'message': 'Report has no dataset',
                'model': {}
            })

        # Use Scanner API to get dataset model
        try:
            from scanner_connector import PowerBIScanner
            scanner = PowerBIScanner()

            print(f"   🐛 DEBUG: Fetching dataset model for {dataset_id}...")
            model = scanner.get_dataset_model(dataset_id, workspace_id=workspace_id)

            # Return the raw model for inspection
            return jsonify({
                'success': True,
                'dataset_id': dataset_id,
                'model': {
                    'tables': model.get('tables', []),
                    'expressions_count': len(model.get('expressions', [])),
                    'expressions': model.get('expressions', []),
                    'columns': {k: len(v) for k, v in model.get('columns', {}).items()},
                    'measures_count': len(model.get('measures', [])),
                    'relationships_count': len(model.get('relationships', []))
                }
            })

        except Exception as scan_error:
            return jsonify({
                'success': False,
                'error': f'Scanner API error: {str(scan_error)}'
            }), 500

    except Exception as e:
        print(f"❌ Debug endpoint error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


def extract_sql_table_names(sql_query):
    """
    Extract table names from SQL query text.

    Detects table references in:
    - FROM clauses
    - JOIN clauses (INNER, LEFT, RIGHT, FULL, CROSS)
    - Table-valued functions

    Args:
        sql_query: SQL query string

    Returns:
        Set of table names found in the query
    """
    import re

    if not sql_query:
        return set()

    tables = set()

    # Remove SQL comments
    # -- single line comments
    sql_query = re.sub(r'--[^\n]*', '', sql_query)
    # /* multi-line comments */
    sql_query = re.sub(r'/\*.*?\*/', '', sql_query, flags=re.DOTALL)

    # Pattern for FROM and JOIN clauses
    # Matches: FROM [schema].[table], FROM table, FROM [table]
    # Also: JOIN [schema].[table], INNER JOIN table AS alias

    # Pattern 1: [schema].[table] or [schema].table or schema.[table]
    bracketed_pattern = r'(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:\[([^\]]+)\]\.)?\[?([^\]\s,;)(]+)\]?'
    matches = re.findall(bracketed_pattern, sql_query, re.IGNORECASE)

    for schema, table in matches:
        # Clean up table name
        table = table.strip('[]').strip()
        if table.upper() not in ('SELECT', 'WHERE', 'ON', 'AS', 'AND', 'OR', 'INNER', 'LEFT', 'RIGHT', 'OUTER', 'FULL', 'CROSS'):
            if schema:
                schema = schema.strip('[]').strip()
                tables.add(f"{schema}.{table}")
            else:
                tables.add(table)

    # Pattern 2: Unbracketed schema.table
    # Matches: FROM dbo.TableName, JOIN Sales.FactSales
    dotted_pattern = r'(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)'
    dotted_matches = re.findall(dotted_pattern, sql_query, re.IGNORECASE)

    for match in dotted_matches:
        # Skip common SQL keywords that might match pattern
        if not any(kw in match.upper() for kw in ['INNER.', 'LEFT.', 'RIGHT.', 'OUTER.', 'FULL.']):
            tables.add(match)

    return tables


def extract_sql_column_names(sql_query):
    """
    Extract column names from SQL SELECT clauses.

    Parses SELECT column lists to identify which columns are being queried.
    These columns are definitively "used" since they're pulled from the database.

    Args:
        sql_query: SQL query string (already cleaned of M-code escapes)

    Returns:
        Set of column names found in SELECT clauses
    """
    import re

    if not sql_query:
        return set()

    columns = set()

    # Remove SQL comments
    sql_query = re.sub(r'--[^\n]*', '', sql_query)
    sql_query = re.sub(r'/\*.*?\*/', '', sql_query, flags=re.DOTALL)

    # Extract the SELECT ... FROM portion(s)
    # Handle multiple SELECT statements (subqueries, CTEs)
    select_blocks = re.findall(
        r'SELECT\s+(?:DISTINCT\s+|TOP\s+\d+\s+)?(.*?)(?:\bFROM\b)',
        sql_query, re.IGNORECASE | re.DOTALL
    )

    for block in select_blocks:
        # Skip SELECT * — doesn't tell us specific columns
        if block.strip() == '*':
            continue

        # Split by comma, handling nested parentheses
        depth = 0
        current = ''
        items = []
        for ch in block:
            if ch == '(':
                depth += 1
                current += ch
            elif ch == ')':
                depth -= 1
                current += ch
            elif ch == ',' and depth == 0:
                items.append(current.strip())
                current = ''
            else:
                current += ch
        if current.strip():
            items.append(current.strip())

        for item in items:
            if not item:
                continue

            # Check for AS alias: extract the source column name, not the alias
            # Pattern: [Table].[Column] AS [Alias] or Column AS Alias
            as_match = re.match(r'(.+?)\s+AS\s+\[?([^\]]+)\]?\s*$', item, re.IGNORECASE)
            if as_match:
                col_expr = as_match.group(1).strip()
            else:
                col_expr = item.strip()

            # Extract bracketed column names: [ColumnName]
            bracket_cols = re.findall(r'\[([^\]]+)\]', col_expr)
            for bc in bracket_cols:
                # Skip schema/table-like names (contain dots or are all uppercase table patterns)
                if bc and not re.match(r'^[A-Z_]+\.[A-Z_]+$', bc):
                    columns.add(bc)

            # Extract unbracketed column references: table.column or just column
            # Match alias.column pattern (e.g., BASE.ITNBR, prod.ManufacturingStatus)
            dot_cols = re.findall(r'(?:^|\s|,)([A-Za-z_]\w*\.)([A-Za-z_]\w*)', col_expr)
            for _, col in dot_cols:
                if col.upper() not in ('AS', 'AND', 'OR', 'NOT', 'NULL', 'CAST', 'CASE',
                                       'WHEN', 'THEN', 'ELSE', 'END', 'FROM', 'WHERE',
                                       'SELECT', 'DISTINCT', 'INT', 'DATE', 'VARCHAR', 'NVARCHAR'):
                    columns.add(col)

    return columns


def extract_m_expression_columns(expression, table_name):
    """
    Parse M/Power Query expression to identify columns referenced in transformations.

    Detects column references from:
    - Table.SelectColumns(source, {"col1", "col2"})
    - Table.RemoveColumns(source, {"col1", "col2"})
    - Table.RenameColumns(source, {{"old", "new"}, ...})
    - Table.TransformColumnTypes(source, {{"col1", type}})
    - Step-level references like [ColumnName]
    - #"Column Name" hash-quoted references in expressions
    - each [ColumnName] row-level references

    Args:
        expression: M expression/Power Query code
        table_name: Name of the table this expression belongs to

    Returns:
        Set of column names found in the expression
    """
    import re

    if not expression:
        return set()

    columns = set()

    # Pattern 1: Table.SelectColumns, Table.RemoveColumns, Table.ReorderColumns
    # These take a list of column names: {"Col1", "Col2", "Col3"}
    select_pattern = r'Table\.(?:SelectColumns|RemoveColumns|ReorderColumns)\s*\([^,]+,\s*\{([^}]+)\}'
    for match in re.finditer(select_pattern, expression, re.IGNORECASE):
        col_list = match.group(1)
        col_names = re.findall(r'"([^"]+)"', col_list)
        columns.update(col_names)

    # Pattern 2: Table.RenameColumns — {{"OldName", "NewName"}, ...}
    rename_pattern = r'Table\.RenameColumns\s*\([^,]+,\s*\{((?:\{[^}]+\}\s*,?\s*)+)\}'
    for match in re.finditer(rename_pattern, expression, re.IGNORECASE):
        pairs = match.group(1)
        pair_cols = re.findall(r'"([^"]+)"', pairs)
        columns.update(pair_cols)  # Both old and new names are relevant

    # Pattern 3: Table.TransformColumnTypes — {{"Col1", type text}, {"Col2", Int64.Type}}
    transform_pattern = r'Table\.TransformColumnTypes\s*\([^,]+,\s*\{((?:\{[^}]+\}\s*,?\s*)+)\}'
    for match in re.finditer(transform_pattern, expression, re.IGNORECASE):
        pairs = match.group(1)
        col_names = re.findall(r'\{\s*"([^"]+)"', pairs)
        columns.update(col_names)

    # Pattern 4: each [ColumnName] — row-level access in M
    each_col_pattern = r'(?:each\s+)?\[([A-Za-z_][A-Za-z0-9_ ]*)\]'
    for match in re.finditer(each_col_pattern, expression):
        col = match.group(1).strip()
        # Filter out M keywords and step names
        if col and len(col) < 80 and col not in ('Source', 'Changes', 'Type', 'Content'):
            columns.add(col)

    # Pattern 5: #"Renamed Columns" step references that contain column names in quotes
    # This catches things like = Table.AddColumn(source, "NewCol", each [ExistingCol])
    add_col_pattern = r'Table\.AddColumn\s*\([^,]+,\s*"([^"]+)"'
    for match in re.finditer(add_col_pattern, expression, re.IGNORECASE):
        columns.add(match.group(1))

    return columns


def parse_m_expression_for_tables(expression, all_tables):
    """
    Parse M expression to identify table references with enhanced pattern matching.

    This function identifies tables referenced within Power Query M expressions by
    detecting various M code patterns where tables are referenced, including:
    - Other Power BI tables/queries in the same dataset
    - SQL tables referenced in embedded SQL queries
    - Dataflow tables referenced in PowerBI.Dataflows
    - Excel tables, SharePoint lists, etc.

    Args:
        expression: The M expression/query code to parse
        all_tables: Set/list of all table names in the dataset model

    Returns:
        List of table names found in the expression (deduplicated)
    """
    import re

    if not expression:
        return []

    tables_found = set()  # Use set for automatic deduplication

    # Convert all_tables to a set for O(1) lookup performance
    all_tables_set = set(all_tables) if not isinstance(all_tables, set) else all_tables

    # ====================
    # PATTERN 1: Hash-quoted references #"TableName" or #"Query Name"
    # This is the MOST COMMON pattern in Power Query
    # Examples:
    #   let Source = #"Sales Data" in Source
    #   Table.Combine({#"Table1", #"Table2"})
    # ====================
    hash_quoted_pattern = r'#"([^"]+)"'
    hash_matches = re.findall(hash_quoted_pattern, expression)

    for match in hash_matches:
        # Only add if it matches a known table name in the model
        if match in all_tables_set:
            tables_found.add(match)

    # ====================
    # PATTERN 2: Direct table name references (unquoted identifiers)
    # Example: let Source = TableName in Source
    # This is less common but can occur with simple table names
    # ====================
    # We need to be careful here to avoid false positives
    # Only check for tables with simple names (no spaces, no special chars)
    for table in all_tables_set:
        # Skip tables that would be quoted (contain spaces or special chars)
        # These would only appear as #"TableName" format
        if ' ' in table or not table.replace('_', '').isalnum():
            continue

        # Create a word boundary pattern to match the table name
        # Use word boundaries to avoid partial matches
        escaped_table = re.escape(table)
        pattern = r'\b' + escaped_table + r'\b'

        # Search for the table name in the expression
        if re.search(pattern, expression):
            tables_found.add(table)

    # ====================
    # PATTERN 3: Table references in curly braces (list syntax)
    # Example: Table.Combine({TableName1, TableName2})
    # Example: {#"Table1", #"Table2"}
    # ====================
    # This pattern is already covered by patterns 1 and 2 above

    # ====================
    # PATTERN 4: Source{[Name="TableName"]} - filtered table references
    # Example: Source{[Name="DimCustomer"]}[Content]
    # ====================
    filtered_pattern = r'\{?\[\s*Name\s*=\s*"([^"]+)"\s*\]\}?'
    filtered_matches = re.findall(filtered_pattern, expression, re.IGNORECASE)

    for match in filtered_matches:
        if match in all_tables_set:
            tables_found.add(match)

    # ====================
    # PATTERN 5: Excel.CurrentWorkbook(){[Name="TableName"]}
    # This is specific to Excel sources with named ranges/tables
    # ====================
    excel_pattern = r'Excel\.CurrentWorkbook\s*\(\s*\)\s*\{?\[\s*Name\s*=\s*"([^"]+)"\s*\]\}?'
    excel_matches = re.findall(excel_pattern, expression, re.IGNORECASE)

    for match in excel_matches:
        if match in all_tables_set:
            tables_found.add(match)

    # ====================
    # PATTERN 6: Nested query references in 'let' statements
    # Example: let Source = #"Query1", Result = #"Query2" in Result
    # This is already covered by Pattern 1
    # ====================

    # ====================
    # PATTERN 7: SQL Table References - Extract from embedded SQL queries
    # Example: Sql.Database("server", "db", [Query="SELECT * FROM dbo.TableName"])
    # Example: FROM [schema].[TableName]
    # ====================
    print(f"         [DEBUG] Checking for SQL patterns...")
    print(f"         [DEBUG] 'Sql.Database' in expression: {'Sql.Database' in expression}")
    print(f"         [DEBUG] 'Query=' in expression: {'Query=' in expression}")

    if 'Sql.Database' in expression or 'Sql.Databases' in expression or 'Query=' in expression:
        print(f"         [DEBUG] SQL pattern detected in expression")
        # Extract the SQL query from the M expression
        # Look for Query="..." or Query='...' parameter
        # The pattern needs to handle #(lf), #(cr), etc. within the quoted string
        sql_query_pattern = r'Query\s*=\s*"([^"]*)"'
        sql_matches = re.findall(sql_query_pattern, expression, re.IGNORECASE)

        print(f"         [DEBUG] Found {len(sql_matches)} SQL query patterns")

        for sql_query in sql_matches:
            print(f"         [DEBUG] SQL Query preview: {sql_query[:100]}")
            # Replace escaped quotes
            sql_query = sql_query.replace('""', '"')
            # Also handle #(lf) line feeds in M code
            sql_query = sql_query.replace('#(lf)', '\n')
            sql_query = sql_query.replace('#(cr)', '\r')
            sql_query = sql_query.replace('#(tab)', '\t')

            print(f"         [DEBUG] After normalization: {sql_query[:150]}")

            # Extract table names from SQL
            sql_tables = extract_sql_table_names(sql_query)
            print(f"         [DEBUG] SQL tables found: {sql_tables}")
            tables_found.update(sql_tables)

    # Also check for direct SQL in Source{[Schema=..., Item=...]} pattern
    schema_item_pattern = r'\[\s*Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"\s*\]'
    schema_items = re.findall(schema_item_pattern, expression, re.IGNORECASE)
    for schema, item in schema_items:
        # Format as schema.table
        table_ref = f"{schema}.{item}"
        tables_found.add(table_ref)

    # ====================
    # PATTERN 8: PowerBI.Dataflows - Extract dataflow table references
    # Example: PowerBI.Dataflows(null){[workspaceId="..."]}[Data]{[dataflowId="..."]}[Data]{[entity="TableName"]}
    # ====================
    if 'PowerBI.Dataflows' in expression:
        # Look for entity= references which indicate dataflow table names
        entity_pattern = r'entity\s*=\s*"([^"]+)"'
        entity_matches = re.findall(entity_pattern, expression, re.IGNORECASE)
        for entity in entity_matches:
            # Add clean entity name without prefix - Source Type column will show "Dataflow"
            tables_found.add(entity)

        # Also look for hash-quoted navigation after dataflows
        # Pattern: #"a47e4573-c455-40af-a9ad-e22c81a07926"[Data]{[entity="WarehouseMaster"]}
        # The hash-quoted GUID is workspace/dataflow ID, not a table

    # ====================
    # EXCLUDE the query's own table name from results
    # A query shouldn't be listed as "using" itself
    # We'll handle this exclusion in the calling function
    # ====================

    # Convert set back to sorted list for consistent output
    return sorted(list(tables_found))



def extract_server_name(expression, source_type, workspace_map=None):
    """
    Extract server/source name from M expression based on source type.

    Args:
        expression: M expression text
        source_type: Type of the data source (SQL Server, Dataflow, etc.)
        workspace_map: Dictionary mapping workspace IDs to workspace names (optional)

    Returns:
        Server name, file path, or source identifier depending on the source type.
    """
    import re

    if not expression:
        return 'N/A'

    if source_type == 'SQL Server':
        # Extract server name from Sql.Database("server.name", "database")
        sql_pattern = r'Sql\.Database\s*\(\s*"([^"]+)"'
        match = re.search(sql_pattern, expression, re.IGNORECASE)
        if match:
            return match.group(1)

    elif source_type == 'Dataflow':
        # Extract workspace ID and resolve to workspace name if available
        # Pattern: PowerBI.Dataflows(null){[workspaceId="..."]}
        workspace_pattern = r'workspaceId\s*=\s*"([^"]+)"'
        match = re.search(workspace_pattern, expression, re.IGNORECASE)
        if match:
            workspace_id = match.group(1)
            # Try to resolve workspace name from the map
            if workspace_map and workspace_id in workspace_map:
                return workspace_map[workspace_id]
            # Fallback to truncated ID if name not available
            return f"Dataflow (Workspace: {workspace_id[:8]}...)"
        return 'Power BI Dataflow'

    elif source_type == 'Excel':
        # Extract file path from Excel.Workbook(File.Contents("path"))
        excel_pattern = r'File\.Contents\s*\(\s*"([^"]+)"'
        match = re.search(excel_pattern, expression, re.IGNORECASE)
        if match:
            file_path = match.group(1)
            # Return just the filename if it's a full path
            if '\\' in file_path or '/' in file_path:
                return file_path.split('\\')[-1].split('/')[-1]
            return file_path
        return 'Local File'

    elif source_type == 'Expression':
        # Internal model references
        return 'Internal Model'

    elif source_type == 'ODBC':
        # Extract DSN or connection string
        odbc_pattern = r'Odbc\.DataSource\s*\(\s*"([^"]+)"'
        match = re.search(odbc_pattern, expression, re.IGNORECASE)
        if match:
            return match.group(1)
        return 'ODBC Source'

    elif source_type == 'Web':
        # Extract URL
        web_pattern = r'Web\.Contents\s*\(\s*"([^"]+)"'
        match = re.search(web_pattern, expression, re.IGNORECASE)
        if match:
            url = match.group(1)
            # Return domain name
            domain_match = re.search(r'https?://([^/]+)', url)
            if domain_match:
                return domain_match.group(1)
            return url
        return 'Web Source'

    elif source_type == 'SharePoint':
        # Extract SharePoint site
        sp_pattern = r'https?://([^/]+)'
        match = re.search(sp_pattern, expression, re.IGNORECASE)
        if match:
            return match.group(1)
        return 'SharePoint'

    elif source_type == 'OData':
        # Extract OData feed URL
        odata_pattern = r'OData\.Feed\s*\(\s*"([^"]+)"'
        match = re.search(odata_pattern, expression, re.IGNORECASE)
        if match:
            url = match.group(1)
            domain_match = re.search(r'https?://([^/]+)', url)
            if domain_match:
                return domain_match.group(1)
            return url
        return 'OData Feed'

    return 'N/A'


def extract_source_line(expression):
    """
    Extract just the source table reference from an M expression.
    For Expression type queries, returns only the table reference without any variable assignment.
    Example: "#\"FactSales\"" (not "Source = #\"FactSales\"")
    """
    import re

    if not expression:
        return expression

    print(f"      🔍 DEBUG extract_source_line - Input: {expression[:100]}")

    # Pattern to find variable = #"TableName" assignments
    # This matches lines like: Source = #"TableName", source = #"Table", BaseTable = #"DimCustomer", etc.
    source_pattern = r'^\s*\w+\s*=\s*(#"[^"]+").*$'

    lines = expression.split('\n')

    # Look for the first line after "let" that assigns a hash-quoted table reference
    in_let_block = False
    for line in lines:
        stripped = line.strip()

        if stripped.lower().startswith('let'):
            in_let_block = True
            continue

        if in_let_block and stripped:
            # Check if this line has a table reference assignment (variable = #"TableName")
            match = re.match(source_pattern, stripped, re.IGNORECASE)
            if match:
                # Return ONLY the table reference part (e.g., #"FactSales")
                # This removes any "Source = ", "source = ", or other variable prefix
                result = match.group(1)
                print(f"      ✅ DEBUG extract_source_line - Extracted from let block: {result}")
                return result

    # If no source line found in let block, search entire expression
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.lower().startswith('let') and not stripped.lower().startswith('in'):
            # Try to extract just the table reference if it contains a pattern like "Variable = #"Table""
            match = re.match(source_pattern, stripped, re.IGNORECASE)
            if match:
                result = match.group(1)
                print(f"      ✅ DEBUG extract_source_line - Extracted from line scan: {result}")
                return result
            # Otherwise return the whole line
            print(f"      ⚠️ DEBUG extract_source_line - Returning whole line: {stripped}")
            return stripped

    # Fallback: if expression contains the pattern anywhere, extract just the table reference
    final_match = re.search(r'\w+\s*=\s*(#"[^"]+")' , expression, re.IGNORECASE)
    if final_match:
        result = final_match.group(1)
        print(f"      ✅ DEBUG extract_source_line - Extracted from final search: {result}")
        return result

    # Absolute fallback: return the full expression
    print(f"      ⚠️ DEBUG extract_source_line - Returning full expression: {expression[:100]}")
    return expression


def analyze_m_expression(expression):
    """Analyze M expression to determine query type and source type"""
    import re

    if not expression:
        return ('Unknown', 'Unknown')

    expression_lower = expression.lower()

    # List of external source indicators
    external_sources = [
        'sql.database', 'sql server',
        'powerbi.dataflows',
        'odbc.datasource', 'odbc.query',
        'excel.workbook', 'excel.currentworkbook',
        'web.contents', 'web.page',
        'sharepoint',
        'folder.files', 'file.contents',
        'odata.feed',
        'json.document',
        'xml.tables',
        'oracle.database',
        'mysql.database',
        'postgresql.database',
        'azuresql.database'
    ]

    # Check if expression has any external sources
    has_external_source = any(src in expression_lower for src in external_sources)

    # Determine source type
    source_type = 'Unknown'
    if 'sql.database' in expression_lower or 'sql server' in expression_lower:
        source_type = 'SQL Server'
    elif 'powerbi.dataflows' in expression_lower:
        source_type = 'Dataflow'
    elif 'odbc.datasource' in expression_lower or 'odbc.query' in expression_lower:
        source_type = 'ODBC'
    elif 'excel.workbook' in expression_lower or 'excel.currentworkbook' in expression_lower:
        source_type = 'Excel'
    elif 'web.contents' in expression_lower or 'web.page' in expression_lower:
        source_type = 'Web'
    elif 'sharepoint' in expression_lower:
        source_type = 'SharePoint'
    elif 'folder.files' in expression_lower or 'file.contents' in expression_lower:
        source_type = 'File System'
    elif 'odata.feed' in expression_lower:
        source_type = 'OData'
    elif 'json.document' in expression_lower:
        source_type = 'JSON'
    elif 'xml.tables' in expression_lower:
        source_type = 'XML'
    elif '#datetime' in expression_lower or '#date' in expression_lower or 'list.dates' in expression_lower:
        source_type = 'Date Function'
    elif 'table.fromrows' in expression_lower or 'table.fromlist' in expression_lower:
        source_type = 'Manual Table'
    elif not has_external_source:
        # Check if expression contains internal table references (hash-quoted names)
        # Pattern: #"TableName" indicates reference to another table in the model
        hash_quote_pattern = r'#"[^"]+"'
        has_internal_refs = bool(re.search(hash_quote_pattern, expression))

        if has_internal_refs:
            source_type = 'Expression'

    # Determine query type
    query_type = 'M Query'
    if 'value.nativequery' in expression_lower:
        query_type = 'Native Query'
    elif source_type == 'Manual Table' or source_type == 'Date Function':
        query_type = 'Generated Table'
    elif source_type == 'Expression':
        query_type = 'Transformation'

    return (query_type, source_type)


@app.route('/api/generate/progress/<job_id>')
@login_required
def get_generation_progress(job_id):
    """Get the current progress of a documentation generation job"""
    progress_data = generation_progress.get(job_id, {'progress': 0, 'status': 'Not started', 'complete': False})
    return jsonify(progress_data)


@app.route('/api/generate/download/<job_id>')
def download_generated_file(job_id):
    """Download the generated file for a completed job"""
    # Note: Removed @login_required to avoid request context issues
    # The job_id itself acts as a secure token (UUID + timestamp)

    job_data = generation_progress.get(job_id)

    if not job_data:
        return jsonify({'error': 'Job not found'}), 404

    if not job_data.get('complete'):
        return jsonify({'error': 'Generation not complete'}), 400

    if job_data.get('error'):
        return jsonify({'error': job_data['error']}), 500

    file_path = job_data.get('file_path')
    if not file_path or not os.path.exists(file_path):
        return jsonify({'error': 'File not found'}), 404

    # Get the filename from the path
    filename = os.path.basename(file_path)

    return send_file(
        file_path,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=filename
    )


def _generate_documentation_background(workspace_id, report_id, dataset_id, report_name, job_id, user_token):
    """Background task to generate documentation - runs in a separate thread"""
    try:
        print(f"\n🚀 [Job {job_id}] Starting complete documentation generation")
        print(f"   Workspace: {workspace_id}")
        print(f"   Report: {report_id}")
        print(f"   Dataset: {dataset_id}")

        generation_progress[job_id] = {'progress': 5, 'status': 'Initializing...', 'complete': False}

        # ✅ Use user-delegated token passed from main thread (already extracted from session)
        if user_token:
            powerbi.set_user_token(user_token)
            print(f"   🔑 Using user-delegated token for API calls")
        else:
            print(f"   ⚠️ No user token found, falling back to service principal")

        generation_progress[job_id] = {'progress': 10, 'status': 'Authenticating...', 'complete': False}

        # Ensure output directory exists
        output_dir = 'output'
        os.makedirs(output_dir, exist_ok=True)

        # Get dataset_id if not provided
        if not dataset_id:
            print(f"   ⚠️ Dataset ID not provided, fetching from report metadata...")
            generation_progress[job_id] = {'progress': 15, 'status': 'Fetching report info...', 'complete': False}
            # Quick fetch to get dataset_id
            reports = powerbi.get_all_reports(workspace_id)
            report = next((r for r in reports if r['id'] == report_id), None)
            if report:
                dataset_id = report.get('datasetId')
                report_name = report.get('name', report_name)
                print(f"   ✅ Found dataset ID: {dataset_id}")

        if not dataset_id:
            generation_progress[job_id] = {
                'progress': 0,
                'status': 'Error: No dataset ID',
                'complete': True,
                'error': 'Could not determine dataset ID for this report'
            }
            return  # Exit the background thread

        generation_progress[job_id] = {'progress': 20, 'status': 'Fetching metadata...', 'complete': False}

        # Use PowerBIDataFetcher for complete metadata (same as Main.py)
        from ai_generator import PowerBIDataFetcher

        # ✅ FIX: Pass user token to PowerBIDataFetcher
        fetcher = PowerBIDataFetcher(
            config.CLIENT_ID,
            config.CLIENT_SECRET,
            config.TENANT_ID,
            workspace_id,
            user_token=user_token  # Pass user-delegated token
        )

        print(f"   🔑 PowerBIDataFetcher initialized with {'user-delegated' if user_token else 'service principal'} token")

        generation_progress[job_id] = {'progress': 30, 'status': 'Analyzing dataset...', 'complete': False}

        # Get complete metadata including scanner data
        metadata = fetcher.get_complete_metadata(
            dataset_id=dataset_id,
            report_id=report_id,
            history_top=20
        )

        generation_progress[job_id] = {'progress': 45, 'status': 'Metadata collected...', 'complete': False}

        # Add report name to metadata
        metadata['report_name'] = report_name
        metadata['name'] = report_name

        generation_progress[job_id] = {'progress': 50, 'status': 'Generating overview...', 'complete': False}

        # Generate AI documentation sections
        print(f"\n🤖 Generating AI documentation sections...")

        # Create the documentation structure (same as generate_complete_documentation)
        documentation = {
            'overview': ai_generator.generate_overview(metadata),
            'data_sources': None,
            'pages': None,
            'user_guide': None,
            'technical_details': None,
            'migration': None,
            'metadata': metadata
        }

        generation_progress[job_id] = {'progress': 60, 'status': 'Generating data sources...', 'complete': False}
        documentation['data_sources'] = ai_generator.generate_data_sources_doc(metadata.get('data_sources', []))

        generation_progress[job_id] = {'progress': 70, 'status': 'Generating pages...', 'complete': False}
        documentation['pages'] = ai_generator.generate_pages_documentation(metadata.get('pages', []))

        generation_progress[job_id] = {'progress': 75, 'status': 'Generating user guide...', 'complete': False}
        documentation['user_guide'] = ai_generator.generate_user_guide(metadata.get('pages', []), report_name)

        generation_progress[job_id] = {'progress': 80, 'status': 'Generating technical details...', 'complete': False}
        documentation['technical_details'] = ai_generator.generate_technical_details(metadata)

        generation_progress[job_id] = {'progress': 85, 'status': 'Generating migration steps...', 'complete': False}
        documentation['migration'] = ai_generator.generate_migration_steps(report_name)

        generation_progress[job_id] = {'progress': 90, 'status': 'Creating document...', 'complete': False}

        # Create Word document
        doc_creator = PowerBIDocumentCreator()
        doc_filename = f"{report_name}_Documentation.docx"
        doc_path = os.path.join(output_dir, doc_filename)
        print(f"\n📝 Creating Word document: {doc_path}")

        # Create comprehensive document with proper structure
        doc_creator.create_documentation_from_json(
            json_data=documentation,  # Pass the full documentation structure, not just metadata
            output_filename=doc_path,
            author=config.AUTHOR_NAME
        )

        generation_progress[job_id] = {
            'progress': 100,
            'status': 'Complete!',
            'complete': True,
            'file_path': doc_path,
            'filename': doc_filename
        }

        print(f"✅ [Job {job_id}] Documentation generation complete!")

        # Clean up progress after a delay
        def cleanup_progress():
            time.sleep(300)  # Keep progress for 5 minutes
            if job_id in generation_progress:
                print(f"🧹 Cleaning up job {job_id}")
                # Also delete the file
                try:
                    if os.path.exists(doc_path):
                        os.remove(doc_path)
                except:
                    pass
                del generation_progress[job_id]

        threading.Thread(target=cleanup_progress, daemon=True).start()

    except Exception as e:
        print(f"❌ [Job {job_id}] Error generating document: {str(e)}")
        import traceback
        traceback.print_exc()

        # Update progress with error
        generation_progress[job_id] = {
            'progress': 0,
            'status': f'Error: {str(e)[:50]}',
            'complete': True,
            'error': str(e)
        }


@app.route('/api/generate', methods=['POST'])
@login_required
def generate_documentation():
    """Start documentation generation in background and return job ID"""
    data = request.get_json()

    workspace_id = data.get('workspace_id')
    report_id = data.get('report_id')
    dataset_id = data.get('dataset_id')
    report_name = data.get('report_name', 'Report')
    job_id = data.get('job_id', f'{report_id}_{int(time.time())}')

    if not report_id:
        return jsonify({
            'success': False,
            'error': 'Report ID is required'
        }), 400

    # Use provided workspace_id or fall back to config
    if not workspace_id:
        workspace_id = config.WORKSPACE_ID

    # Get user token from session
    user_token = session.get('access_token')

    # Initialize progress tracking
    generation_progress[job_id] = {'progress': 0, 'status': 'Starting...', 'complete': False}

    # Start background thread for generation
    thread = threading.Thread(
        target=_generate_documentation_background,
        args=(workspace_id, report_id, dataset_id, report_name, job_id, user_token),
        daemon=True
    )
    thread.start()

    # Return immediately with job ID
    return jsonify({
        'success': True,
        'job_id': job_id,
        'message': 'Documentation generation started'
    })



def _warm_catalog_async():
    """
    Warm ONLY thin packs (Home + Impact table list + summary).

    Never preload workspace_catalog.json / impact_index.json into workers —
    that was the main App Service OOM path:
      Worker was sent SIGKILL! Perhaps out of memory?
    Full catalog is read on demand from disk/SharePoint and not kept in RAM
    (see CATALOG_KEEP_HEAVY_IN_MEMORY).
    """
    if not CATALOG_AVAILABLE or catalog_service is None:
        return

    def _run():
        try:
            t0 = time.time()
            home = catalog_service.get_json('ui_home_index.json', force_refresh=False)
            tables = catalog_service.get_json('ui_impact_tables.json', force_refresh=False)
            report_dir = catalog_service.get_json('ui_report_directory.json', force_refresh=False)
            summary = catalog_service.get_summary(force_refresh=False)
            try:
                catalog_service.get_json('ops_summary.json', force_refresh=False)
            except Exception:
                pass
            n_home = len((home or {}).get('workspaces') or [])
            n_tables = len((tables or {}).get('rows') or []) if isinstance(tables, dict) else 0
            n_dir = len((report_dir or {}).get('rows') or []) if isinstance(report_dir, dict) else 0
            if not n_tables and hasattr(catalog_service, 'impact_table_rows'):
                try:
                    n_tables = len(catalog_service.impact_table_rows())
                except Exception:
                    pass
            # Ensure we never left a heavy blob from a rebuild path
            try:
                catalog_service.drop_heavy_memory()
            except Exception:
                pass
            print(
                f"⚡ Catalog warm-up (thin only) in {time.time() - t0:.1f}s "
                f"(homeWs={n_home}, impactRows={n_tables}, reportDir={n_dir}, "
                f"summary={bool(summary)}, opsEnrichedAt={(home or summary or {}).get('opsEnrichedAt')})"
            )
            if n_home == 0:
                print(
                    "⚠️ ui_home_index empty/missing after warm-up — Home will show offline "
                    "until SharePoint has the thin pack (extract job). "
                    "Do NOT load full workspace_catalog on this SKU."
                )
        except Exception as exc:
            print(f"⚠️ Catalog warm-up failed: {exc}")

    threading.Thread(target=_run, daemon=True, name='catalog-warm').start()


# Warm catalog on import so Gunicorn/Azure workers also preload thin packs.
# Guard with env if you need to disable on tiny SKUs: CATALOG_WARM_ON_START=false
if os.getenv('CATALOG_WARM_ON_START', 'true').lower() in ('1', 'true', 'yes', 'y'):
    try:
        _warm_catalog_async()
    except Exception as _warm_exc:
        print(f"Catalog warm-on-import skipped: {_warm_exc}")


if __name__ == '__main__':
    # Ensure output directory exists
    os.makedirs('output', exist_ok=True)
    # Use localhost instead of 0.0.0.0 to match Azure AD redirect URI
    app.run(debug=True, host='localhost', port=5000, use_reloader=False)
