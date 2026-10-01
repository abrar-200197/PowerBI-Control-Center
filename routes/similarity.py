"""Similarity Analysis blueprint — page + API routes.

Extracted from app.py verbatim (no behavior change).

NOTE: `login_required`, `CATALOG_AVAILABLE`, `catalog_service`,
`_is_excluded_report_name`, `_get_workspace_scan_cached`,
`_semantic_scan_lock`, `_semantic_scan_cache`, and
`_extract_measures_and_relationships` live in app.py. They are intentionally
NOT imported at module level here — doing so causes a circular import when
app.py is run directly as a script (`python app.py`), because the file is
then loaded as `__main__` and `from app import ...` triggers Python to
re-import app.py fresh under the name `app`, re-running the blueprint
registration line while this module is still mid-import. Instead, resolve
them lazily at call/request time, by which point `app` is fully initialized
in sys.modules.
"""
import logging
from functools import wraps
from flask import Blueprint, request, jsonify, render_template, session, send_file

similarity_bp = Blueprint('similarity', __name__)

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


def _is_excluded_report_name(name):
    from app import _is_excluded_report_name as _real
    return _real(name)


def _get_workspace_scan_cached(workspace_id):
    from app import _get_workspace_scan_cached as _real
    return _real(workspace_id)


def _get_semantic_scan_lock():
    from app import _semantic_scan_lock as _real
    return _real


def _get_semantic_scan_cache():
    from app import _semantic_scan_cache as _real
    return _real


def _extract_measures_and_relationships(dataset):
    from app import _extract_measures_and_relationships as _real
    return _real(dataset)



@similarity_bp.route('/similarity-analysis')
@login_required
def similarity_analysis_page():
    """Similarity Analysis page - Discover similar reports, visuals, measures, and tables"""
    return render_template('similarity_analysis.html')




@similarity_bp.route('/api/reports/similarity-analysis/<workspace_id>')
@login_required
def analyze_report_similarity(workspace_id):
    """Analyze similarity between reports in a workspace or across all workspaces to identify potential duplicates"""
    try:
        from scanner_connector import PowerBIScanner
        from difflib import SequenceMatcher
        import requests

        # Get threshold from query parameter (default 0.3 for 30%)
        threshold = float(request.args.get('threshold', 0.3))
        threshold_percent = int(threshold * 100)

        # Check if this is a cross-workspace analysis
        is_global = workspace_id.lower() == 'all' or workspace_id.lower() == 'global'

        if is_global:
            print(f"\n🌍 Starting GLOBAL similarity analysis across all workspaces (threshold: {threshold_percent}%)...")
        else:
            print(f"\n🔍 Starting similarity analysis for workspace: {workspace_id} (threshold: {threshold_percent}%)...")

        # Get user token
        user_token = session.get('access_token')
        if not user_token:
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        # Initialize Scanner API
        scanner = PowerBIScanner()
        scanner.access_token = scanner.get_access_token()  # Use service principal token for Scanner API

        # Get list of workspaces to scan
        workspaces_to_scan = []
        if is_global:
            print("   📊 Fetching all accessible workspaces...")
            # Get all workspaces user has access to
            headers = {'Authorization': f'Bearer {user_token}', 'Content-Type': 'application/json'}
            ws_response = requests.get('https://api.powerbi.com/v1.0/myorg/groups', headers=headers)
            if ws_response.status_code == 200:
                all_workspaces = ws_response.json().get('value', [])
                workspaces_to_scan = [ws['id'] for ws in all_workspaces]
                print(f"   ✅ Found {len(workspaces_to_scan)} workspaces to analyze")
            else:
                return jsonify({'success': False, 'error': 'Failed to fetch workspaces'}), 500
        else:
            workspaces_to_scan = [workspace_id]

        # Run scan to get all reports with metadata
        print(f"   📊 Running Scanner API scan for {len(workspaces_to_scan)} workspace(s)...")

        # For global scan, aggregate data from multiple workspace scans
        all_scan_data = {'workspaces': []}

        if is_global:
            # Limit to first 10 workspaces for performance (can be adjusted)
            workspaces_to_scan = workspaces_to_scan[:10]
            print(f"   ⚠️  Limiting global scan to first {len(workspaces_to_scan)} workspaces for performance")

            for ws_id in workspaces_to_scan:
                try:
                    ws_scan = scanner.run_scan(workspace_id=ws_id)
                    if ws_scan and 'workspaces' in ws_scan:
                        all_scan_data['workspaces'].extend(ws_scan['workspaces'])
                        print(f"      ✅ Scanned workspace {ws_id}")
                except Exception as e:
                    print(f"      ⚠️  Failed to scan workspace {ws_id}: {e}")
                    continue
            scan_data = all_scan_data
        else:
            scan_data = scanner.run_scan(workspace_id=workspace_id)

        if not scan_data or "workspaces" not in scan_data:
            return jsonify({'success': False, 'error': 'Failed to scan workspace'}), 500

        # Extract reports and their metadata (ONLY from the scanned workspace)
        reports_data = []
        processed_report_ids = set()  # Track to avoid duplicates

        for ws in scan_data["workspaces"]:
            ws_id = ws.get("id")
            ws_name = ws.get('name', 'Unknown')

            # For single workspace analysis, only process the requested workspace
            if not is_global and ws_id != workspace_id:
                print(f"   ⚠️ Skipping workspace {ws_name} (not the requested workspace)")
                continue

            # Exclude App workspaces
            workspace_type = ws.get("type", "").lower()
            if workspace_type == "app" or "app" in ws_name.lower():
                print(f"   🚫 Skipping App workspace: {ws_name}")
                continue

            print(f"   ✓ Processing workspace: {ws_name} ({ws_id})")
            print(f"      Workspace type: {ws.get('type', 'Unknown')}")

            # Build dataset lookup for this workspace
            datasets_lookup = {}
            for dataset in ws.get("datasets", []):
                datasets_lookup[dataset.get("id")] = dataset

            # Process each report in THIS workspace only
            for report in ws.get("reports", []):
                report_id = report.get('id')
                report_name = report.get('name', '')

                # CRITICAL: Exclude App reports (they have [App] prefix or appId field)
                if '[App]' in report_name or report.get('appId'):
                    print(f"   🚫 Skipping App report: {report_name}")
                    continue

                # Skip if already processed (avoid duplicates)
                if report_id in processed_report_ids:
                    print(f"   ⚠️ Skipping duplicate report: {report_name}")
                    continue

                processed_report_ids.add(report_id)
                dataset_id = report.get("datasetId")
                dataset = datasets_lookup.get(dataset_id, {})

                print(f"   ✓ Including report: {report_name}")

                report_meta = {
                    'id': report_id,
                    'name': report_name,
                    'workspace_id': ws_id,
                    'workspace_name': ws_name,
                    'datasetId': dataset_id,
                    'tables': dataset.get('tables', []),
                    'expressions': dataset.get('expressions', []),
                    'measures': [],
                    'pages': [],
                    'visuals': [],  # Will be populated with visual metadata
                    # Additive metadata (safe: Scanner API already returns these fields
                    # for reports; unused by any existing consumer until now)
                    'modifiedBy': report.get('modifiedBy') or report.get('modifiedByUserPrincipalName') or '',
                    'modifiedDateTime': report.get('modifiedDateTime') or report.get('modifiedDate') or ''
                }

                # Extract DAX measures from tables
                for table in dataset.get('tables', []):
                    for measure in table.get('measures', []):
                        report_meta['measures'].append({
                            'name': measure.get('name'),
                            'expression': measure.get('expression', '')
                        })

                reports_data.append(report_meta)

        print(f"   📊 Total reports found in workspace: {len(reports_data)}")

        # Get page/visual metadata using regular API
        print(f"   📄 Enriching with page/visual data for {len(reports_data)} reports...")
        headers = {'Authorization': f'Bearer {user_token}', 'Content-Type': 'application/json'}

        for report in reports_data:
            try:
                # Get workspace ID for API calls (use the report's workspace_id from metadata)
                report_ws_id = report.get('workspace_id', workspace_id)

                # Get pages
                pages_url = f"https://api.powerbi.com/v1.0/myorg/groups/{report_ws_id}/reports/{report['id']}/pages"
                pages_response = requests.get(pages_url, headers=headers)
                if pages_response.status_code == 200:
                    pages = pages_response.json().get('value', [])
                    report['pages'] = pages

                    # Extract visual metadata from scanner data if available
                    # Scanner API provides detailed visual information in the report sections
                    for page_data in scan_data.get('workspaces', []):
                        for report_data in page_data.get('reports', []):
                            if report_data.get('id') == report['id']:
                                # Extract visuals from pages in scanner data
                                for page in report_data.get('pages', []):
                                    for visual in page.get('visuals', []):
                                        visual_info = {
                                            'visual_type': visual.get('visualType', ''),
                                            'title': visual.get('title', ''),
                                            'fields': []
                                        }
                                        # Extract fields/measures used in the visual
                                        if 'config' in visual:
                                            # Parse visual config to extract field references
                                            config_str = str(visual.get('config', ''))
                                            # Simple extraction - look for field names in config
                                            # This is a simplified approach; full parsing would need JSON parsing
                                            visual_info['fields'] = []  # Placeholder

                                        report['visuals'].append(visual_info)
            except Exception as e:
                print(f"      ⚠️ Could not get pages for {report['name']}: {e}")

        # Compare all reports pairwise (ONLY workspace reports, not Apps)
        print(f"   🔄 Comparing {len(reports_data)} workspace reports...")

        # DEBUG: Show what data we have for each report
        for idx, r in enumerate(reports_data):
            print(f"      📊 Report {idx+1}: {r['name']}")
            print(f"         Tables: {len(r.get('tables', []))}")
            print(f"         Measures: {len(r.get('measures', []))}")
            print(f"         Pages: {len(r.get('pages', []))}")
            if r.get('tables'):
                table_names = [t.get('name') for t in r['tables'][:3]]
                print(f"         Table names: {table_names}")

        comparisons = []

        for i in range(len(reports_data)):
            for j in range(i + 1, len(reports_data)):
                report_a = reports_data[i]
                report_b = reports_data[j]

                # FINAL SAFETY CHECK: Ensure neither report is an App report
                if '[App]' in report_a['name'] or '[App]' in report_b['name']:
                    print(f"   🚫 Skipping comparison with App report: {report_a['name']} vs {report_b['name']}")
                    continue

                # Calculate similarity scores
                similarity = calculate_report_similarity(report_a, report_b)

                # Use dynamic threshold (convert percentage to decimal if needed)
                threshold_value = threshold * 100 if threshold <= 1 else threshold
                if similarity['overall_score'] >= threshold_value:
                    print(f"   ✓ Found similar pair: {report_a['name']} vs {report_b['name']} ({similarity['overall_score']}%)")

                    # Include workspace info for cross-workspace analysis
                    comp_data = {
                        'report_a': {
                            'id': report_a['id'],
                            'name': report_a['name'],
                            'workspace_id': report_a.get('workspace_id'),
                            'workspace_name': report_a.get('workspace_name'),
                            'modifiedBy': report_a.get('modifiedBy', ''),
                            'modifiedDateTime': report_a.get('modifiedDateTime', '')
                        },
                        'report_b': {
                            'id': report_b['id'],
                            'name': report_b['name'],
                            'workspace_id': report_b.get('workspace_id'),
                            'workspace_name': report_b.get('workspace_name'),
                            'modifiedBy': report_b.get('modifiedBy', ''),
                            'modifiedDateTime': report_b.get('modifiedDateTime', '')
                        },
                        'is_cross_workspace': report_a.get('workspace_id') != report_b.get('workspace_id'),
                        'similarity_score': similarity['overall_score'],
                        'dax_similarity': similarity['scores']['dax_similarity'],
                        'table_similarity': similarity['scores']['table_similarity'],
                        'page_similarity': similarity['scores']['page_similarity'],
                        'visual_similarity': similarity['scores']['visual_similarity'],
                        'identical_measures': similarity['details']['identical_measures'],
                        'logic_matched_measures': similarity['details']['logic_matched_measures'],
                        'similar_measures': similarity['details']['similar_measures'],
                        'unique_measures_a': similarity['details']['unique_to_a'],
                        'unique_measures_b': similarity['details']['unique_to_b'],
                        'identical_tables': similarity['details']['identical_tables'],
                        'unique_tables_a': similarity['details']['unique_tables_a'],
                        'unique_tables_b': similarity['details']['unique_tables_b'],
                        'identical_pages': similarity['details']['identical_pages'],
                        'unique_pages_a': similarity['details']['unique_pages_a'],
                        'unique_pages_b': similarity['details']['unique_pages_b'],
                        'identical_visuals': similarity['details']['identical_visuals'],
                        'similar_visuals': similarity['details']['similar_visuals']
                    }
                    comparisons.append(comp_data)

        # Sort by similarity score (highest first)
        comparisons.sort(key=lambda x: x['similarity_score'], reverse=True)

        print(f"   ✅ Found {len(comparisons)} report pairs with >= 70% similarity")

        return jsonify({
            'success': True,
            'workspace_id': workspace_id,
            'total_reports': len(reports_data),
            'similar_pairs': len(comparisons),
            'comparisons': comparisons
        })

    except Exception as e:
        print(f"❌ Error in similarity analysis: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500



@similarity_bp.route('/api/similarity-analysis/export', methods=['GET', 'POST'])
@login_required
def export_similarity_analysis():
    """Export similarity analysis results to Excel or CSV with full details"""
    try:
        import io
        from datetime import datetime
        import json

        # Additive note (not present before): set only when the GET re-analyze
        # path caps the number of workspaces scanned for a cross-workspace
        # ('ALL'/'GLOBAL') export. None otherwise — existing behavior/response
        # shape is unchanged unless this note is populated.
        workspace_scan_note = None

        # Support both GET (old way) and POST (new way with results data)
        if request.method == 'POST':
            workspace_id = request.form.get('workspace_id')
            export_format = request.form.get('format', 'excel')
            results_json = request.form.get('results')

            if not results_json:
                return jsonify({'success': False, 'error': 'results data is required'}), 400

            print(f"\n📊 Exporting pre-analyzed similarity results")
            print(f"   Workspace: {workspace_id}")
            print(f"   Format: {export_format}")
            similarity_logger.info(f"Export (POST/pre-analyzed): workspace={workspace_id} format={export_format}")

            # Parse the results from frontend
            comparisons = json.loads(results_json)
            print(f"   ✅ Received {len(comparisons)} comparison(s) from frontend")

        else:
            # GET request - old behavior (re-analyze)
            workspace_id = request.args.get('workspace_id')
            export_format = request.args.get('format', 'excel')

            if not workspace_id:
                return jsonify({'success': False, 'error': 'workspace_id is required'}), 400

            print(f"\n📊 Exporting similarity analysis for workspace: {workspace_id}")
            print(f"   Format: {export_format}")
            print(f"   ⚠️ WARNING: Re-analyzing (slow for large datasets)")
            similarity_logger.info(f"Export (GET/re-analyze): workspace={workspace_id} format={export_format}")

            # Get user token
            user_token = session.get('access_token')
            if not user_token:
                return jsonify({'success': False, 'error': 'Not authenticated'}), 401

            # Run the same similarity analysis
            from scanner_connector import PowerBIScanner
            scanner = PowerBIScanner()
            scanner.access_token = scanner.get_access_token()

            # Check if this is a cross-workspace analysis
            is_global = workspace_id.upper() == 'ALL' or workspace_id.upper() == 'GLOBAL'

            # Get list of workspaces to scan
            workspaces_to_scan = []
            if is_global:
                print("   🌍 Cross-workspace export - fetching all accessible workspaces...")
                # Get all workspaces user has access to
                headers = {'Authorization': f'Bearer {user_token}', 'Content-Type': 'application/json'}
                ws_response = requests.get('https://api.powerbi.com/v1.0/myorg/groups', headers=headers)
                if ws_response.status_code == 200:
                    all_workspaces = ws_response.json().get('value', [])
                    workspaces_to_scan = [ws['id'] for ws in all_workspaces[:10]]  # Limit to 10 for performance
                    print(f"   ✅ Will export from {len(workspaces_to_scan)} workspaces")
                    if len(all_workspaces) > len(workspaces_to_scan):
                        workspace_scan_note = (
                            f"Only {len(workspaces_to_scan)} of {len(all_workspaces)} accessible "
                            f"workspaces were scanned (performance cap)."
                        )
                        print(f"   ⚠️ {workspace_scan_note}")
                    similarity_logger.info(
                        f"Cross-workspace export: scanning {len(workspaces_to_scan)} of {len(all_workspaces)} workspaces"
                    )
                else:
                    return jsonify({'success': False, 'error': 'Failed to fetch workspaces'}), 500
            else:
                workspaces_to_scan = [workspace_id]

            # Scan all workspaces
            all_scan_data = {'workspaces': []}
            for ws_id in workspaces_to_scan:
                try:
                    ws_scan = scanner.run_scan(workspace_id=ws_id)
                    if ws_scan and 'workspaces' in ws_scan:
                        all_scan_data['workspaces'].extend(ws_scan['workspaces'])
                        print(f"      ✅ Scanned workspace {ws_id}")
                except Exception as e:
                    print(f"      ⚠️ Failed to scan workspace {ws_id}: {e}")
                    continue

            scan_data = all_scan_data

            if not scan_data or "workspaces" not in scan_data:
                return jsonify({'success': False, 'error': 'Failed to scan workspace'}), 500

            # Extract reports
            reports_data = []
            processed_report_ids = set()

            for ws in scan_data["workspaces"]:
                ws_id = ws.get("id")
                ws_name = ws.get('name', 'Unknown')

                # For single workspace, only process requested workspace
                if not is_global and ws_id != workspace_id:
                    continue

                workspace_type = ws.get("type", "").lower()
                if workspace_type == "app" or "app" in ws_name.lower():
                    continue

                datasets_lookup = {}
                for dataset in ws.get("datasets", []):
                    datasets_lookup[dataset.get("id")] = dataset

                for report in ws.get("reports", []):
                    report_id = report.get('id')
                    report_name = report.get('name', '')

                    if '[App]' in report_name or report.get('appId'):
                        continue

                    if report_id in processed_report_ids:
                        continue

                    processed_report_ids.add(report_id)
                    dataset_id = report.get("datasetId")
                    dataset = datasets_lookup.get(dataset_id, {})

                    report_meta = {
                        'id': report_id,
                        'name': report_name,
                        'workspace_id': ws_id,
                        'workspace_name': ws_name,
                        'datasetId': dataset_id,
                        'tables': dataset.get('tables', []),
                        'measures': [],
                        'pages': [],
                        'modifiedBy': report.get('modifiedBy') or report.get('modifiedByUserPrincipalName') or '',
                        'modifiedDateTime': report.get('modifiedDateTime') or report.get('modifiedDate') or ''
                    }

                    for table in dataset.get('tables', []):
                        for measure in table.get('measures', []):
                            report_meta['measures'].append({
                                'name': measure.get('name'),
                                'expression': measure.get('expression', '')
                            })

                    reports_data.append(report_meta)

            # Get page/visual metadata
            headers = {'Authorization': f'Bearer {user_token}', 'Content-Type': 'application/json'}
            for report in reports_data:
                try:
                    report_ws_id = report.get('workspace_id', workspace_id)
                    pages_url = f"https://api.powerbi.com/v1.0/myorg/groups/{report_ws_id}/reports/{report['id']}/pages"
                    pages_response = requests.get(pages_url, headers=headers)
                    if pages_response.status_code == 200:
                        report['pages'] = pages_response.json().get('value', [])
                except Exception as e:
                    pass

            # Run comparisons
            print(f"\n📊 Starting similarity comparisons for {len(reports_data)} reports...")
            print(f"   Total comparisons to process: {len(reports_data) * (len(reports_data) - 1) // 2}")

            comparisons = []
            total_comparisons = len(reports_data) * (len(reports_data) - 1) // 2
            comparison_count = 0

            for i in range(len(reports_data)):
                for j in range(i + 1, len(reports_data)):
                    comparison_count += 1

                    # Progress update every 100 comparisons
                    if comparison_count % 100 == 0:
                        print(f"   Progress: {comparison_count}/{total_comparisons} comparisons ({int(comparison_count/total_comparisons*100)}%)")

                report_a = reports_data[i]
                report_b = reports_data[j]

                if '[App]' in report_a['name'] or '[App]' in report_b['name']:
                    continue

                similarity = calculate_report_similarity(report_a, report_b)

                # Use dynamic threshold (convert percentage to decimal if needed)
                threshold_value = threshold * 100 if threshold <= 1 else threshold
                if similarity['overall_score'] >= threshold_value:
                    # Check if cross-workspace
                    is_cross_ws = report_a.get('workspace_id') != report_b.get('workspace_id')

                    comparisons.append({
                        'report_a_id': report_a.get('id', ''),
                        'report_a_name': report_a['name'],
                        'report_a_workspace': report_a.get('workspace_name', ''),
                        'report_a_workspace_id': report_a.get('workspace_id', ''),
                        'report_a_modified_by': report_a.get('modifiedBy', ''),
                        'report_a_modified_date': report_a.get('modifiedDateTime', ''),
                        'report_b_id': report_b.get('id', ''),
                        'report_b_name': report_b['name'],
                        'report_b_workspace': report_b.get('workspace_name', ''),
                        'report_b_workspace_id': report_b.get('workspace_id', ''),
                        'report_b_modified_by': report_b.get('modifiedBy', ''),
                        'report_b_modified_date': report_b.get('modifiedDateTime', ''),
                        'is_cross_workspace': 'Yes' if is_cross_ws else 'No',
                        'similarity_score': similarity['overall_score'],
                        'dax_similarity': similarity['scores']['dax_similarity'],
                        'table_similarity': similarity['scores']['table_similarity'],
                        'page_similarity': similarity['scores']['page_similarity'],
                        'visual_similarity': similarity['scores'].get('visual_similarity', 0),
                        'identical_measures_count': len(similarity['details']['identical_measures']),
                        'identical_measures': ', '.join(similarity['details']['identical_measures']),
                        'logic_matched_measures_count': len(similarity['details'].get('logic_matched_measures', [])),
                        'logic_matched_measures': '; '.join([f"{m.get('measure_a')} ⟷ {m.get('measure_b')}" for m in similarity['details'].get('logic_matched_measures', [])]),
                        'identical_tables_count': len(similarity['details']['identical_tables']),
                        'identical_tables': ', '.join(similarity['details']['identical_tables']),
                        'identical_pages_count': len(similarity['details']['identical_pages']),
                        'identical_pages': ', '.join(similarity['details']['identical_pages']),
                        'identical_visuals_count': len(similarity['details'].get('identical_visuals', [])),
                        'similar_visuals_count': len(similarity['details'].get('similar_visuals', [])),
                        'unique_measures_a_count': len(similarity['details']['unique_to_a']),
                        'unique_measures_a': ', '.join(similarity['details']['unique_to_a']),
                        'unique_measures_b_count': len(similarity['details']['unique_to_b']),
                        'unique_measures_b': ', '.join(similarity['details']['unique_to_b']),
                        'unique_tables_a_count': len(similarity['details']['unique_tables_a']),
                        'unique_tables_a': ', '.join(similarity['details']['unique_tables_a']),
                        'unique_tables_b_count': len(similarity['details']['unique_tables_b']),
                        'unique_tables_b': ', '.join(similarity['details']['unique_tables_b']),
                        'unique_pages_a_count': len(similarity['details']['unique_pages_a']),
                        'unique_pages_a': ', '.join(similarity['details']['unique_pages_a']),
                        'unique_pages_b_count': len(similarity['details']['unique_pages_b']),
                        'unique_pages_b': ', '.join(similarity['details']['unique_pages_b'])
                    })

            print(f"\n✅ Comparison complete! Found {len(comparisons)} similar pairs (>= 70% similarity)")
            print(f"   Now generating export file...")

        # Enhanced sorting: First by Report A name (A-Z), then by Similarity Score (Descending)
        # Support both frontend format (report_a.name) and backend format (report_a_name)
        def get_report_a_name(comp):
            if 'report_a_name' in comp:
                return comp['report_a_name'].lower()
            elif 'report_a' in comp and isinstance(comp['report_a'], dict):
                return comp['report_a'].get('name', '').lower()
            return ''

        def get_similarity_score(comp):
            return comp.get('similarity_score', 0)

        comparisons.sort(key=lambda x: (get_report_a_name(x), -get_similarity_score(x)))

        # Normalize comparisons to flat structure for export (handle both frontend and backend formats)
        normalized_comparisons = []
        for comp in comparisons:
            normalized = {}

            # Handle both frontend format (nested) and backend format (flat)
            if 'report_a' in comp and isinstance(comp['report_a'], dict):
                # Frontend format
                normalized['report_a_id'] = comp['report_a'].get('id', '')
                normalized['report_a_name'] = comp['report_a'].get('name', '')
                normalized['report_a_workspace'] = comp['report_a'].get('workspace_name', '')
                normalized['report_a_workspace_id'] = comp['report_a'].get('workspace_id', '')
                normalized['report_a_modified_by'] = comp['report_a'].get('modifiedBy', '')
                normalized['report_a_modified_date'] = comp['report_a'].get('modifiedDateTime', '')
            else:
                # Backend format
                normalized['report_a_id'] = comp.get('report_a_id', '')
                normalized['report_a_name'] = comp.get('report_a_name', '')
                normalized['report_a_workspace'] = comp.get('report_a_workspace', '')
                normalized['report_a_workspace_id'] = comp.get('report_a_workspace_id', '')
                normalized['report_a_modified_by'] = comp.get('report_a_modified_by', '')
                normalized['report_a_modified_date'] = comp.get('report_a_modified_date', '')

            if 'report_b' in comp and isinstance(comp['report_b'], dict):
                # Frontend format
                normalized['report_b_id'] = comp['report_b'].get('id', '')
                normalized['report_b_name'] = comp['report_b'].get('name', '')
                normalized['report_b_workspace'] = comp['report_b'].get('workspace_name', '')
                normalized['report_b_workspace_id'] = comp['report_b'].get('workspace_id', '')
                normalized['report_b_modified_by'] = comp['report_b'].get('modifiedBy', '')
                normalized['report_b_modified_date'] = comp['report_b'].get('modifiedDateTime', '')
            else:
                # Backend format
                normalized['report_b_id'] = comp.get('report_b_id', '')
                normalized['report_b_name'] = comp.get('report_b_name', '')
                normalized['report_b_workspace'] = comp.get('report_b_workspace', '')
                normalized['report_b_workspace_id'] = comp.get('report_b_workspace_id', '')
                normalized['report_b_modified_by'] = comp.get('report_b_modified_by', '')
                normalized['report_b_modified_date'] = comp.get('report_b_modified_date', '')

            # Copy all other fields
            normalized['similarity_score'] = comp.get('similarity_score', 0)
            normalized['is_cross_workspace'] = comp.get('is_cross_workspace', 'No')
            normalized['dax_similarity'] = comp.get('scores', {}).get('dax_similarity', 0) if 'scores' in comp else comp.get('dax_similarity', 0)
            normalized['table_similarity'] = comp.get('scores', {}).get('table_similarity', 0) if 'scores' in comp else comp.get('table_similarity', 0)
            normalized['page_similarity'] = comp.get('scores', {}).get('page_similarity', 0) if 'scores' in comp else comp.get('page_similarity', 0)
            normalized['visual_similarity'] = comp.get('scores', {}).get('visual_similarity', 0) if 'scores' in comp else comp.get('visual_similarity', 0)

            # Handle details
            details = comp.get('details', {})
            normalized['identical_measures_count'] = len(details.get('identical_measures', []))
            normalized['identical_measures'] = ', '.join(details.get('identical_measures', []))
            normalized['logic_matched_measures_count'] = len(details.get('logic_matched_measures', []))

            # Logic matched measures formatting
            logic_matches = details.get('logic_matched_measures', [])
            if logic_matches:
                normalized['logic_matched_measures'] = '; '.join([f"{m.get('measure_a')} ⟷ {m.get('measure_b')}" for m in logic_matches])
            else:
                normalized['logic_matched_measures'] = ''

            normalized['identical_tables_count'] = len(details.get('identical_tables', []))
            normalized['identical_tables'] = ', '.join(details.get('identical_tables', []))
            normalized['identical_pages_count'] = len(details.get('identical_pages', []))
            normalized['identical_pages'] = ', '.join(details.get('identical_pages', []))
            normalized['identical_visuals_count'] = len(details.get('identical_visuals', []))
            normalized['similar_visuals_count'] = len(details.get('similar_visuals', []))
            normalized['unique_measures_a_count'] = len(details.get('unique_to_a', []))
            normalized['unique_measures_a'] = ', '.join(details.get('unique_to_a', []))
            normalized['unique_measures_b_count'] = len(details.get('unique_to_b', []))
            normalized['unique_measures_b'] = ', '.join(details.get('unique_to_b', []))
            normalized['unique_tables_a_count'] = len(details.get('unique_tables_a', []))
            normalized['unique_tables_a'] = ', '.join(details.get('unique_tables_a', []))
            normalized['unique_tables_b_count'] = len(details.get('unique_tables_b', []))
            normalized['unique_tables_b'] = ', '.join(details.get('unique_tables_b', []))
            normalized['unique_pages_a_count'] = len(details.get('unique_pages_a', []))
            normalized['unique_pages_a'] = ', '.join(details.get('unique_pages_a', []))
            normalized['unique_pages_b_count'] = len(details.get('unique_pages_b', []))
            normalized['unique_pages_b'] = ', '.join(details.get('unique_pages_b', []))

            normalized_comparisons.append(normalized)

        comparisons = normalized_comparisons

        # Prepare export metadata
        total_reports = len(comparisons) if request.method == 'POST' else len(reports_data)

        # Additive helpers for the Excel export column improvements (report link,
        # recommended-action flag). Pure functions — no effect on existing fields.
        def _powerbi_report_url(ws_id, rpt_id):
            if not ws_id or not rpt_id:
                return ''
            return f"https://app.powerbi.com/groups/{ws_id}/reports/{rpt_id}"

        def _recommended_action(score):
            try:
                score_val = float(score)
            except (TypeError, ValueError):
                return ''
            if score_val >= 95:
                return 'Consolidate (near-duplicate)'
            if score_val >= 85:
                return 'Review (high overlap)'
            return 'Monitor (moderate overlap)'

        if export_format == 'excel':
            # Export to Excel with professional formatting
            try:
                import pandas as pd
                from openpyxl import load_workbook
                from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
                from openpyxl.utils import get_column_letter

                output = io.BytesIO()

                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    # ===== SHEET 1: Summary =====
                    summary_metrics = [
                        'Report Title',
                        'Analysis Date',
                        'Analysis Time',
                        'Workspace ID',
                        'Total Reports Analyzed',
                        'Similar Pairs Found (≥70%)',
                        'Highest Similarity',
                        'Lowest Similarity'
                    ]
                    summary_values = [
                        'Power BI Similarity Analysis',
                        datetime.now().strftime('%Y-%m-%d'),
                        datetime.now().strftime('%H:%M:%S'),
                        workspace_id,
                        total_reports,
                        len(comparisons),
                        max([c['similarity_score'] for c in comparisons]) if comparisons else 0,
                        min([c['similarity_score'] for c in comparisons]) if comparisons else 0
                    ]
                    if workspace_scan_note:
                        summary_metrics.append('Note')
                        summary_values.append(workspace_scan_note)
                    summary_data = {'Metric': summary_metrics, 'Value': summary_values}
                    pd.DataFrame(summary_data).to_excel(writer, sheet_name='Summary', index=False)

                    # ===== SHEET 2: Comparison Overview =====
                    overview_data = []
                    for comp in comparisons:
                        report_a_link = _powerbi_report_url(comp.get('report_a_workspace_id'), comp.get('report_a_id'))
                        report_b_link = _powerbi_report_url(comp.get('report_b_workspace_id'), comp.get('report_b_id'))
                        overview_data.append({
                            # --- Identity ---
                            'Report A': comp['report_a_name'],
                            'Workspace A': comp.get('report_a_workspace', ''),
                            'Report A ID': comp.get('report_a_id', ''),
                            'Report A Link': report_a_link,
                            'Report B': comp['report_b_name'],
                            'Workspace B': comp.get('report_b_workspace', ''),
                            'Report B ID': comp.get('report_b_id', ''),
                            'Report B Link': report_b_link,
                            'Cross-Workspace': comp.get('is_cross_workspace', 'No'),
                            # --- Scores ---
                            'Overall Similarity %': comp['similarity_score'],
                            'DAX Logic %': comp['dax_similarity'],
                            'Schema %': comp['table_similarity'],
                            'Pages %': comp['page_similarity'],
                            'Visuals %': comp.get('visual_similarity', 0),
                            # --- Counts ---
                            'Common Measures': comp['identical_measures_count'],
                            'Logic-Matched Measures': comp.get('logic_matched_measures_count', 0),
                            'Common Tables': comp['identical_tables_count'],
                            'Common Pages': comp['identical_pages_count'],
                            'Identical Visuals': comp.get('identical_visuals_count', 0),
                            'Similar Visuals': comp.get('similar_visuals_count', 0),
                            # --- Metadata / Action ---
                            'Report A Modified By': comp.get('report_a_modified_by', ''),
                            'Report A Last Modified': comp.get('report_a_modified_date', ''),
                            'Report B Modified By': comp.get('report_b_modified_by', ''),
                            'Report B Last Modified': comp.get('report_b_modified_date', ''),
                            'Recommended Action': _recommended_action(comp['similarity_score'])
                        })

                    df_overview = pd.DataFrame(overview_data)
                    df_overview.to_excel(writer, sheet_name='Comparison Overview', index=False)

                    # ===== SHEET 3: Detailed Breakdown =====
                    detailed_data = []
                    for comp in comparisons:
                        report_a_link = _powerbi_report_url(comp.get('report_a_workspace_id'), comp.get('report_a_id'))
                        report_b_link = _powerbi_report_url(comp.get('report_b_workspace_id'), comp.get('report_b_id'))
                        detailed_data.append({
                            # --- Identity ---
                            'Report A': comp['report_a_name'],
                            'Workspace A': comp.get('report_a_workspace', ''),
                            'Report A ID': comp.get('report_a_id', ''),
                            'Report A Link': report_a_link,
                            'Report B': comp['report_b_name'],
                            'Workspace B': comp.get('report_b_workspace', ''),
                            'Report B ID': comp.get('report_b_id', ''),
                            'Report B Link': report_b_link,
                            'Cross-Workspace': comp.get('is_cross_workspace', 'No'),
                            # --- Scores ---
                            'Overall Similarity %': comp['similarity_score'],
                            'DAX Logic %': comp['dax_similarity'],
                            'Schema %': comp['table_similarity'],
                            'Pages %': comp['page_similarity'],
                            'Visuals %': comp.get('visual_similarity', 0),
                            # --- Names/detail (kept for full traceability) ---
                            'Common Tables': comp['identical_tables'],
                            'Missing in A (Tables)': comp['unique_tables_b'],
                            'Missing in B (Tables)': comp['unique_tables_a'],
                            'Common Measures (Identical Name & DAX)': comp['identical_measures'],
                            'Logic-Matched Measures (Same DAX, Different Name)': comp.get('logic_matched_measures', ''),
                            'Missing in A (Measures)': comp['unique_measures_b'],
                            'Missing in B (Measures)': comp['unique_measures_a'],
                            'Common Pages': comp['identical_pages'],
                            'Missing in A (Pages)': comp['unique_pages_b'],
                            'Missing in B (Pages)': comp['unique_pages_a'],
                            'Identical Visuals': comp.get('identical_visuals_count', 0),
                            'Similar Visuals (70%+ Overlap)': comp.get('similar_visuals_count', 0),
                            # --- Metadata / Action ---
                            'Report A Modified By': comp.get('report_a_modified_by', ''),
                            'Report A Last Modified': comp.get('report_a_modified_date', ''),
                            'Report B Modified By': comp.get('report_b_modified_by', ''),
                            'Report B Last Modified': comp.get('report_b_modified_date', ''),
                            'Recommended Action': _recommended_action(comp['similarity_score'])
                        })

                    df_detailed = pd.DataFrame(detailed_data)
                    df_detailed.to_excel(writer, sheet_name='Detailed Breakdown', index=False)

                # Now apply formatting to the workbook
                wb = load_workbook(output)

                # Define styles
                header_fill = PatternFill(start_color='667EEA', end_color='667EEA', fill_type='solid')
                header_font = Font(bold=True, color='FFFFFF', size=11)
                header_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
                thin_border = Border(
                    left=Side(style='thin', color='E2E8F0'),
                    right=Side(style='thin', color='E2E8F0'),
                    top=Side(style='thin', color='E2E8F0'),
                    bottom=Side(style='thin', color='E2E8F0')
                )

                # Format each sheet
                for sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]

                    # Format header row + locate columns needing special treatment
                    # (percentage number format, clickable report links) by header text.
                    percent_cols = set()
                    link_cols = set()
                    for cell in ws[1]:
                        cell.fill = header_fill
                        cell.font = header_font
                        cell.alignment = header_alignment
                        cell.border = thin_border
                        header_text = str(cell.value or '')
                        if header_text.endswith('%'):
                            percent_cols.add(cell.column)
                        elif header_text.endswith('Link'):
                            link_cols.add(cell.column)

                    # Apply percentage number formatting to score columns (values are
                    # already 0-100 scale, so a literal "%" suffix is used rather than
                    # dividing by 100 — display-only change, underlying value unchanged).
                    if percent_cols and ws.max_row > 1:
                        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
                            for cell in row:
                                if cell.column in percent_cols and isinstance(cell.value, (int, float)):
                                    cell.number_format = '0.0"%"'

                    # Turn report link columns into clickable hyperlinks
                    if link_cols and ws.max_row > 1:
                        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
                            for cell in row:
                                if cell.column in link_cols and cell.value:
                                    cell.hyperlink = cell.value
                                    cell.font = Font(color='0563C1', underline='single')

                    # Auto-adjust column widths
                    for column in ws.columns:
                        max_length = 0
                        column_letter = get_column_letter(column[0].column)

                        for cell in column:
                            try:
                                cell_length = len(str(cell.value))
                                if cell_length > max_length:
                                    max_length = cell_length
                            except:
                                pass

                        # Set width with limits
                        adjusted_width = min(max(max_length + 2, 12), 60)
                        ws.column_dimensions[column_letter].width = adjusted_width

                    # Freeze top row
                    ws.freeze_panes = 'A2'

                    # Enable auto-filter on header row
                    if ws.max_row > 1:
                        ws.auto_filter.ref = ws.dimensions

                    # Apply borders to all cells
                    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, min_col=1, max_col=ws.max_column):
                        for cell in row:
                            cell.border = thin_border
                            if cell.row > 1:  # Data rows
                                cell.alignment = Alignment(vertical='top', wrap_text=True)

                # Special formatting for Summary sheet
                if 'Summary' in wb.sheetnames:
                    ws_summary = wb['Summary']
                    ws_summary['A1'].font = Font(bold=True, size=12)
                    ws_summary.column_dimensions['A'].width = 30
                    ws_summary.column_dimensions['B'].width = 40

                # Save formatted workbook
                output = io.BytesIO()
                wb.save(output)
                output.seek(0)

                filename = f"similarity_analysis_{workspace_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

                print(f"\n📥 Excel export ready!")
                print(f"   Filename: {filename}")
                print(f"   File size: {output.getbuffer().nbytes} bytes")
                print(f"   Sending file to client...")
                similarity_logger.info(f"Excel export ready: {filename} ({output.getbuffer().nbytes} bytes)")

                return send_file(
                    output,
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    as_attachment=True,
                    download_name=filename
                )

            except Exception as e:
                print(f"❌ Error creating Excel: {str(e)}")
                import traceback
                traceback.print_exc()
                # Fallback to CSV
                export_format = 'csv'

        if export_format == 'csv':  # CSV format
            import csv

            output = io.StringIO()
            writer = csv.writer(output)

            # Write summary
            writer.writerow(['Power BI Similarity Analysis Report'])
            writer.writerow(['Generated:', datetime.now().strftime('%Y-%m-%d %H:%M:%S')])
            writer.writerow(['Workspace ID:', workspace_id])
            writer.writerow(['Total Reports:', len(reports_data)])
            writer.writerow(['Similar Pairs:', len(comparisons)])
            if workspace_scan_note:
                writer.writerow(['Note:', workspace_scan_note])
            writer.writerow([])

            # Write headers with enhanced columns (grouped: identity, scores, counts,
            # names, metadata/action — mirrors the Excel export column order)
            writer.writerow([
                'Report A', 'Workspace A', 'Report A ID', 'Report A Link',
                'Report B', 'Workspace B', 'Report B ID', 'Report B Link', 'Cross-Workspace',
                'Overall %', 'DAX Logic %', 'Schema %', 'Pages %', 'Visuals %',
                'Common Measures', 'Logic-Matched Measures', 'Common Tables', 'Common Pages',
                'Identical Visuals', 'Similar Visuals',
                'Unique Tables (A)', 'Unique Tables (B)',
                'Unique Measures (A)', 'Unique Measures (B)',
                'Unique Pages (A)', 'Unique Pages (B)',
                'Report A Modified By', 'Report A Last Modified',
                'Report B Modified By', 'Report B Last Modified',
                'Recommended Action'
            ])

            # Write data
            for comp in comparisons:
                writer.writerow([
                    comp['report_a_name'],
                    comp.get('report_a_workspace', ''),
                    comp.get('report_a_id', ''),
                    _powerbi_report_url(comp.get('report_a_workspace_id'), comp.get('report_a_id')),
                    comp['report_b_name'],
                    comp.get('report_b_workspace', ''),
                    comp.get('report_b_id', ''),
                    _powerbi_report_url(comp.get('report_b_workspace_id'), comp.get('report_b_id')),
                    comp.get('is_cross_workspace', 'No'),
                    comp['similarity_score'],
                    comp['dax_similarity'],
                    comp['table_similarity'],
                    comp['page_similarity'],
                    comp.get('visual_similarity', 0),
                    comp['identical_measures'],
                    comp.get('logic_matched_measures', ''),
                    comp['identical_tables'],
                    comp['identical_pages'],
                    comp.get('identical_visuals_count', 0),
                    comp.get('similar_visuals_count', 0),
                    comp['unique_tables_a'],
                    comp['unique_tables_b'],
                    comp['unique_measures_a'],
                    comp['unique_measures_b'],
                    comp['unique_pages_a'],
                    comp['unique_pages_b'],
                    comp.get('report_a_modified_by', ''),
                    comp.get('report_a_modified_date', ''),
                    comp.get('report_b_modified_by', ''),
                    comp.get('report_b_modified_date', ''),
                    _recommended_action(comp['similarity_score'])
                ])

            output.seek(0)
            filename = f"similarity_analysis_{workspace_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

            csv_bytes = output.getvalue().encode('utf-8')
            print(f"\n📄 CSV export ready!")
            print(f"   Filename: {filename}")
            print(f"   File size: {len(csv_bytes)} bytes")
            print(f"   Sending file to client...")
            similarity_logger.info(f"CSV export ready: {filename} ({len(csv_bytes)} bytes)")

            return send_file(
                io.BytesIO(csv_bytes),
                mimetype='text/csv',
                as_attachment=True,
                download_name=filename
            )

    except Exception as e:
        print(f"❌ Error in export: {str(e)}")
        similarity_logger.error(f"Error in export: {str(e)}", exc_info=True)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500



def calculate_report_similarity(report_a, report_b):
    """
    Calculate similarity between two reports based on DAX, structure, and visuals.
    Enhanced with logic-based DAX comparison and visual-level analysis.
    """
    from difflib import SequenceMatcher
    import re

    scores = {
        'dax_similarity': 0,
        'table_similarity': 0,
        'page_similarity': 0,
        'visual_similarity': 0,
        'overall_score': 0
    }

    details = {
        'identical_measures': [],
        'logic_matched_measures': [],  # NEW: Measures with same logic but different names
        'similar_measures': [],
        'unique_to_a': [],
        'unique_to_b': [],
        'identical_tables': [],
        'unique_tables_a': [],
        'unique_tables_b': [],
        'identical_pages': [],
        'unique_pages_a': [],
        'unique_pages_b': [],
        'identical_visuals': [],  # NEW: Visuals with same type and fields
        'similar_visuals': []  # NEW: Visuals with partial field overlap
    }

    # Helper function to normalize DAX expressions for logic comparison
    def normalize_dax(expression):
        """Normalize DAX expression by removing whitespace and converting to lowercase"""
        if not expression:
            return ""
        # Remove all whitespace
        normalized = re.sub(r'\s+', '', str(expression))
        # Convert to lowercase for case-insensitive comparison
        return normalized.lower()

    # 1. ENHANCED: Compare DAX Measures with Logic-Based Matching
    measures_a = {m['name']: m['expression'] for m in report_a.get('measures', [])}
    measures_b = {m['name']: m['expression'] for m in report_b.get('measures', [])}

    # Create lookup of normalized expressions to find logic matches
    expr_to_names_a = {}  # {normalized_expr: [measure_names]}
    expr_to_names_b = {}

    for name, expr in measures_a.items():
        norm_expr = normalize_dax(expr)
        if norm_expr not in expr_to_names_a:
            expr_to_names_a[norm_expr] = []
        expr_to_names_a[norm_expr].append(name)

    for name, expr in measures_b.items():
        norm_expr = normalize_dax(expr)
        if norm_expr not in expr_to_names_b:
            expr_to_names_b[norm_expr] = []
        expr_to_names_b[norm_expr].append(name)

    all_measure_names = set(measures_a.keys()) | set(measures_b.keys())
    if all_measure_names:
        identical_count = 0
        logic_matched_count = 0
        similar_count = 0
        matched_names = set()  # Track which measures have been matched

        # First pass: Exact name matches
        for name in list(all_measure_names):
            if name in measures_a and name in measures_b:
                expr_a = measures_a[name]
                expr_b = measures_b[name]

                if expr_a == expr_b:
                    identical_count += 1
                    details['identical_measures'].append(name)
                    matched_names.add(name)
                else:
                    # Calculate expression similarity
                    ratio = SequenceMatcher(None, str(expr_a), str(expr_b)).ratio()
                    if ratio >= 0.8:  # 80% similar
                        similar_count += 1
                        details['similar_measures'].append({'name': name, 'similarity': round(ratio * 100)})
                        matched_names.add(name)

        # Second pass: Logic-based matching (different names, same logic)
        for norm_expr_a, names_a in expr_to_names_a.items():
            if norm_expr_a in expr_to_names_b:
                names_b = expr_to_names_b[norm_expr_a]
                # Found measures with identical logic but potentially different names
                for name_a in names_a:
                    for name_b in names_b:
                        if name_a != name_b and name_a not in matched_names and name_b not in matched_names:
                            logic_matched_count += 1
                            details['logic_matched_measures'].append({
                                'measure_a': name_a,
                                'measure_b': name_b,
                                'expression': measures_a[name_a]
                            })
                            matched_names.add(name_a)
                            matched_names.add(name_b)
                            break  # Only match each measure once

        # Identify truly unique measures
        for name in all_measure_names:
            if name not in matched_names:
                if name in measures_a:
                    details['unique_to_a'].append(name)
                else:
                    details['unique_to_b'].append(name)

        # Calculate DAX similarity score (logic matches count as identical)
        total_matches = identical_count + logic_matched_count + similar_count * 0.5
        scores['dax_similarity'] = round((total_matches / len(all_measure_names)) * 100) if all_measure_names else 0

    # 2. Compare Tables/Data Model
    tables_a = set([t.get('name') for t in report_a.get('tables', [])])
    tables_b = set([t.get('name') for t in report_b.get('tables', [])])

    if tables_a or tables_b:
        common_tables = tables_a & tables_b
        details['identical_tables'] = list(common_tables)
        details['unique_tables_a'] = list(tables_a - tables_b)
        details['unique_tables_b'] = list(tables_b - tables_a)

        all_tables = tables_a | tables_b
        scores['table_similarity'] = round((len(common_tables) / len(all_tables)) * 100) if all_tables else 0

    # 3. Compare Page Structure
    pages_a = set([p.get('displayName', p.get('name', '')) for p in report_a.get('pages', [])])
    pages_b = set([p.get('displayName', p.get('name', '')) for p in report_b.get('pages', [])])

    if pages_a or pages_b:
        common_pages = pages_a & pages_b
        details['identical_pages'] = list(common_pages)
        details['unique_pages_a'] = list(pages_a - pages_b)
        details['unique_pages_b'] = list(pages_b - pages_a)

        all_pages = pages_a | pages_b
        scores['page_similarity'] = round((len(common_pages) / len(all_pages)) * 100) if all_pages else 0

    # 4. NEW: Compare Visual-Level Similarity
    visuals_a = report_a.get('visuals', [])
    visuals_b = report_b.get('visuals', [])

    if visuals_a or visuals_b:
        visual_matches = 0
        visual_partial_matches = 0

        for vis_a in visuals_a:
            vis_a_type = vis_a.get('visual_type', '')
            vis_a_title = vis_a.get('title', '')
            vis_a_fields = set(vis_a.get('fields', []))

            for vis_b in visuals_b:
                vis_b_type = vis_b.get('visual_type', '')
                vis_b_title = vis_b.get('title', '')
                vis_b_fields = set(vis_b.get('fields', []))

                # Check if visuals are identical (same type, title, and fields)
                if vis_a_type == vis_b_type and vis_a_title == vis_b_title and vis_a_fields == vis_b_fields:
                    visual_matches += 1
                    details['identical_visuals'].append({
                        'type': vis_a_type,
                        'title': vis_a_title,
                        'fields': list(vis_a_fields)
                    })
                    break
                # Check for partial matches (same type and overlapping fields)
                elif vis_a_type == vis_b_type and vis_a_fields and vis_b_fields:
                    common_fields = vis_a_fields & vis_b_fields
                    if len(common_fields) / max(len(vis_a_fields), len(vis_b_fields)) >= 0.7:  # 70% field overlap
                        visual_partial_matches += 1
                        details['similar_visuals'].append({
                            'type': vis_a_type,
                            'title_a': vis_a_title,
                            'title_b': vis_b_title,
                            'common_fields': list(common_fields),
                            'overlap_ratio': round(len(common_fields) / max(len(vis_a_fields), len(vis_b_fields)), 2)
                        })
                        break

        total_visuals = max(len(visuals_a), len(visuals_b))
        if total_visuals > 0:
            scores['visual_similarity'] = round(((visual_matches + visual_partial_matches * 0.5) / total_visuals) * 100)
        else:
            scores['visual_similarity'] = 0
    else:
        scores['visual_similarity'] = 0

    # Calculate overall similarity (weighted average)
    # DAX = 35%, Tables = 25%, Pages = 20%, Visuals = 20%
    scores['overall_score'] = round(
        scores['dax_similarity'] * 0.35 +
        scores['table_similarity'] * 0.25 +
        scores['page_similarity'] * 0.20 +
        scores['visual_similarity'] * 0.20
    )

    return {
        'scores': scores,
        'details': details,
        'overall_score': scores['overall_score']
    }



@similarity_bp.route('/api/similarity-analysis')
@login_required
def get_similarity_analysis():
    """
    Multi-type similarity analysis.
    Query params:
      - workspace_id (required)
      - type: reports | visuals | measures | tables
      - threshold: 0.0-1.0 (default 0.3)

    Data sources (same family as Semantic Models):
      - tables / report structure: catalog (columns fast)
      - DAX measures: same workspace Scanner enrich cache used by
        /api/semantic-model-details?enrich=1  (_get_workspace_scan_cached)
      - visuals: full Scanner workspace payload (pages/visuals) when available
    """
    import time
    try:
        workspace_id = request.args.get('workspace_id')
        analysis_type = (request.args.get('type') or 'reports').strip().lower()
        try:
            threshold = float(request.args.get('threshold', 0.3))
        except (TypeError, ValueError):
            threshold = 0.3
        threshold = max(0.0, min(1.0, threshold))

        if not workspace_id:
            return jsonify({'success': False, 'error': 'workspace_id is required'}), 400
        if analysis_type not in {'reports', 'visuals', 'measures', 'tables'}:
            return jsonify({
                'success': False,
                'error': f"Unsupported type '{analysis_type}'. Use reports|visuals|measures|tables"
            }), 400

        print(f"\n🔍 SIMILARITY ANALYSIS: type={analysis_type} ws={workspace_id} thr={threshold}")
        similarity_logger.info(f"SIMILARITY ANALYSIS: type={analysis_type} ws={workspace_id} thr={threshold}")

        # Catalog-first payload for reports/tables
        catalog_ws, catalog_datasets = _similarity_catalog_workspace(workspace_id)

        # Semantic-models style dataset schema cache (measures + tables with expressions)
        # Shared with /api/semantic-model-details enrich — one scan serves both UIs.
        schema_by_id = {}
        scan_ws = None
        scan_note = None
        schema_source = None

        def _load_schema_cache():
            nonlocal schema_by_id, schema_source, scan_note
            try:
                schema_by_id = _get_workspace_scan_cached(workspace_id) or {}
                schema_source = 'semantic_scan_cache'
                print(f"   📦 schema cache datasets={len(schema_by_id)}")
            except Exception as e:
                scan_note = f'Schema enrich scan failed: {e}'
                print(f"   ⚠️ {scan_note}")
                schema_by_id = {}

        def _load_full_scan_ws():
            """Full workspace scan (needed for report pages/visuals)."""
            nonlocal scan_ws, scan_note
            try:
                from scanner_connector import PowerBIScanner
                scanner = PowerBIScanner()
                scan_result = scanner.run_scan(workspace_id=workspace_id) or {}
                for ws in scan_result.get('workspaces') or []:
                    if ws.get('id') == workspace_id:
                        scan_ws = ws
                        break
                if not scan_ws and (scan_result.get('workspaces') or []):
                    scan_ws = (scan_result.get('workspaces') or [None])[0]
                if not scan_ws:
                    scan_note = (scan_note + '; ' if scan_note else '') + 'Scanner returned no workspace payload'
                else:
                    # Also seed schema cache from this full scan when empty
                    if not schema_by_id:
                        by_id = {}
                        for ds in scan_ws.get('datasets') or []:
                            did = ds.get('id')
                            if did:
                                by_id[did] = ds
                        if by_id:
                            schema_by_id.update(by_id)
                            with _get_semantic_scan_lock():
                                _get_semantic_scan_cache()[workspace_id] = {
                                    'ts': time.time(),
                                    'by_id': dict(by_id),
                                }
            except Exception as e:
                scan_note = f'Scanner failed: {e}'
                print(f"   ⚠️ {scan_note}")

        notes = []
        data_src = {'catalog': bool(catalog_ws), 'scanner': False, 'schema_cache': False}

        if analysis_type == 'tables':
            # Catalog columns are enough; optional schema enrich not required
            matches = _sim_analyze_tables(catalog_ws, catalog_datasets, None, threshold)

        elif analysis_type == 'reports':
            # Prefer catalog structure; enrich measure names from schema cache if available
            # (same source Semantic Models Details uses for DAX)
            if not catalog_ws:
                _load_schema_cache()
            else:
                # cheap: use cache if already warm; don't force scan for report mode
                with _get_semantic_scan_lock():
                    hit = _get_semantic_scan_cache().get(workspace_id)
                if hit and hit.get('by_id'):
                    schema_by_id = hit.get('by_id') or {}
                    schema_source = 'semantic_scan_cache_warm'
            scan_ws_stub = {'datasets': list(schema_by_id.values())} if schema_by_id else None
            matches = _sim_analyze_reports(catalog_ws, catalog_datasets, scan_ws_stub, threshold)
            data_src['schema_cache'] = bool(schema_by_id)

        elif analysis_type == 'measures':
            # Same DAX source as Semantic Models Details (workspace scan cache)
            _load_schema_cache()
            data_src['schema_cache'] = bool(schema_by_id)
            data_src['scanner'] = bool(schema_by_id)
            matches, mnote = _sim_analyze_measures(
                catalog_ws,
                catalog_datasets,
                schema_by_id,
                threshold,
                schema_source=schema_source or 'scanner',
            )
            if mnote:
                notes.append(mnote)

        else:  # visuals — still need full report pages payload
            _load_full_scan_ws()
            data_src['scanner'] = bool(scan_ws)
            matches, vnote = _sim_analyze_visuals(scan_ws, threshold)
            if vnote:
                notes.append(vnote)

        if scan_note and analysis_type in {'visuals', 'measures'} and not matches:
            notes.append(scan_note)

        # Cap huge pairwise result sets for UI responsiveness
        max_rows = 500
        truncated = len(matches) > max_rows
        if truncated:
            matches = matches[:max_rows]
            notes.append(f'Showing top {max_rows} matches (truncated)')

        print(f"   ✅ {analysis_type}: {len(matches)} matches")
        similarity_logger.info(f"{analysis_type}: {len(matches)} matches (truncated={truncated})")

        return jsonify({
            'success': True,
            'workspace_id': workspace_id,
            'analysis_type': analysis_type,
            'threshold': threshold,
            'matches': matches,
            'total_matches': len(matches),
            'truncated': truncated,
            'notes': notes,
            'data_source': data_src,
        })

    except Exception as e:
        print(f"❌ Error in similarity analysis: {str(e)}")
        similarity_logger.error(f"Error in similarity analysis: {str(e)}", exc_info=True)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


def _similarity_catalog_workspace(workspace_id):
    """Return (workspace_dict_or_None, datasets_by_id) from catalog."""
    if not _catalog_available():
        return None, {}
    try:
        cat = _get_catalog_service().get_workspace_catalog() or {}
    except Exception as e:
        print(f"   ⚠️ catalog load failed for similarity: {e}")
        return None, {}
    datasets = cat.get('datasets') or {}
    ws = next((w for w in (cat.get('workspaces') or []) if w.get('id') == workspace_id), None)
    return ws, datasets


def _sim_dataset_tables(dataset_obj):
    """Normalize Scanner/catalog dataset → list of table dicts with columns/measures."""
    if not dataset_obj:
        return []
    # Catalog & modern Scanner: tables at top-level. Legacy some payloads nested under model.
    tables = dataset_obj.get('tables')
    if tables is None:
        tables = (dataset_obj.get('model') or {}).get('tables') or []
    return tables or []


def _sim_col_name(col):
    if isinstance(col, dict):
        return (col.get('name') or col.get('column') or '').strip()
    return str(col or '').strip()


def _sim_measure_expr(measure):
    if not isinstance(measure, dict):
        return '', ''
    name = (measure.get('name') or '').strip()
    expr = (
        measure.get('expression')
        or measure.get('Expression')
        or measure.get('dax')
        or ''
    )
    return name, str(expr).strip()


def _sim_extract_visual_fields(visual):
    """Best-effort field extraction across Scanner visual shapes."""
    fields = set()
    if not isinstance(visual, dict):
        return fields

    def add_field(val):
        if val is None:
            return
        if isinstance(val, dict):
            name = (
                val.get('name')
                or val.get('column')
                or val.get('measure')
                or val.get('field')
                or val.get('queryRef')
                or val.get('nativeQueryRef')
                or ''
            )
            if name:
                fields.add(str(name).split('.')[-1].strip('[]'))
            return
        s = str(val).strip()
        if s:
            fields.add(s.split('.')[-1].strip('[]'))

    # Common buckets
    for key in (
        'columns', 'measures', 'values', 'categories', 'rows', 'series',
        'fields', 'projections', 'dataRoles', 'selects',
    ):
        bucket = visual.get(key)
        if isinstance(bucket, list):
            for item in bucket:
                add_field(item)
        elif isinstance(bucket, dict):
            for item in bucket.values():
                if isinstance(item, list):
                    for x in item:
                        add_field(x)
                else:
                    add_field(item)

    # Nested payload used by some scanner dumps
    for nest_key in ('query', 'config', 'prototypeQuery', 'visual'):
        nest = visual.get(nest_key)
        if isinstance(nest, dict):
            for k, v in nest.items():
                if k.lower() in {
                    'columns', 'measures', 'values', 'categories', 'rows', 'fields', 'select'
                }:
                    if isinstance(v, list):
                        for item in v:
                            add_field(item)

    return {f for f in fields if f}


def _sim_analyze_reports(catalog_ws, catalog_datasets, scan_ws, threshold):
    """Pairwise report similarity via shared table/measure names on bound datasets."""
    datasets_map = {}
    reports = []

    if catalog_ws:
        reports = list(catalog_ws.get('reports') or [])
        # Prefer global catalog datasets map; fall back to workspace-local datasets
        datasets_map = dict(catalog_datasets or {})
        for d in catalog_ws.get('datasets') or []:
            if d.get('id') and d['id'] not in datasets_map:
                datasets_map[d['id']] = d

    if scan_ws:
        if not reports:
            reports = list(scan_ws.get('reports') or [])
        for d in scan_ws.get('datasets') or []:
            did = d.get('id')
            if did:
                # Scanner often has richer measure expressions — prefer if present
                existing = datasets_map.get(did)
                scan_tables = _sim_dataset_tables(d)
                if not existing:
                    datasets_map[did] = d
                else:
                    # merge: if catalog tables lack measures, keep scanner tables
                    cat_tables = _sim_dataset_tables(existing)
                    cat_has_meas = any((t.get('measures') or []) for t in cat_tables)
                    scan_has_meas = any((t.get('measures') or []) for t in scan_tables)
                    if scan_has_meas and not cat_has_meas:
                        datasets_map[did] = d

    profiles = []
    for report in reports:
        name = report.get('name') or ''
        if _is_excluded_report_name(name):
            continue
        ds_id = report.get('datasetId') or ''
        ds = datasets_map.get(ds_id) or {}
        tables = set()
        measures = set()
        for t in _sim_dataset_tables(ds):
            tname = (t.get('name') or '').strip()
            if tname:
                tables.add(tname)
            for m in t.get('measures') or []:
                if isinstance(m, dict):
                    mn = (m.get('name') or '').strip()
                else:
                    mn = str(m or '').strip()
                if mn:
                    measures.add(mn)
            # some catalogs store measureCount only — still useful via columns for table sim
        if not tables and not measures:
            # still include with empty sets only if dataset known? skip empty noise
            continue
        profiles.append({
            'id': report.get('id'),
            'name': name,
            'dataset_id': ds_id,
            'dataset_name': ds.get('name') or report.get('datasetName') or '',
            'tables': tables,
            'measures': measures,
        })

    matches = []
    for i in range(len(profiles)):
        for j in range(i + 1, len(profiles)):
            r1, r2 = profiles[i], profiles[j]
            # Same dataset means effectively identical model — still valid similarity
            common_tables = r1['tables'] & r2['tables']
            common_measures = r1['measures'] & r2['measures']
            all_tables = r1['tables'] | r2['tables']
            all_measures = r1['measures'] | r2['measures']
            if not all_tables and not all_measures:
                continue
            table_sim = (len(common_tables) / len(all_tables)) if all_tables else 0.0
            measure_sim = (len(common_measures) / len(all_measures)) if all_measures else 0.0
            if all_tables and all_measures:
                score = (table_sim + measure_sim) / 2.0
            elif all_tables:
                score = table_sim
            else:
                score = measure_sim
            if score >= threshold:
                matches.append({
                    'report1_id': r1['id'],
                    'report1_name': r1['name'],
                    'report2_id': r2['id'],
                    'report2_name': r2['name'],
                    'score': round(score, 3),
                    'common_tables': sorted(common_tables)[:40],
                    'common_tables_count': len(common_tables),
                    'common_measures': sorted(common_measures)[:40],
                    'common_measures_count': len(common_measures),
                    'dataset1_name': r1['dataset_name'],
                    'dataset2_name': r2['dataset_name'],
                    'same_dataset': bool(r1['dataset_id'] and r1['dataset_id'] == r2['dataset_id']),
                })
    matches.sort(key=lambda x: x['score'], reverse=True)
    return matches


def _sim_analyze_tables(catalog_ws, catalog_datasets, scan_ws, threshold):
    """Pairwise table similarity by shared column names."""
    datasets = []
    seen = set()

    def add_ds(d):
        if not d:
            return
        did = d.get('id') or id(d)
        if did in seen:
            return
        seen.add(did)
        datasets.append(d)

    if catalog_ws:
        # Prefer global dataset map entries for this workspace
        ws_id = catalog_ws.get('id')
        for d in (catalog_datasets or {}).values():
            if d.get('workspaceId') == ws_id or d.get('workspace_id') == ws_id:
                add_ds(d)
        for d in catalog_ws.get('datasets') or []:
            full = (catalog_datasets or {}).get(d.get('id')) or d
            add_ds(full)

    if scan_ws:
        for d in scan_ws.get('datasets') or []:
            add_ds(d)

    all_tables = []
    for dataset in datasets:
        ds_name = dataset.get('name') or ''
        for table in _sim_dataset_tables(dataset):
            cols = set()
            for c in table.get('columns') or []:
                cn = _sim_col_name(c)
                if cn:
                    cols.add(cn)
            if not cols:
                continue
            source_type = (
                table.get('sourceTypeLabel')
                or table.get('source_type')
                or 'Unknown'
            )
            if source_type == 'Unknown':
                for partition in table.get('partitions') or []:
                    source = partition.get('source') or {}
                    if isinstance(source, dict):
                        if source.get('type') == 'M':
                            source_type = 'M Query'
                        elif source.get('expression'):
                            source_type = 'DAX / expression'
            all_tables.append({
                'name': table.get('name') or '',
                'dataset': ds_name,
                'columns': cols,
                'source_type': source_type,
            })

    matches = []
    n = len(all_tables)
    # Limit O(n^2) explosion
    hard_cap = 400
    if n > hard_cap:
        all_tables = all_tables[:hard_cap]

    for i in range(len(all_tables)):
        for j in range(i + 1, len(all_tables)):
            t1, t2 = all_tables[i], all_tables[j]
            # skip exact same table name in same dataset
            if t1['dataset'] == t2['dataset'] and t1['name'] == t2['name']:
                continue
            common = t1['columns'] & t2['columns']
            union = t1['columns'] | t2['columns']
            if not union:
                continue
            score = len(common) / len(union)
            if score >= threshold:
                matches.append({
                    'table1_name': t1['name'],
                    'table2_name': t2['name'],
                    'dataset1': t1['dataset'],
                    'dataset2': t2['dataset'],
                    'score': round(score, 3),
                    'common_columns': sorted(common)[:50],
                    'common_columns_count': len(common),
                    'source_type': t1['source_type'],
                })
    matches.sort(key=lambda x: x['score'], reverse=True)
    return matches


def _sim_analyze_measures(catalog_ws, catalog_datasets, schema_source_obj, threshold, schema_source='scanner'):
    """
    Pairwise measure similarity using normalized DAX.

    schema_source_obj can be:
      - dict of dataset_id -> dataset  (from _get_workspace_scan_cached — same as Semantic Models Details)
      - workspace scan dict with .datasets list
      - None
    """
    from difflib import SequenceMatcher

    all_measures = []
    sources_tried = []

    def collect_from_datasets(datasets, label):
        count = 0
        for dataset in datasets or []:
            if not dataset:
                continue
            ds_name = dataset.get('name') or ''
            ds_id = dataset.get('id') or ''
            before = count
            # Same flatten helper as /api/semantic-model-details
            flat_measures, _rels = _extract_measures_and_relationships(dataset)
            for m in flat_measures or []:
                name = (m.get('name') or '').strip()
                dax = str(m.get('expression') or '').strip()
                if not dax:
                    continue
                all_measures.append({
                    'name': name or '(unnamed)',
                    'dax': dax,
                    'table': m.get('table') or '',
                    'dataset': ds_name,
                    'dataset_id': ds_id,
                    'normalized_dax': normalize_dax(dax),
                })
                count += 1
            # Per-dataset fallback if flatten had no expressions
            if count == before:
                for table in _sim_dataset_tables(dataset):
                    tname = table.get('name') or ''
                    for measure in table.get('measures') or []:
                        name, dax = _sim_measure_expr(measure)
                        if not dax:
                            continue
                        all_measures.append({
                            'name': name or '(unnamed)',
                            'dax': dax,
                            'table': tname,
                            'dataset': ds_name,
                            'dataset_id': ds_id,
                            'normalized_dax': normalize_dax(dax),
                        })
                        count += 1
        sources_tried.append(f'{label}:{count}')
        return count

    # 1) Preferred: Semantic Models schema cache (dataset id → dataset with measures/expressions)
    if isinstance(schema_source_obj, dict) and schema_source_obj:
        # Heuristic: cache map keys are dataset GUIDs; workspace object has 'datasets' list
        if 'datasets' in schema_source_obj and isinstance(schema_source_obj.get('datasets'), list):
            collect_from_datasets(schema_source_obj.get('datasets') or [], schema_source or 'scanner_ws')
        else:
            # Treat as by_id map (possibly mixed with non-dataset keys — only keep dict values that look like datasets)
            ds_list = []
            for k, v in schema_source_obj.items():
                if isinstance(v, dict) and (v.get('tables') is not None or v.get('name') or v.get('id')):
                    # skip accidental nested structures
                    if k in {'datasets', 'reports', 'dashboards', 'workspaces'}:
                        continue
                    ds_list.append(v)
            collect_from_datasets(ds_list, schema_source or 'semantic_scan_cache')

    # 2) Catalog fallback (usually has columns, rarely DAX expressions today)
    if not all_measures and catalog_ws:
        ws_id = catalog_ws.get('id')
        ds_list = []
        for d in (catalog_datasets or {}).values():
            if d.get('workspaceId') == ws_id or d.get('workspace_id') == ws_id:
                ds_list.append(d)
        if not ds_list:
            for d in catalog_ws.get('datasets') or []:
                ds_list.append((catalog_datasets or {}).get(d.get('id')) or d)
        collect_from_datasets(ds_list, 'catalog')

    note = None
    if not all_measures:
        note = (
            'No DAX measure expressions found for this workspace. '
            'Semantic Models Details uses the same Scanner enrich cache — open a model Details once '
            'to warm the cache, or ensure Admin Scanner returns datasetSchema + measures. '
            f"Tried: {', '.join(sources_tried) or 'none'}"
        )
        return [], note

    if len(all_measures) > 600:
        all_measures = all_measures[:600]
        note = (
            f'Using {schema_source or "scanner"} DAX (same source as Semantic Models). '
            'Compared first 600 measures with expressions (capped).'
        )
    else:
        note = (
            f'Using {schema_source or "scanner"} DAX expressions '
            f'({len(all_measures)} measures — same enrich path as Semantic Models Details).'
        )

    matches = []
    for i in range(len(all_measures)):
        for j in range(i + 1, len(all_measures)):
            m1, m2 = all_measures[i], all_measures[j]
            if (
                m1.get('dataset_id') and m1.get('dataset_id') == m2.get('dataset_id')
                and m1['name'] == m2['name'] and m1['table'] == m2['table']
            ):
                continue
            if m1['dataset'] == m2['dataset'] and m1['name'] == m2['name'] and m1['table'] == m2['table']:
                continue
            ratio = SequenceMatcher(None, m1['normalized_dax'], m2['normalized_dax']).ratio()
            tokens1 = set(m1['normalized_dax'].split())
            tokens2 = set(m2['normalized_dax'].split())
            union = tokens1 | tokens2
            token_score = (len(tokens1 & tokens2) / len(union)) if union else 0.0
            score = max(ratio, token_score)
            if score >= threshold:
                matches.append({
                    'measure1_name': m1['name'],
                    'measure2_name': m2['name'],
                    'table1_name': m1['table'],
                    'table2_name': m2['table'],
                    'dataset1': m1['dataset'],
                    'dataset2': m2['dataset'],
                    'score': round(score, 3),
                    'dax1': m1['dax'][:500],
                    'dax2': m2['dax'][:500],
                    'dax_pattern': extract_dax_pattern(m1['dax']),
                })
    matches.sort(key=lambda x: x['score'], reverse=True)
    return matches, note


def _sim_analyze_visuals(scan_ws, threshold):
    """Pairwise visual similarity by type + shared fields (Scanner pages/visuals)."""
    if not scan_ws:
        return [], (
            'Visual similarity requires Admin Scanner report pages/visuals. '
            'Scan returned no workspace data for this id.'
        )

    all_visuals = []
    reports_with_pages = 0
    for report in scan_ws.get('reports') or []:
        name = report.get('name') or ''
        if _is_excluded_report_name(name):
            continue
        pages = report.get('pages') or []
        if pages:
            reports_with_pages += 1
        for page in pages:
            page_name = page.get('displayName') or page.get('name') or ''
            for visual in page.get('visuals') or []:
                fields = _sim_extract_visual_fields(visual)
                vtype = (
                    visual.get('type')
                    or visual.get('visualType')
                    or visual.get('visualTypeName')
                    or 'Unknown'
                )
                title = (
                    visual.get('title')
                    or visual.get('name')
                    or visual.get('displayName')
                    or 'Untitled'
                )
                all_visuals.append({
                    'report_name': name,
                    'page_name': page_name,
                    'visual_title': title,
                    'visual_type': vtype,
                    'fields': fields,
                })

    if not all_visuals:
        if reports_with_pages == 0:
            return [], (
                'No report pages/visuals in Scanner payload. '
                'Tenant Scanner often omits visual metadata — report/table/measure modes still work.'
            )
        return [], 'Pages found but no visuals extracted from Scanner payload.'

    # Cap
    if len(all_visuals) > 800:
        all_visuals = all_visuals[:800]

    matches = []
    for i in range(len(all_visuals)):
        for j in range(i + 1, len(all_visuals)):
            v1, v2 = all_visuals[i], all_visuals[j]
            if v1['visual_type'] != v2['visual_type']:
                continue
            # avoid comparing a visual to itself on same report/page/title with no fields
            common = v1['fields'] & v2['fields']
            union = v1['fields'] | v2['fields']
            if not union:
                # Same type only → weak score; skip empty field pairs
                continue
            score = len(common) / len(union)
            if score >= threshold:
                matches.append({
                    'visual1_title': v1['visual_title'],
                    'visual2_title': v2['visual_title'],
                    'visual_type': v1['visual_type'],
                    'report1_name': v1['report_name'],
                    'report2_name': v2['report_name'],
                    'page1': v1['page_name'],
                    'page2': v2['page_name'],
                    'score': round(score, 3),
                    'common_fields': sorted(common)[:40],
                    'common_fields_count': len(common),
                })
    matches.sort(key=lambda x: x['score'], reverse=True)
    return matches, None


def normalize_dax(dax_expr):
    """Normalize DAX expression for comparison"""
    import re
    # Remove whitespace, convert to lowercase, remove comments
    normalized = re.sub(r'--.*', '', dax_expr)  # Remove comments
    normalized = re.sub(r'/\*.*?\*/', '', normalized, flags=re.DOTALL)  # Remove block comments
    normalized = normalized.lower()
    normalized = re.sub(r'\s+', ' ', normalized)  # Normalize whitespace
    return normalized.strip()


def extract_dax_pattern(dax_expr):
    """Extract common DAX pattern (e.g., SUM, CALCULATE, etc.)"""
    import re
    # Find main DAX function
    match = re.search(r'(\w+)\s*\(', dax_expr.strip())
    if match:
        return match.group(1).upper()
    return 'Custom'
