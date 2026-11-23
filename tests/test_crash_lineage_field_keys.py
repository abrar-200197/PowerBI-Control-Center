"""Regression test: crash-test lineage field extraction accepts all binding
key variants (P1 crash-test fix).

The lineage analysis used to read only the field/column/table keys, silently
dropping fields bound under name/tableName/displayName — the same bindings the
schema validator (_validate_visual_bindings_against_schema) accepts. Affected
fields vanished from field_usage_map and affected_tables.

Run standalone:  python tests/test_crash_lineage_field_keys.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

from crash_test_analyzer import CrashTestAnalyzer  # noqa: E402


def _analyzer_with(bindings):
    a = CrashTestAnalyzer(
        workspace_id="ws", report_id="rpt", dataset_id="ds",
        access_token=None, client_id=None, client_secret=None, tenant_id=None,
    )
    a.visual_metadata = bindings
    return a


def test_lineage_accepts_name_and_tablename_keys():
    a = _analyzer_with([{
        "displayName": "Page 1",
        "visuals": [{
            "title": "Chart",
            "type": "columnChart",
            "fields": [
                {"tableName": "Sales", "name": "Revenue"},  # variant keys
                {"table": "Sales", "field": "Qty"},         # classic keys
            ],
        }],
    }])
    lineage = a._analyze_lineage_impact()
    usage = lineage["field_usage_map"]
    assert "Sales.Revenue" in usage, usage
    assert "Sales.Qty" in usage, usage
    assert "Sales" in lineage["affected_tables"], lineage["affected_tables"]


def test_lineage_accepts_displayname_variant():
    a = _analyzer_with([{
        "name": "Page 2",
        "visuals": [{
            "name": "Card",
            "type": "card",
            "fields": [{"displayName": "Region", "table": "Geo"}],
        }],
    }])
    lineage = a._analyze_lineage_impact()
    assert "Geo.Region" in lineage["field_usage_map"], lineage["field_usage_map"]


if __name__ == "__main__":
    test_lineage_accepts_name_and_tablename_keys()
    test_lineage_accepts_displayname_variant()
    print("All tests passed.")
