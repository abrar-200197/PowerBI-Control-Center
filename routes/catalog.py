"""Catalog / Impact Explorer blueprint — page + API routes.

Extracted from app.py verbatim (no behavior change).

NOTE: `login_required`, `CATALOG_AVAILABLE`, `catalog_service`,
`get_user_powerbi_headers`, `workspaces_cache`, `CACHE_DURATION`,
`reports_cache`, and `workspace_folders_cache` live in app.py. They are
intentionally NOT imported at module level here — doing so causes a
circular import when app.py is run directly as a script (`python app.py`),
because the file is then loaded as `__main__` and `from app import ...`
triggers Python to re-import app.py fresh under the name `app`, re-running
the blueprint registration line while this module is still mid-import.
Instead, resolve them lazily at call/request time, by which point `app` is
fully initialized in sys.modules.
"""
import os
import time
from functools import wraps
from flask import Blueprint, request, jsonify, render_template, session

catalog_bp = Blueprint('catalog', __name__)


def login_required(f):
    """Lazy proxy for app.login_required (avoids circular import at load time)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not hasattr(wrapper, '_decorated'):
            from app import login_required as _real_login_required
            wrapper._decorated = _real_login_required(f)
        return wrapper._decorated(*args, **kwargs)
    return wrapper


def _catalog_available():
    from app import CATALOG_AVAILABLE as _real
    return _real


def _get_catalog_service():
    from app import catalog_service as _real
    return _real


def _get_user_powerbi_headers():
    from app import get_user_powerbi_headers as _real
    return _real()


@catalog_bp.route('/impact')
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
    import requests
    from app import workspaces_cache, CACHE_DURATION
    try:
        user_id = session.get('user', {}).get('oid', 'unknown')
        cache_key = f"workspaces_{user_id}"
        current_time = time.time()
        if cache_key in workspaces_cache and workspaces_cache[cache_key].get('data') and \
           (current_time - workspaces_cache[cache_key].get('timestamp', 0)) < CACHE_DURATION:
            return {w.get('id') for w in workspaces_cache[cache_key]['data'] if w.get('id')}

        # Lightweight live fetch (same as get_workspaces core)
        headers = _get_user_powerbi_headers()
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


@catalog_bp.route('/api/catalog/status')
@login_required
def api_catalog_status():
    """Catalog availability, mode, freshness — for UI banners and ops."""
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
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
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
        return {'success': False, 'error': 'Catalog not available', '_http': 503}

    catalog_service.invalidate()
    # Prefer thin packs first (fast UI), then heavy files for report/model APIs
    home = catalog_service.get_json('ui_home_index.json', force_refresh=True)
    tables = catalog_service.get_json('ui_impact_tables.json', force_refresh=True)
    try:
        impact_reports = catalog_service.get_json('ui_impact_reports.json', force_refresh=True)
    except Exception:
        impact_reports = None
    # Field usage / unused-fields thin packs (Impact Explorer "Field usage" and
    # "Unused columns & measures" tabs) — NOT covered by the generic home/impact
    # reload above, so force them here too or Refresh silently leaves them stale.
    try:
        field_index = catalog_service.get_json('ui_field_index.json', force_refresh=True)
    except Exception:
        field_index = None
    try:
        unused_index = catalog_service.get_json('ui_unused_fields_index.json', force_refresh=True)
    except Exception:
        unused_index = None
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
        import app as _app_module
        cleared_reports = len(_app_module.reports_cache)
        _app_module.reports_cache = {}
        _app_module.workspace_folders_cache = {}
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
        'ui_field_index': bool(field_index),
        'ui_unused_fields_index': bool(unused_index),
        'workspace_catalog': bool(cat),
        'impact_index': bool(impact),
        'summary': bool(summary),
        'opsEnrichedAt': ops_at,
        'generatedAt': (cat or {}).get('generatedAt') if isinstance(cat, dict) else None,
        'clearedReportsCacheEntries': cleared_reports,
        'status': catalog_service.status(),
    }


@catalog_bp.route('/api/catalog/refresh', methods=['POST'])
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


@catalog_bp.route('/api/catalog/refresh-internal', methods=['POST'])
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


@catalog_bp.route('/api/catalog/data/<path:name>')
@login_required
def api_catalog_data(name):
    """
    Serve *small* catalog JSON only (summary, ops_summary, ui packs).

    Large files (workspace_catalog, impact_index) are server-side only —
    use /api/catalog/impact/* and workspace report APIs instead.
    Browser never downloads SharePoint blobs.
    """
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
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


@catalog_bp.route('/api/catalog/impact/tables')
@login_required
def api_catalog_impact_tables():
    """
    Thin table list for Impact Explorer (no nested datasets).
    Strips bulky searchText field — browser builds search locally.
    Enables short browser/proxy cache so revisits are near-instant.
    """
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
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


@catalog_bp.route('/api/catalog/impact/table')
@login_required
def api_catalog_impact_table_detail():
    """One table's full impact (drawer) — server reads index, browser gets one object."""
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
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


@catalog_bp.route('/api/catalog/reports/search')
@login_required
def api_catalog_reports_search():
    """
    Reverse search: report name → workspace(s).
    Thin catalog directory (not full workspace_catalog in browser).
    ?q=wanek&limit=40
    """
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
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


@catalog_bp.route('/api/catalog/impact/reports')
@login_required
def api_catalog_impact_reports():
    """
    Thin report list for Impact Explorer «Report sources» tab (report → source counts).
    No nested source lists on the wire — use /impact/report for drawer detail.
    """
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
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


@catalog_bp.route('/api/catalog/impact/report')
@login_required
def api_catalog_impact_report_detail():
    """
    All sources for one report (SQL / Excel / SharePoint / model tables / …).
    Drawer payload for Report sources tab.
    """
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
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


@catalog_bp.route('/api/catalog/impact/fields')
@login_required
def api_catalog_impact_fields():
    """
    Thin field/metric usage list for Impact Explorer «Field usage» tab.
    Built weekly (--fresh) — see catalog_service/field_usage_index.py.
    """
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
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


@catalog_bp.route('/api/catalog/impact/field-detail')
@login_required
def api_catalog_impact_field_detail():
    """All reports using one field/metric (drawer) for Field usage tab."""
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
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


@catalog_bp.route('/api/catalog/impact/unused-fields')
@login_required
def api_catalog_impact_unused_fields():
    """
    Thin tenant-wide "truly unused" column/measure list for Impact Explorer
    «Unused Columns & Measures» tab (Level 1).
    Built weekly (--fresh) — see catalog_service/unused_fields_index.py.
    """
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    try:
        force = request.args.get('refresh') in ('1', 'true', 'yes')
        allowed = _user_allowed_workspace_ids()
        data = catalog_service.unused_fields_rows(
            force_refresh=force,
            allowed_workspace_ids=allowed if allowed is not None else None,
        )
        payload = {
            'success': True,
            'v': 1,
            'count': len(data.get('rows') or []),
            'rows': data.get('rows') or [],
            'generatedAt': data.get('generatedAt'),
            'caveat': data.get('caveat'),
            'totalColumns': data.get('totalColumns'),
            'totalMeasures': data.get('totalMeasures'),
            'excludedViaReportUsage': data.get('excludedViaReportUsage'),
            'excludedViaDaxOrMExpression': data.get('excludedViaDaxOrMExpression'),
            'excludedViaRelationshipKey': data.get('excludedViaRelationshipKey'),
            'source': 'server-thin',
        }
        resp = jsonify(payload)
        resp.headers['Cache-Control'] = 'no-store' if force else 'private, max-age=120'
        resp.headers['X-Data-Source'] = 'server-thin'
        return resp
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@catalog_bp.route('/api/catalog/impact/report-column-usage')
@login_required
def api_catalog_report_column_usage():
    """
    Level 2 — per-report column/measure usage breakdown (report drawer
    signal, NOT a deletion list). See CatalogService.report_column_usage.
    """
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    report_id = (request.args.get('report_id') or '').strip()
    if not report_id:
        return jsonify({'success': False, 'error': 'report_id required'}), 400
    try:
        detail = catalog_service.report_column_usage(report_id)
        if not detail:
            return jsonify({'success': False, 'error': f'No column usage data found for report {report_id}'}), 404
        return jsonify({'success': True, 'data': detail})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@catalog_bp.route('/api/catalog/impact/lookup')
@login_required
def api_catalog_impact_lookup():
    """Lookup table → datasets → reports blast radius from impact index (server-side)."""
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
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


@catalog_bp.route('/api/catalog/impact/model-details')
@login_required
def api_catalog_impact_model_details():
    """
    Thin semantic-model payload for Impact Explorer popup.
    Catalog-only (no full workspace_catalog in browser). Optional focus_table
    highlights the impact table the user came from.
    """
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
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


@catalog_bp.route('/api/home-summary')
@login_required
def api_home_summary():
    """
    Fast Home dashboard: one response for all accessible workspaces.
    Prefers precomputed SharePoint/local catalog (report/inactive/orphaned counts).
    Falls back to workspace list only if catalog unavailable (UI may still use live paths).
    """
    catalog_service = _get_catalog_service()
    try:
        allowed = _user_allowed_workspace_ids()
        # allowed may be empty set on failure — still try catalog unfiltered only if None
        if _catalog_available() and catalog_service is not None and catalog_service.is_available():
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


@catalog_bp.route('/api/home-summary/details')
@login_required
def api_home_summary_details():
    """
    Row lists for Home KPI tabs.
    ?metric=workspaces|reports|inactive|orphaned|zero_views
    """
    catalog_service = _get_catalog_service()
    try:
        metric = (request.args.get('metric') or 'workspaces').strip().lower()
        try:
            limit = int(request.args.get('limit') or 5000)
        except Exception:
            limit = 5000
        limit = max(1, min(limit, 20000))
        allowed = _user_allowed_workspace_ids()
        if not _catalog_available() or catalog_service is None or not catalog_service.is_available():
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


@catalog_bp.route('/api/catalog/impact/top')
@login_required
def api_catalog_impact_top():
    """Top blast-radius tables from precomputed impact index."""
    catalog_service = _get_catalog_service()
    if not _catalog_available() or catalog_service is None:
        return jsonify({'success': False, 'error': 'Catalog not available'}), 503
    try:
        n = min(int(request.args.get('n', 50)), 500)
        rows = catalog_service.impact_top(n=n)
        return jsonify({'success': True, 'count': len(rows), 'tables': rows,
                        'generatedAt': (catalog_service.get_impact_index() or {}).get('generatedAt')})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
