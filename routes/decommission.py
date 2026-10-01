"""Decommissioned Reports blueprint — page + API routes.

Extracted from app.py verbatim (no behavior change). Business logic for the
SharePoint archive inventory lives in features/decommission_inventory.py;
this module only owns the Flask route shells plus the small amount of
Overview-tab (Power BI embed) resolution logic that was inline in app.py.

NOTE: `login_required`, `get_user_powerbi_token`, and `_jwt_seconds_left` live
in app.py. They are intentionally NOT imported at module level here — doing
so causes a circular import when app.py is run directly as a script
(`python app.py`), because the file is then loaded as `__main__` and
`from app import ...` triggers Python to re-import app.py fresh under the
name `app`, re-running the blueprint registration line while this module is
still mid-import. Instead, resolve them lazily at call/request time, by
which point `app` is fully initialized in sys.modules.
"""
import os
import time
import requests
from functools import wraps
from flask import Blueprint, request, jsonify, render_template, session

decommission_bp = Blueprint('decommission', __name__)


def login_required(f):
    """Lazy proxy for app.login_required (avoids circular import at load time)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not hasattr(wrapper, '_decorated'):
            from app import login_required as _real_login_required
            wrapper._decorated = _real_login_required(f)
        return wrapper._decorated(*args, **kwargs)
    return wrapper


def get_user_powerbi_token():
    """Lazy proxy for app.get_user_powerbi_token."""
    from app import get_user_powerbi_token as _real
    return _real()


def _jwt_seconds_left(token):
    """Lazy proxy for app._jwt_seconds_left."""
    from app import _jwt_seconds_left as _real
    return _real(token)

# In-process cache for discovered programme report (user-token lookup by name)
_DECOMM_DASH_RESOLVE_CACHE = {'ts': 0.0, 'payload': None}

# In-process cache for the resolved Get-Report metadata (embedUrl/datasetId/name),
# keyed by workspace+report GUID pair. Avoids one Power BI API round trip on every
# Overview-tab open/re-open — metadata rarely changes, so a short TTL is safe.
_DECOMM_DASH_META_CACHE = {'key': None, 'ts': 0.0, 'payload': None}
_DECOMM_DASH_META_TTL_SECONDS = 300


def _parse_pbi_service_url(url: str):
    """Extract workspace + report GUIDs from an app.powerbi.com report URL."""
    import re
    u = (url or '').strip()
    if not u:
        return None, None
    m = re.search(
        r'/groups/([0-9a-fA-F-]{36})/reports/([0-9a-fA-F-]{36})',
        u,
        re.I,
    )
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _decomm_dashboard_config():
    """
    Overview-tab Power BI report settings (Estate Decommissioning Programme).
    All optional — Detail tab never depends on these.

    Resolution order for IDs:
      1) DECOMM_DASHBOARD_SERVICE_URL (parse groups/…/reports/…)
      2) DECOMM_DASHBOARD_WORKSPACE_ID + REPORT_ID env
      3) Built-in defaults (may be wrong if report moved — discovery fixes at runtime)
    """
    title = (
        os.getenv('DECOMM_DASHBOARD_TITLE')
        or 'Power BI Estate Decommissioning Programme'
    ).strip()
    # Prefer DECOMM_DASHBOARD_*; accept short aliases if someone set plain names in App Service
    dataset_id = (
        os.getenv('DECOMM_DASHBOARD_DATASET_ID')
        or os.getenv('DECOMM_DATASET_ID')
        or ''
    ).strip()
    service_url = (os.getenv('DECOMM_DASHBOARD_SERVICE_URL') or '').strip()

    workspace_id = (
        os.getenv('DECOMM_DASHBOARD_WORKSPACE_ID')
        or os.getenv('DECOMM_WORKSPACE_ID')
        or ''
    ).strip()
    report_id = (
        os.getenv('DECOMM_DASHBOARD_REPORT_ID')
        or os.getenv('DECOMM_REPORT_ID')
        or ''
    ).strip()

    # Prefer parsing a full working service URL (most reliable — paste from browser).
    if service_url:
        w_from_url, r_from_url = _parse_pbi_service_url(service_url)
        if w_from_url and r_from_url:
            workspace_id = workspace_id or w_from_url
            report_id = report_id or r_from_url

    # NO hard-coded GUIDs — wrong IDs caused service 404 + embed failure.
    # Resolve at runtime via DECOMM_DASHBOARD_* env or name discovery.

    if not service_url and workspace_id and report_id:
        service_url = (
            f'https://app.powerbi.com/groups/{workspace_id}/reports/{report_id}'
            f'/Overview?experience=power-bi'
        )

    # Embed UI is shown when enabled; IDs may be filled by discovery on first load
    embed_enabled = (os.getenv('DECOMM_DASHBOARD_EMBED') or 'true').strip().lower() in (
        '1', 'true', 'yes', 'on',
    )
    return {
        'workspaceId': workspace_id or None,
        'reportId': report_id or None,
        'datasetId': dataset_id or None,
        'serviceUrl': service_url or None,
        'embedEnabled': embed_enabled,
        'title': title or 'Power BI Estate Decommissioning Programme',
        'discoverName': (
            os.getenv('DECOMM_DASHBOARD_DISCOVER_NAME')
            or 'Power BI Estate Decommissioning Programme'
        ).strip(),
        'needsConfig': not bool(workspace_id and report_id) and not bool(service_url),
    }


def _score_decomm_report_name(rname: str, name_keys_l: list) -> int:
    rl = (rname or '').casefold()
    if not rl:
        return 0
    score = 0
    if rl in name_keys_l:
        return 100
    for nk in name_keys_l:
        if nk and nk in rl:
            score = max(score, 80)
    if 'decommission' in rl and ('estate' in rl or 'programme' in rl or 'program' in rl):
        score = max(score, 70)
    if 'summary decommission' in rl:
        score = max(score, 75)
    return score


def _discover_decomm_dashboard_report(access_token: str, prefer_name: str = None):
    """
    Find the programme report in workspaces the signed-in user can access.
    1) GET /reports (all accessible)
    2) Fallback: scan groups + reports
    """
    import time as _time

    global _DECOMM_DASH_RESOLVE_CACHE
    now = _time.time()
    cached = _DECOMM_DASH_RESOLVE_CACHE.get('payload')
    if cached and (now - float(_DECOMM_DASH_RESOLVE_CACHE.get('ts') or 0)) < 600:
        return dict(cached)

    if not access_token:
        return None

    prefer_name = (prefer_name or '').strip()
    name_keys = [
        prefer_name,
        'Power BI Estate Decommissioning Programme',
        'Estate Decommissioning Programme',
        'Summary Decommission',
    ]
    name_keys = [n for n in name_keys if n]
    name_keys_l = [n.casefold() for n in name_keys]

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
    }
    hits = []

    def _add_hit(rep, workspace_id=None, workspace_name='', priority=0):
        rname = (rep.get('name') or '').strip()
        rid = rep.get('id')
        if not rname or not rid:
            return
        score = _score_decomm_report_name(rname, name_keys_l)
        if score <= 0:
            return
        wid = workspace_id or rep.get('datasetWorkspaceId') or rep.get('workspaceId')
        # webUrl often has the correct groups/{id}/reports/{id}
        web = rep.get('webUrl') or ''
        if not wid:
            w2, _r2 = _parse_pbi_service_url(web)
            wid = w2
        hits.append({
            'score': score + priority,
            'workspaceId': wid,
            'workspaceName': workspace_name,
            'reportId': rid,
            'reportName': rname,
            'datasetId': rep.get('datasetId') or '',
            'embedUrl': rep.get('embedUrl') or '',
            'webUrl': web,
        })

    # Pass 1: all reports the user can see (fast, single call)
    try:
        rr = requests.get(
            'https://api.powerbi.com/v1.0/myorg/reports',
            headers=headers,
            timeout=90,
        )
        if rr.ok:
            for rep in (rr.json() or {}).get('value') or []:
                _add_hit(rep)
            print(f'   🔎 decomm discover /reports hits={len(hits)}')
        else:
            print(f'   ⚠️ decomm discover /reports HTTP {rr.status_code}')
    except Exception as e:
        print(f'   ⚠️ decomm discover /reports: {e}')

    # Pass 2: if nothing, walk workspaces (slower)
    if not hits:
        try:
            gr = requests.get(
                'https://api.powerbi.com/v1.0/myorg/groups?$top=5000',
                headers=headers,
                timeout=60,
            )
            if not gr.ok:
                print(f'   ⚠️ decomm discover groups HTTP {gr.status_code}')
            else:
                groups = (gr.json() or {}).get('value') or []
                for g in groups:
                    gid = g.get('id')
                    gname = g.get('name') or ''
                    if not gid:
                        continue
                    gname_l = gname.casefold()
                    priority = 0
                    if 'fabric' in gname_l and 'admin' in gname_l:
                        priority = 2
                    elif 'governance' in gname_l or 'decommission' in gname_l:
                        priority = 1
                    try:
                        r2 = requests.get(
                            f'https://api.powerbi.com/v1.0/myorg/groups/{gid}/reports',
                            headers=headers,
                            timeout=45,
                        )
                        if not r2.ok:
                            continue
                        for rep in (r2.json() or {}).get('value') or []:
                            _add_hit(rep, workspace_id=gid, workspace_name=gname, priority=priority)
                    except Exception as e:
                        print(f'   ⚠️ decomm discover reports in {gname}: {e}')
        except Exception as e:
            print(f'   ⚠️ decomm discover groups: {e}')

    if not hits:
        print('   ⚠️ decomm discover: no matching report in user scope')
        return None

    # Prefer hits that have a workspace id
    hits.sort(
        key=lambda h: (
            -h['score'],
            0 if h.get('workspaceId') else 1,
            (h.get('reportName') or '').lower(),
        )
    )
    best = hits[0]
    rid = best['reportId']
    wid = best.get('workspaceId')

    # If workspace missing, try to parse from webUrl or fetch report detail
    if not wid and best.get('webUrl'):
        wid, _ = _parse_pbi_service_url(best['webUrl'])
    if not wid:
        try:
            det = requests.get(
                f'https://api.powerbi.com/v1.0/myorg/reports/{rid}',
                headers=headers,
                timeout=45,
            )
            if det.ok:
                d = det.json() or {}
                best['embedUrl'] = best.get('embedUrl') or d.get('embedUrl') or ''
                best['webUrl'] = best.get('webUrl') or d.get('webUrl') or ''
                best['datasetId'] = best.get('datasetId') or d.get('datasetId') or ''
                wid, _ = _parse_pbi_service_url(best.get('webUrl') or '')
                # embedUrl sometimes: ...?reportId=&groupId=
                if not wid and best.get('embedUrl'):
                    import re
                    m = re.search(r'groupId=([0-9a-fA-F-]{36})', best['embedUrl'], re.I)
                    if m:
                        wid = m.group(1)
        except Exception as e:
            print(f'   ⚠️ decomm discover report detail: {e}')

    if not wid:
        print(f'   ⚠️ decomm discover: found report {rid} but no workspaceId')
        return None

    service_url = best.get('webUrl') or (
        f'https://app.powerbi.com/groups/{wid}/reports/{rid}'
        f'/Overview?experience=power-bi'
    )
    embed_url = best.get('embedUrl') or (
        f'https://app.powerbi.com/reportEmbed?reportId={rid}&groupId={wid}'
    )
    payload = {
        'workspaceId': wid,
        'workspaceName': best.get('workspaceName'),
        'reportId': rid,
        'reportName': best.get('reportName'),
        'datasetId': best.get('datasetId') or '',
        'embedUrl': embed_url,
        'serviceUrl': service_url,
        'discovered': True,
        'candidates': len(hits),
    }
    _DECOMM_DASH_RESOLVE_CACHE = {'ts': now, 'payload': payload}
    print(
        f"   ✅ decomm discover: {payload.get('reportName')!r} "
        f"ws={payload.get('workspaceName') or wid!r} id={rid}"
    )
    return dict(payload)


@decommission_bp.route('/decommissioned-reports')
@login_required
def decommissioned_reports_page():
    """Reports archived to SharePoint Report Decommission Activity (file inventory)."""
    return render_template(
        'decommissioned_reports.html',
        decomm_dashboard=_decomm_dashboard_config(),
    )


@decommission_bp.route('/api/decommissioned-reports/dashboard-config')
@login_required
def api_decommissioned_dashboard_config():
    """Public (auth) config for Overview tab — no secrets."""
    cfg = _decomm_dashboard_config()
    # Include feed path hints (no secrets) for UI help text
    try:
        from features.decommission_inventory import _dataset_feed_folder
        feed_folder = _dataset_feed_folder()
    except Exception:
        feed_folder = None
    return jsonify({
        'success': True,
        **cfg,
        'datasetFeedFolder': feed_folder,
        'datasetFeedFile': 'Decommissioned_Inventory_Latest.xlsx',
        'hasDatasetId': bool(cfg.get('datasetId')),
    })


@decommission_bp.route('/api/decommissioned-reports/publish-dataset-feed', methods=['POST'])
@login_required
def api_decommissioned_publish_dataset_feed():
    """
    Scan SharePoint archive → overwrite fixed inventory Excel/CSV for Power BI.
    Optional body/query: refresh_dataset=1 to trigger dataset refresh after publish
    (requires DECOMM_DASHBOARD_DATASET_ID + user permission to refresh).
    Does not change Detail inventory API or archive folders.
    """
    try:
        from features.decommission_inventory import (
            publish_decommission_dataset_feed,
            trigger_decommission_dataset_refresh,
            wait_decommission_dataset_refresh,
        )

        body = request.get_json(silent=True) or {}
        refresh_flag = (
            str(request.args.get('refresh_dataset') or body.get('refresh_dataset') or '')
            .strip()
            .lower()
        )
        # Default ON for Sync data button (export → SharePoint → refresh)
        if refresh_flag in ('', 'none'):
            do_refresh = True
        else:
            do_refresh = refresh_flag in ('1', 'true', 'yes', 'on')

        wait_flag = str(
            request.args.get('wait_refresh')
            or body.get('wait_refresh')
            or os.getenv('DECOMM_SYNC_WAIT_REFRESH')
            or '1'
        ).strip().lower()
        do_wait = wait_flag in ('1', 'true', 'yes', 'on')
        try:
            wait_sec = int(
                request.args.get('wait_sec')
                or body.get('wait_sec')
                or os.getenv('DECOMM_SYNC_WAIT_SEC')
                or 90
            )
        except Exception:
            wait_sec = 90
        wait_sec = max(15, min(wait_sec, 300))

        force = str(
            request.args.get('refresh') or body.get('force_inventory_refresh') or '1'
        ).lower() in ('1', 'true', 'yes', 'on')

        print(f"   📦 decomm Sync data: publish feed force={force} refresh={do_refresh}")
        result = publish_decommission_dataset_feed(force_inventory_refresh=force)
        if not result.get('success'):
            return jsonify(result), 502

        out = {
            'success': True,
            'publish': result,
            'datasetRefresh': None,
            'refreshWait': None,
            # SharePoint feed path for Power BI Get Data (always useful even if refresh fails)
            'feedNote': (
                'Inventory file updated on SharePoint. '
                f"Path: {result.get('xlsxPath') or result.get('folder') or '_dataset_feed'}. "
                'Programme report should refresh from this file.'
            ),
        }
        if do_refresh:
            cfg = _decomm_dashboard_config()
            token = get_user_powerbi_token()
            # Prefer fresh token
            if not token:
                session.pop('access_token', None)
                token = get_user_powerbi_token()

            workspace_id = cfg.get('workspaceId') or ''
            report_id = cfg.get('reportId') or ''
            dataset_id = cfg.get('datasetId') or ''

            # 1) Discovery cache from Overview embed
            try:
                cached = _DECOMM_DASH_RESOLVE_CACHE.get('payload') or {}
                if cached:
                    workspace_id = workspace_id or cached.get('workspaceId') or ''
                    report_id = report_id or cached.get('reportId') or ''
                    dataset_id = dataset_id or cached.get('datasetId') or ''
            except Exception:
                pass

            # 2) Parse service URL env
            if (not workspace_id or not report_id) and cfg.get('serviceUrl'):
                w, r = _parse_pbi_service_url(cfg.get('serviceUrl') or '')
                workspace_id = workspace_id or (w or '')
                report_id = report_id or (r or '')

            # 3) Discover report by name → datasetId
            if token and (not dataset_id or not workspace_id):
                try:
                    found = _discover_decomm_dashboard_report(
                        token,
                        prefer_name=cfg.get('discoverName') or cfg.get('title'),
                    )
                    if found:
                        workspace_id = workspace_id or found.get('workspaceId') or ''
                        report_id = report_id or found.get('reportId') or ''
                        dataset_id = dataset_id or found.get('datasetId') or ''
                except Exception as discover_err:
                    print(f"   ⚠️ decomm sync discover: {discover_err}")

            # 4) Get Report for datasetId
            if token and not dataset_id and workspace_id and report_id:
                try:
                    meta_url = (
                        f"https://api.powerbi.com/v1.0/myorg/groups/"
                        f"{workspace_id}/reports/{report_id}"
                    )
                    mr = requests.get(
                        meta_url,
                        headers={'Authorization': f'Bearer {token}'},
                        timeout=45,
                    )
                    if mr.ok:
                        dataset_id = (mr.json() or {}).get('datasetId') or ''
                except Exception as resolve_err:
                    print(f"   ⚠️ decomm dataset id resolve: {resolve_err}")

            # 5) GET /reports/{id} without group
            if token and not dataset_id and report_id:
                try:
                    mr = requests.get(
                        f"https://api.powerbi.com/v1.0/myorg/reports/{report_id}",
                        headers={'Authorization': f'Bearer {token}'},
                        timeout=45,
                    )
                    if mr.ok:
                        dataset_id = (mr.json() or {}).get('datasetId') or dataset_id
                except Exception:
                    pass

            print(
                f"   🔄 decomm sync refresh ws={workspace_id!r} "
                f"ds={dataset_id!r} report={report_id!r}"
            )
            refresh = trigger_decommission_dataset_refresh(
                access_token=token or '',
                workspace_id=workspace_id or None,
                dataset_id=dataset_id or None,
            )
            out['datasetRefresh'] = refresh
            out['resolved'] = {
                'workspaceId': workspace_id or None,
                'reportId': report_id or None,
                'datasetId': dataset_id or None,
            }

            # Optional: wait until refresh completes so embed sees new numbers
            if refresh.get('success') and do_wait and token and dataset_id:
                print(f"   ⏳ decomm sync waiting refresh up to {wait_sec}s…")
                waited = wait_decommission_dataset_refresh(
                    access_token=token,
                    workspace_id=workspace_id or None,
                    dataset_id=dataset_id or None,
                    max_wait_sec=wait_sec,
                )
                out['refreshWait'] = waited
                if waited.get('success'):
                    out['warning'] = None
                elif waited.get('timedOut'):
                    out['warning'] = waited.get('message') or 'Refresh still running'
                elif waited.get('error'):
                    out['warning'] = waited.get('error')
            # Publish OK even if refresh skipped/failed — feed is the critical path
            elif refresh.get('skipped'):
                out['warning'] = refresh.get('error')
            elif not refresh.get('success'):
                err = refresh.get('error') or 'Dataset refresh failed'
                # Keep UI readable but leave enough for 403/404 diagnostics
                if len(err) > 320:
                    err = err[:317] + '…'
                out['warning'] = err
            else:
                out['warning'] = None
        return jsonify(out)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@decommission_bp.route('/api/decommissioned-reports/dashboard-embed-token')
@login_required
def api_decommissioned_dashboard_embed_token():
    """
    Overview-tab embed credentials.

    Primary path: user Azure AD token (TokenType.Aad) — same SSO session as the app.
    Does NOT require GenerateToken / embed capacity (works for standard viewers).

    Optional fallback: GenerateToken (TokenType.Embed) when
    DECOMM_DASHBOARD_USE_GENERATE_TOKEN=true.

    Detail tab is independent of this endpoint.
    """
    try:
        cfg = _decomm_dashboard_config()
        if not cfg.get('embedEnabled'):
            return jsonify({
                'success': False,
                'error': 'Dashboard embed is disabled (DECOMM_DASHBOARD_EMBED).',
                'serviceUrl': cfg.get('serviceUrl'),
            }), 400

        def _fresh_user_token(force_refresh=False):
            if force_refresh:
                session.pop('access_token', None)
            return get_user_powerbi_token()

        user_token = _fresh_user_token(force_refresh=False)
        if not user_token:
            return jsonify({
                'success': False,
                'error': 'No Power BI user token in session. Sign out and sign in again.',
                'serviceUrl': cfg.get('serviceUrl'),
            }), 401

        workspace_id = cfg.get('workspaceId')
        report_id = cfg.get('reportId')
        dataset_id = cfg.get('datasetId') or ''
        report_name = cfg.get('title')
        service_url = cfg.get('serviceUrl')
        discovered = False

        def _get_report_meta(token, ws, rid):
            return requests.get(
                f'https://api.powerbi.com/v1.0/myorg/groups/{ws}/reports/{rid}',
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                },
                timeout=45,
            )

        class _CachedMetaResponse:
            """Lightweight stand-in for a requests.Response, built from cached JSON."""
            def __init__(self, payload):
                self.status_code = 200
                self.ok = True
                self._payload = payload
                self.text = ''

            def json(self):
                return self._payload

        meta_res = None
        meta_cache_key = f'{workspace_id}::{report_id}' if (workspace_id and report_id) else None
        now_ts = time.time()
        if (
            meta_cache_key
            and _DECOMM_DASH_META_CACHE.get('key') == meta_cache_key
            and _DECOMM_DASH_META_CACHE.get('payload') is not None
            and (now_ts - float(_DECOMM_DASH_META_CACHE.get('ts') or 0)) < _DECOMM_DASH_META_TTL_SECONDS
        ):
            meta_res = _CachedMetaResponse(_DECOMM_DASH_META_CACHE['payload'])
        elif workspace_id and report_id:
            meta_res = _get_report_meta(user_token, workspace_id, report_id)
            if meta_res.status_code == 401:
                print('   ⚠️ decomm embed: 401 on Get Report — refreshing user token…')
                user_token = _fresh_user_token(force_refresh=True)
                if not user_token:
                    return jsonify({
                        'success': False,
                        'error': 'Power BI session expired. Sign out and sign in again.',
                        'serviceUrl': service_url,
                    }), 401
                meta_res = _get_report_meta(user_token, workspace_id, report_id)
            if meta_res.ok:
                try:
                    _DECOMM_DASH_META_CACHE['key'] = meta_cache_key
                    _DECOMM_DASH_META_CACHE['ts'] = now_ts
                    _DECOMM_DASH_META_CACHE['payload'] = meta_res.json()
                except Exception:
                    pass

        # Always discover when IDs missing or Get Report failed (404/400).
        # Never keep using a dead GUID that makes Open-in-service 404 in the browser.
        need_discover = (
            not workspace_id
            or not report_id
            or meta_res is None
            or (meta_res is not None and meta_res.status_code in (404, 400))
            or (meta_res is not None and not meta_res.ok and meta_res.status_code != 401)
        )
        if need_discover:
            print(
                f"   🔎 decomm embed: resolving report by name "
                f"(cfg ws={workspace_id} rid={report_id} "
                f"http={getattr(meta_res, 'status_code', None)})"
            )
            found = _discover_decomm_dashboard_report(
                user_token,
                prefer_name=cfg.get('discoverName') or cfg.get('title'),
            )
            if found and found.get('reportId') and found.get('workspaceId'):
                workspace_id = found['workspaceId']
                report_id = found['reportId']
                dataset_id = dataset_id or found.get('datasetId') or ''
                report_name = found.get('reportName') or report_name
                service_url = found.get('serviceUrl') or service_url
                discovered = True
                meta_res = _get_report_meta(user_token, workspace_id, report_id)

        if not workspace_id or not report_id:
            return jsonify({
                'success': False,
                'error': (
                    'Could not resolve the decommission programme report. '
                    'Set DECOMM_DASHBOARD_SERVICE_URL to the full app.powerbi.com link '
                    'that opens for you, or DECOMM_DASHBOARD_WORKSPACE_ID + REPORT_ID.'
                ),
                'serviceUrl': service_url,
            }), 404

        fallback_embed_url = (
            f'https://app.powerbi.com/reportEmbed'
            f'?reportId={report_id}&groupId={workspace_id}'
        )
        embed_url = fallback_embed_url

        if meta_res is not None and meta_res.status_code == 401:
            return jsonify({
                'success': False,
                'error': (
                    'Not authorized to read this report via API (HTTP 401). '
                    'Confirm workspace access and re-login. '
                    'If the report opens in Power BI service, paste that URL as '
                    'DECOMM_DASHBOARD_SERVICE_URL on the App Service.'
                ),
                'serviceUrl': service_url,
                'detail': (meta_res.text or '')[:300],
            }), 401

        if meta_res is not None and meta_res.status_code == 404:
            return jsonify({
                'success': False,
                'error': (
                    'Report still not found after name discovery (HTTP 404). '
                    'Copy the browser URL from Power BI (groups/…/reports/…) into '
                    'App Setting DECOMM_DASHBOARD_SERVICE_URL and restart the app.'
                ),
                'serviceUrl': service_url,
                'configuredWorkspaceId': cfg.get('workspaceId'),
                'configuredReportId': cfg.get('reportId'),
                'httpStatus': 404,
            }), 404

        if meta_res is not None and meta_res.ok:
            meta = meta_res.json() or {}
            embed_url = meta.get('embedUrl') or fallback_embed_url
            dataset_id = dataset_id or meta.get('datasetId') or ''
            report_name = meta.get('name') or report_name
            if meta.get('webUrl'):
                service_url = meta.get('webUrl')
        elif discovered:
            # discovery already provided embedUrl
            found_eu = None
            try:
                found_eu = (_DECOMM_DASH_RESOLVE_CACHE.get('payload') or {}).get('embedUrl')
            except Exception:
                found_eu = None
            embed_url = found_eu or fallback_embed_url
        else:
            print(
                f"   ⚠️ decomm embed: Get Report HTTP "
                f"{getattr(meta_res, 'status_code', '?')} — using fallback embedUrl"
            )

        if not service_url:
            service_url = (
                f'https://app.powerbi.com/groups/{workspace_id}/reports/{report_id}'
                f'/Overview?experience=power-bi'
            )

        use_generate = (
            os.getenv('DECOMM_DASHBOARD_USE_GENERATE_TOKEN') or ''
        ).strip().lower() in ('1', 'true', 'yes', 'on')

        # --- Preferred: user AAD token (TokenType.Aad) ---
        if not use_generate:
            exp = None
            try:
                left = _jwt_seconds_left(user_token)
                if left is not None:
                    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                    exp = (_dt.now(_tz.utc) + _td(seconds=max(0, int(left)))).isoformat()
            except Exception:
                exp = None
            return jsonify({
                'success': True,
                'token': user_token,
                'tokenType': 'Aad',
                'expiration': exp,
                'embedUrl': embed_url,
                'reportId': report_id,
                'workspaceId': workspace_id,
                'datasetId': dataset_id or None,
                'serviceUrl': service_url,
                'title': report_name or cfg.get('title'),
                'mode': 'aad',
                'discovered': discovered,
            })

        # --- Optional: embed token via GenerateToken ---
        headers = {
            'Authorization': f'Bearer {user_token}',
            'Content-Type': 'application/json',
        }
        token_body = {'accessLevel': 'View'}
        if dataset_id:
            token_body['datasetId'] = dataset_id
            token_body['allowSaveAs'] = False
        token_url = (
            f'https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}'
            f'/reports/{report_id}/GenerateToken'
        )
        tok_res = requests.post(token_url, headers=headers, json=token_body, timeout=45)
        if not tok_res.ok:
            err_txt = (tok_res.text or '')[:400]
            print(f'   ⚠️ GenerateToken HTTP {tok_res.status_code} — falling back to Aad embed')
            return jsonify({
                'success': True,
                'token': user_token,
                'tokenType': 'Aad',
                'embedUrl': embed_url,
                'reportId': report_id,
                'workspaceId': workspace_id,
                'datasetId': dataset_id or None,
                'serviceUrl': service_url,
                'title': report_name or cfg.get('title'),
                'mode': 'aad_fallback',
                'discovered': discovered,
                'warning': f'GenerateToken failed ({tok_res.status_code}); using AAD embed. {err_txt[:120]}',
            })

        tok = tok_res.json() or {}
        access_token = tok.get('token')
        if not access_token:
            return jsonify({
                'success': True,
                'token': user_token,
                'tokenType': 'Aad',
                'embedUrl': embed_url,
                'reportId': report_id,
                'workspaceId': workspace_id,
                'serviceUrl': service_url,
                'title': report_name or cfg.get('title'),
                'mode': 'aad_fallback',
                'discovered': discovered,
            })

        return jsonify({
            'success': True,
            'token': access_token,
            'tokenType': 'Embed',
            'tokenId': tok.get('tokenId'),
            'expiration': tok.get('expiration'),
            'embedUrl': embed_url,
            'reportId': report_id,
            'workspaceId': workspace_id,
            'datasetId': dataset_id or None,
            'serviceUrl': service_url,
            'title': report_name or cfg.get('title'),
            'mode': 'embed',
            'discovered': discovered,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        cfg = _decomm_dashboard_config()
        return jsonify({
            'success': False,
            'error': str(e),
            'serviceUrl': cfg.get('serviceUrl'),
        }), 500


@decommission_bp.route('/api/decommissioned-reports')
@login_required
def api_decommissioned_reports():
    """
    List decommissioned reports from SharePoint archive tree.
    Query: workspace (optional name filter), q (search), refresh=1 to bypass cache.
    """
    try:
        from features.decommission_inventory import build_decommission_inventory

        force = request.args.get('refresh', '').lower() in ('1', 'true', 'yes')
        payload = build_decommission_inventory(force_refresh=force)
        if not payload.get('success'):
            return jsonify(payload), 502

        ws_filter = (request.args.get('workspace') or '').strip()
        q = (request.args.get('q') or '').strip().lower()

        rows = list(payload.get('rows') or [])
        if ws_filter:
            wsl = ws_filter.lower()
            rows = [r for r in rows if (r.get('workspaceName') or '').lower() == wsl]
        if q:
            def _match(r):
                blob = " ".join([
                    str(r.get('reportName') or ''),
                    str(r.get('workspaceName') or ''),
                    str(r.get('folderName') or ''),
                    str(r.get('batchFolder') or ''),
                    str(r.get('fileName') or ''),
                ]).lower()
                return q in blob
            rows = [r for r in rows if _match(r)]

        # Rebuild workspace groups for filtered report rows
        by_ws = {}
        for r in rows:
            wn = r.get('workspaceName') or 'Unknown'
            by_ws.setdefault(wn, []).append(r)

        # Keep empty SharePoint workspace folders (0 reports) from inventory.
        # Text search (q) only matches report rows — hide empties while searching.
        # Workspace dropdown filter still shows an empty folder when selected.
        if not q:
            for w in (payload.get('workspaces') or []):
                wn = (w.get('workspaceName') or 'Unknown')
                if ws_filter and wn.lower() != ws_filter.lower():
                    continue
                if wn not in by_ws:
                    by_ws[wn] = list(w.get('reports') or [])

        workspaces = [
            {
                'workspaceName': wn,
                'reportCount': len(rs),
                'reports': rs,
                'isEmpty': len(rs) == 0,
            }
            for wn, rs in sorted(by_ws.items(), key=lambda x: x[0].lower())
        ]

        empty_n = sum(1 for w in workspaces if not w.get('reportCount'))
        return jsonify({
            **payload,
            'rows': rows,
            'workspaces': workspaces,
            'totalReports': len(rows),
            'workspaceCount': len(workspaces),
            'emptyWorkspaceCount': empty_n,
            'filter': {'workspace': ws_filter or None, 'q': q or None},
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e), 'rows': [], 'workspaces': []}), 500


@decommission_bp.route('/api/decommissioned-reports/export')
@login_required
def export_decommissioned_reports():
    """
    Excel export of SharePoint decommissioned-report inventory.
    Same columns as the Decommissioned Reports UI table.
    Honors optional workspace + q filters (same as list API).
    """
    try:
        import io
        from datetime import datetime, timezone

        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from flask import send_file

        from features.decommission_inventory import build_decommission_inventory

        force = request.args.get('refresh', '').lower() in ('1', 'true', 'yes')
        payload = build_decommission_inventory(force_refresh=force)
        if not payload.get('success'):
            return jsonify({
                'success': False,
                'error': payload.get('error') or 'Failed to load decommission inventory',
            }), 502

        ws_filter = (request.args.get('workspace') or '').strip()
        q = (request.args.get('q') or '').strip().lower()

        rows = list(payload.get('rows') or [])
        if ws_filter:
            wsl = ws_filter.lower()
            rows = [r for r in rows if (r.get('workspaceName') or '').lower() == wsl]
        if q:
            def _match(r):
                blob = " ".join([
                    str(r.get('reportName') or ''),
                    str(r.get('workspaceName') or ''),
                    str(r.get('folderName') or ''),
                    str(r.get('batchFolder') or ''),
                    str(r.get('fileName') or ''),
                ]).lower()
                return q in blob
            rows = [r for r in rows if _match(r)]

        # Workspace A-Z, within each workspace newest decommissioned first
        from collections import defaultdict
        by_ws = defaultdict(list)
        for r in rows:
            by_ws[r.get('workspaceName') or 'Unknown'].append(r)
        ordered = []
        for wn in sorted(by_ws.keys(), key=lambda x: x.lower()):
            grp = by_ws[wn]
            grp.sort(
                key=lambda r: r.get('decommissionedAt') or r.get('lastModifiedAt') or '',
                reverse=True,
            )
            ordered.extend(grp)
        rows = ordered

        wb = Workbook()
        ws_out = wb.active
        ws_out.title = 'Decommissioned Reports'

        header_fill = PatternFill(start_color='2B6CB0', end_color='2B6CB0', fill_type='solid')
        header_font = Font(color='FFFFFF', bold=True, size=11)
        center_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
        left_align = Alignment(horizontal='left', vertical='center', wrap_text=True)

        headers = [
            '#',
            'Report',
            'File Name',
            'Workspace',
            'Folder',
            'Type',
            'Decommissioned',
            'Batch',
            'Size',
            'Size (bytes)',
            'File URL',
            'SharePoint Path',
        ]
        ws_out.append(headers)
        for col_num, _h in enumerate(headers, 1):
            cell = ws_out.cell(row=1, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = center_align

        ws_out.auto_filter.ref = f'A1:L1'
        ws_out.freeze_panes = 'A2'

        for i, r in enumerate(rows, 1):
            ws_out.append([
                i,
                r.get('reportName') or '',
                r.get('fileName') or '',
                r.get('workspaceName') or '',
                r.get('folderName') or '—',
                r.get('fileType') or '',
                r.get('decommissionedAtDisplay') or r.get('decommissionedAt') or '—',
                r.get('batchFolder') or '—',
                r.get('sizeDisplay') or '—',
                r.get('sizeBytes') if r.get('sizeBytes') is not None else '',
                r.get('webUrl') or '',
                r.get('sharePointPath') or '',
            ])
            rn = ws_out.max_row
            for c in range(1, 13):
                cell = ws_out.cell(row=rn, column=c)
                cell.alignment = center_align if c in (1, 6, 9) else left_align
            # Hyperlink file URL when present
            url = r.get('webUrl') or ''
            if url:
                link_cell = ws_out.cell(row=rn, column=11)
                try:
                    link_cell.hyperlink = url
                    link_cell.font = Font(color='0563C1', underline='single')
                except Exception:
                    pass

        if not rows:
            ws_out.append([
                '', 'No decommissioned report files found', '', '', '', '', '', '', '', '', '', ''
            ])

        widths = {
            'A': 6, 'B': 36, 'C': 36, 'D': 28, 'E': 22, 'F': 10,
            'G': 22, 'H': 40, 'I': 12, 'J': 14, 'K': 40, 'L': 50,
        }
        for letter, w in widths.items():
            ws_out.column_dimensions[letter].width = w

        # Meta sheet
        ws_meta = wb.create_sheet('Summary')
        ws_meta.append(['Decommissioned Reports export'])
        ws_meta.append(['Generated (UTC)', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')])
        ws_meta.append(['Source', 'SharePoint archive inventory'])
        ws_meta.append(['Base path', payload.get('basePath') or ''])
        ws_meta.append(['Reports in export', len(rows)])
        ws_meta.append(['Batch folders (all)', payload.get('batchCount') or 0])
        ws_meta.append(['Workspace filter', ws_filter or '(all)'])
        ws_meta.append(['Search filter', q or '(none)'])
        ws_meta.append([])
        ws_meta.append(['Note', payload.get('note') or ''])
        ws_meta.column_dimensions['A'].width = 28
        ws_meta.column_dimensions['B'].width = 80
        ws_meta.cell(row=1, column=1).font = Font(bold=True, size=13, color='FFFFFF')
        ws_meta.cell(row=1, column=1).fill = header_fill

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        stamp = datetime.now(timezone.utc).strftime('%Y%m%d')
        safe_ws = ''.join(
            c if c.isalnum() or c in ('-', '_') else '_' for c in (ws_filter or 'All')
        )[:40]
        filename = f'Decommissioned_Reports_{safe_ws}_{stamp}.xlsx'
        print(f"📁 decommissioned export rows={len(rows)} file={filename}")

        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
