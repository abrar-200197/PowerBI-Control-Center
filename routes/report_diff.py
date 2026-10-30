"""Report Differences blueprint — compare two PBIP project zips (e.g. UAT vs
PROD exports of the same report) and list structural differences.

NOTE: `login_required` lives in app.py. It is intentionally NOT imported at
module level here — doing so causes a circular import when app.py is run
directly as a script (`python app.py`), same reasoning as routes/catalog.py
and routes/similarity.py. Resolved lazily at request time instead.
"""
import os
import shutil
import tempfile
from functools import wraps

from flask import Blueprint, jsonify, render_template, request

report_diff_bp = Blueprint('report_diff', __name__)

# Per-file cap matching the ~1.5 GB PBIP export scale confirmed for this
# feature, with headroom for zip overhead.
MAX_UPLOAD_BYTES = 1700 * 1024 * 1024


def login_required(f):
    """Lazy proxy for app.login_required (avoids circular import at load time)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not hasattr(wrapper, '_decorated'):
            from app import login_required as _real_login_required
            wrapper._decorated = _real_login_required(f)
        return wrapper._decorated(*args, **kwargs)
    return wrapper


@report_diff_bp.route('/report-differences')
@login_required
def report_differences_page():
    """Report Differences page — upload two PBIP zips and compare them."""
    return render_template('report_diff.html')


@report_diff_bp.route('/api/report-differences/compare', methods=['POST'])
@login_required
def api_compare_report_differences():
    """Compare two uploaded PBIP project zips (multipart form fields
    'version_a' and 'version_b') and return a structural diff."""
    from features.pbip_differ import diff_pbip
    from features.pbip_parser import parse_pbip_zip

    file_a = request.files.get('version_a')
    file_b = request.files.get('version_b')
    if not file_a or not file_a.filename or not file_b or not file_b.filename:
        return jsonify({'success': False, 'error': 'Both version_a and version_b zip files are required'}), 400

    tmp_dir = tempfile.mkdtemp(prefix='pbip_diff_')
    path_a = os.path.join(tmp_dir, 'version_a.zip')
    path_b = os.path.join(tmp_dir, 'version_b.zip')
    try:
        file_a.save(path_a)
        file_b.save(path_b)

        for label, path in (('version_a', path_a), ('version_b', path_b)):
            size = os.path.getsize(path)
            if size > MAX_UPLOAD_BYTES:
                return jsonify({
                    'success': False,
                    'error': f'{label} is too large ({size / (1024 * 1024):.0f} MB); limit is '
                             f'{MAX_UPLOAD_BYTES / (1024 * 1024):.0f} MB',
                }), 413

        print(f"\n📊 REPORT DIFFERENCES: comparing '{file_a.filename}' vs '{file_b.filename}'")
        parsed_a = parse_pbip_zip(path_a)
        parsed_b = parse_pbip_zip(path_b)
        result = diff_pbip(parsed_a, parsed_b)

        summary = {
            category: {bucket: len(items) for bucket, items in buckets.items()}
            for category, buckets in result.items()
        }
        print(f"   ✅ Diff summary: {summary}")

        return jsonify({'success': True, 'summary': summary, 'diff': result})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
