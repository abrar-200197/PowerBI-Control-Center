"""Offline behavioral tests for the webapp ACL allow-list (P0 fail-closed contract).

`routes.catalog._user_allowed_workspace_ids()` is the single allow-list source
for every fail-closed workspace gate in the webapp (AGENTS.md gotcha 19). The
contract tested here:

  1. Fetch failure (HTTP error or network exception) returns set() — DENY,
     never None, never a fall-through to tenant-wide data.
  2. The delegated /groups fetch is PAGINATED (this tenant has ~550 workspaces
     and Power BI caps list responses at ~100 per page; a single-page fetch
     silently truncated the allow-list and produced false 403 denials).
  3. The helper reads ONLY its own `acl_workspaces_{oid}` cache namespace.
     The UI `workspaces_{oid}` entry is written by /api/workspaces, whose
     service-principal fallback stores the TENANT-WIDE workspace list under
     the user's key — reading it here would silently defeat every gate.
  4. Cache entries are source-tagged; anything not tagged as a delegated
     fetch (e.g. 'service_principal') is ignored and refetched live.
  5. Denial semantics: membership checks against an empty set() deny.

Run standalone:  python tests/test_webapp_acl.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# UTF-8 stdout/stderr (repo pattern for cp1252 consoles; the helper prints emoji).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import requests  # noqa: E402
import app  # noqa: E402  (module-level import; app.py is import-safe)
import routes.catalog as rc  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles (repo convention: plain objects, no mock library, no network)
# ---------------------------------------------------------------------------

class _StubSession:
    """Minimal flask.session stand-in for the helper's `session.get('user')`."""

    def __init__(self, user):
        self._user = user

    def get(self, key, default=None):
        if key == 'user':
            return self._user
        return default


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FlakyGet:
    """Scripted requests.get replacement: pops one scripted outcome per call."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append({'url': url, 'timeout': timeout})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


_TENANT_WIDE = [{'id': f'tenant-ws-{i}', 'name': f'Tenant WS {i}'} for i in range(6)]
_DELEGATED = [{'id': 'ws-allowed-1', 'name': 'Mine 1'}, {'id': 'ws-allowed-2', 'name': 'Mine 2'}]

_ORIGINALS = {}


def _setup():
    """Point the helper's lazy-resolved dependencies at test doubles."""
    _ORIGINALS['workspaces_cache'] = app.workspaces_cache
    _ORIGINALS['get_user_powerbi_headers'] = app.get_user_powerbi_headers
    _ORIGINALS['session'] = rc.session
    _ORIGINALS['requests_get'] = requests.get

    app.workspaces_cache = {}
    app.get_user_powerbi_headers = lambda: {'Authorization': 'Bearer test-token'}
    rc.session = _StubSession({'oid': 'acl-test-user'})


def _teardown():
    app.workspaces_cache = _ORIGINALS['workspaces_cache']
    app.get_user_powerbi_headers = _ORIGINALS['get_user_powerbi_headers']
    rc.session = _ORIGINALS['session']
    requests.get = _ORIGINALS['requests_get']


def _run_get(outcomes):
    flaky = _FlakyGet(outcomes)
    requests.get = flaky
    return flaky


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fetch_failure_returns_empty_set():
    """Non-200 from /groups → set() (deny), never None and never tenant data."""
    _setup()
    try:
        flaky = _run_get([_FakeResponse(403)])
        result = rc._user_allowed_workspace_ids()
        assert result == set(), result
        assert result is not None
        assert len(flaky.calls) == 1
    finally:
        _teardown()


def test_request_exception_returns_empty_set():
    """Network exception from /groups → set() (deny)."""
    _setup()
    try:
        _run_get([requests.ConnectionError("boom")])
        result = rc._user_allowed_workspace_ids()
        assert result == set(), result
        assert result is not None
    finally:
        _teardown()


def test_paginated_fetch_follows_nextlink():
    """The allow-list spans ALL /groups pages, not just the first ~100 rows."""
    _setup()
    try:
        page1 = {'value': _DELEGATED, 'nextLink': 'https://api.powerbi.com/v1.0/myorg/groups?$skip=100'}
        page2 = {'value': [{'id': 'ws-allowed-3', 'name': 'Mine 3'}]}
        flaky = _run_get([
            _FakeResponse(200, page1),
            _FakeResponse(200, page2),
        ])
        result = rc._user_allowed_workspace_ids()
        assert result == {'ws-allowed-1', 'ws-allowed-2', 'ws-allowed-3'}, result
        assert len(flaky.calls) == 2, flaky.calls
        # Every hop carries an explicit timeout (app-wide stall class).
        assert all(c['timeout'] for c in flaky.calls), flaky.calls
    finally:
        _teardown()


def test_ignores_ui_workspaces_cache_poisoning():
    """The UI cache entry (workspaces_{oid}) may hold tenant-wide SP fallback
    data; the ACL helper must never treat it as the user's allow-list."""
    _setup()
    try:
        now = time.time()
        app.workspaces_cache['workspaces_acl-test-user'] = {
            'data': _TENANT_WIDE, 'timestamp': now,
        }
        flaky = _run_get([_FakeResponse(200, {'value': _DELEGATED})])
        result = rc._user_allowed_workspace_ids()
        assert result == {'ws-allowed-1', 'ws-allowed-2'}, result
        assert 'tenant-ws-0' not in result
        # A live delegated fetch must have happened instead.
        assert len(flaky.calls) == 1, flaky.calls
    finally:
        _teardown()


def test_reads_own_acl_cache_entry():
    """A fresh delegated ACL entry is used WITHOUT a network call."""
    _setup()
    try:
        now = time.time()
        app.workspaces_cache['acl_workspaces_acl-test-user'] = {
            'data': _DELEGATED, 'timestamp': now, 'source': 'delegated_groups',
        }
        flaky = _run_get([])  # any call here is a failure
        result = rc._user_allowed_workspace_ids()
        assert result == {'ws-allowed-1', 'ws-allowed-2'}, result
        assert flaky.calls == [], flaky.calls
    finally:
        _teardown()


def test_ignores_non_delegated_acl_entry():
    """An ACL-namespace entry not tagged as delegated (e.g. written from SP
    fallback data) is never trusted — the helper refetches live."""
    _setup()
    try:
        now = time.time()
        app.workspaces_cache['acl_workspaces_acl-test-user'] = {
            'data': _TENANT_WIDE, 'timestamp': now, 'source': 'service_principal',
        }
        flaky = _run_get([_FakeResponse(200, {'value': _DELEGATED})])
        result = rc._user_allowed_workspace_ids()
        assert result == {'ws-allowed-1', 'ws-allowed-2'}, result
        assert 'tenant-ws-0' not in result
        assert len(flaky.calls) == 1, flaky.calls
    finally:
        _teardown()


def test_stale_acl_entry_refetches():
    """Expired ACL entries are refetched, not served."""
    _setup()
    try:
        stale_ts = time.time() - (app.CACHE_DURATION + 60)
        app.workspaces_cache['acl_workspaces_acl-test-user'] = {
            'data': _DELEGATED, 'timestamp': stale_ts, 'source': 'delegated_groups',
        }
        fresh = _DELEGATED + [{'id': 'ws-allowed-9', 'name': 'Mine 9'}]
        _run_get([_FakeResponse(200, {'value': fresh})])
        result = rc._user_allowed_workspace_ids()
        assert result == {'ws-allowed-1', 'ws-allowed-2', 'ws-allowed-9'}, result
    finally:
        _teardown()


def test_fail_closed_denial_semantics():
    """The gate contract: an empty allow-list (fetch failure) denies every
    workspace id — `id not in set()` is always True, and the helper never
    signals 'unfiltered' on failure."""
    _setup()
    try:
        _run_get([_FakeResponse(503)])
        result = rc._user_allowed_workspace_ids()
        assert isinstance(result, set)
        assert 'some-workspace' not in result  # deny
        assert '' not in result               # deny even the empty id
    finally:
        _teardown()


if __name__ == "__main__":
    test_fetch_failure_returns_empty_set()
    test_request_exception_returns_empty_set()
    test_paginated_fetch_follows_nextlink()
    test_ignores_ui_workspaces_cache_poisoning()
    test_reads_own_acl_cache_entry()
    test_ignores_non_delegated_acl_entry()
    test_stale_acl_entry_refetches()
    test_fail_closed_denial_semantics()
    print("All tests passed.")
