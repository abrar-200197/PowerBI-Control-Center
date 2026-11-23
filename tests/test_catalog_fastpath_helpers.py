"""Behavioral tests for the restored catalog fast-path helpers (offline).

These module-level helpers were lost by the blueprint-extraction commit
(28bdeb9) and restored from 4f74f7b (fd450d7^) on 2026-11-23. They are pure
functions - tested here with fabricated rows, no network, no Flask session.

Run standalone:  python tests/test_catalog_fastpath_helpers.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# UTF-8 stdout/stderr (repo pattern for cp1252 consoles).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import app  # noqa: E402  (module-level import; app.py is import-safe)


def test_catalog_reports_shell_ops_rows():
    rows = app._catalog_reports_shell([
        {
            'id': 'r1', 'name': 'Sales Overview', 'datasetId': 'd1',
            'ops_from_catalog': True, 'last_refreshed': '2026-11-20T05:00:00.000Z',
            'last_refresh_status': 'Completed', 'view_count': 42,
            'last_viewed': '2026-11-21T00:00:00Z', 'webUrl': 'https://app.powerbi.com/r1',
            'datasetName': 'Sales DS', 'reportType': 'Report',
        },
        {'id': 'r2', 'name': 'No Ops Yet', 'datasetId': ''},
    ])
    r1, r2 = rows
    # Row with ops data: fully populated, not pending
    assert r1['id'] == 'r1' and r1['name'] == 'Sales Overview'
    assert r1['datasetId'] == 'd1'
    assert r1['ops_from_catalog'] is True
    assert r1['refresh_pending'] is False
    assert r1['usage_loaded'] is True
    assert r1['from_catalog'] is True
    assert r1['webUrl'] == 'https://app.powerbi.com/r1'
    assert r1['has_documentation'] is False
    # Row without ops data: pending, usage not loaded
    assert r2['ops_from_catalog'] is False
    assert r2['refresh_pending'] is True
    assert r2['usage_loaded'] is False
    assert r2['datasetId'] == ''


def test_filter_reports_by_refresh_status():
    fresh = {'id': 'fresh', 'last_refreshed': '2026-11-22T00:00:00Z', 'refresh_type': 'scheduled'}
    stale = {'id': 'stale', 'last_refreshed': '2020-01-01T00:00:00Z', 'refresh_type': 'scheduled'}
    live = {'id': 'dq', 'refresh_type': 'directquery'}
    no_history = {'id': 'none'}

    inactive = app._filter_reports_by_refresh_status([fresh, stale, live, no_history], 'inactive')
    ids = {r['id'] for r in inactive}
    assert ids == {'stale', 'none'}, ids

    active = app._filter_reports_by_refresh_status([fresh, stale, live, no_history], 'active')
    ids = {r['id'] for r in active}
    assert ids == {'fresh', 'dq'}, ids


def test_attach_folder_ids():
    rows = [{'id': 'r1', 'name': 'A'}, {'id': 'r2', 'name': 'B'}]
    out = app._attach_folder_ids(rows, {'r1': 'f1'})
    assert out[0]['folderId'] == 'f1'
    assert out[0]['folderObjectId'] == 'f1'
    assert 'folderId' not in out[1]
    # Original rows are not mutated
    assert 'folderId' not in rows[0]
    # Empty map: passthrough
    assert app._attach_folder_ids(rows, {}) == rows


def test_filter_reports_by_folder_nested_and_root():
    folder_meta = {
        'folder_names_map': {
            'f-parent': {'id': 'f-parent', 'name': 'Parent', 'parentFolderId': None},
            'f-child': {'id': 'f-child', 'name': 'Child', 'parentFolderId': 'f-parent'},
        },
        'report_folder_map': {'r1': 'f-child', 'r2': 'f-parent', 'r3': None},
    }
    rows = [
        {'id': 'r1', 'name': 'In nested child'},
        {'id': 'r2', 'name': 'In parent'},
        {'id': 'r3', 'name': 'At root'},
    ]
    # Selecting the parent folder includes nested children
    out = app._filter_reports_by_folder(rows, 'f-parent', folder_meta)
    assert {r['id'] for r in out} == {'r1', 'r2'}
    # Root pseudo-folder returns only unmapped reports
    out = app._filter_reports_by_folder(rows, '__ROOT__', folder_meta)
    assert {r['id'] for r in out} == {'r3'}
    # No folder filter: passthrough
    assert app._filter_reports_by_folder(rows, None, folder_meta) == rows


if __name__ == "__main__":
    test_catalog_reports_shell_ops_rows()
    test_filter_reports_by_refresh_status()
    test_attach_folder_ids()
    test_filter_reports_by_folder_nested_and_root()
    print("All tests passed.")
