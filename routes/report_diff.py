"""Report Differences blueprint — compare two PBIP project zips (e.g. UAT vs
PROD exports of the same report) and list structural differences.

NOTE: `login_required` lives in app.py. It is intentionally NOT imported at
module level here — doing so causes a circular import when app.py is run
directly as a script (`python app.py`), same reasoning as routes/catalog.py
and routes/similarity.py. Resolved lazily at request time instead.
"""
import glob
import os
import shutil
import tempfile
import time
import zipfile
from functools import wraps

from flask import Blueprint, jsonify, render_template, request

report_diff_bp = Blueprint('report_diff', __name__)

# Per-file cap matching the ~1.5 GB PBIP export scale confirmed for this
# feature, with headroom for zip overhead.
MAX_UPLOAD_BYTES = 1700 * 1024 * 1024

# Stale leftover age threshold (seconds) — only prune folders older than this
# so we never touch another comparison that may still be in flight (the app
# runs multiple worker threads).
_STALE_CACHE_AGE_SECONDS = 15 * 60


def _strip_data_cache_from_zip(zip_path: str) -> int:
    """Rewrite the uploaded PBIP zip in place, dropping the semantic model's
    '.pbi/cache.abf' (an Analysis Services Backup of the model's actual DATA,
    written by Power BI Desktop when a PBIP is saved — see
    https://learn.microsoft.com/power-bi/developer/projects/projects-dataset).

    This file can be gigabytes for large semantic models and is only used by
    Desktop to preview data locally; the structural diff (measures, Power
    Query, relationships, visuals) never reads it. Stripping it up front
    keeps the size check and all downstream parsing proportional to the
    report's actual structure instead of its data volume, which is what was
    causing comparisons of large reports to fail.

    Returns the number of bytes removed.
    """
    with zipfile.ZipFile(zip_path, 'r') as zf:
        names = zf.namelist()
        drop = [n for n in names if os.path.basename(n.replace('\\', '/')).lower() == 'cache.abf']
        if not drop:
            return 0

        removed_bytes = sum(zf.getinfo(n).compress_size for n in drop)
        tmp_path = zip_path + '.stripped'
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as out_zf:
            for item in zf.infolist():
                if item.filename in drop:
                    continue
                out_zf.writestr(item, zf.read(item.filename))

    os.replace(tmp_path, zip_path)
    return removed_bytes


def _clear_stale_pbip_diff_cache() -> None:
    """Remove leftover 'pbip_diff_*' temp folders from previous comparisons.

    Each request normally cleans up its own temp folder in a `finally` block,
    but very large PBIP exports can crash/OOM-kill the worker mid-comparison
    (bypassing that cleanup), leaving multi-GB folders behind in the OS temp
    directory. Those leftovers were observed to make the *next* comparison of
    a large file fail (e.g. disk space exhaustion) until someone manually
    deleted them. Proactively clearing stale ones before every new upload
    avoids needing that manual step.
    """
    pattern = os.path.join(tempfile.gettempdir(), 'pbip_diff_*')
    now = time.time()
    for path in glob.glob(pattern):
        try:
            if not os.path.isdir(path):
                continue
            age = now - os.path.getmtime(path)
            if age < _STALE_CACHE_AGE_SECONDS:
                continue  # likely still in use by another in-flight comparison
            shutil.rmtree(path, ignore_errors=True)
            print(f"   🧹 Cleared stale report-differences cache folder: {path}")
        except Exception as exc:
            print(f"   ⚠️ Could not clear stale cache folder {path}: {exc}")


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

    # Clear out any stale leftover cache folders from previous (crashed/killed)
    # comparisons before starting this one — see _clear_stale_pbip_diff_cache.
    _clear_stale_pbip_diff_cache()

    tmp_dir = tempfile.mkdtemp(prefix='pbip_diff_')
    path_a = os.path.join(tmp_dir, 'version_a.zip')
    path_b = os.path.join(tmp_dir, 'version_b.zip')
    try:
        file_a.save(path_a)
        file_b.save(path_b)

        for label, path in (('version_a', path_a), ('version_b', path_b)):
            try:
                removed = _strip_data_cache_from_zip(path)
                if removed:
                    print(f"   🧹 Stripped cache.abf from {label} ({removed / (1024 * 1024):.0f} MB of model data not needed for comparison)")
            except Exception as exc:
                print(f"   ⚠️ Could not strip cache.abf from {label} (continuing with original file): {exc}")

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
