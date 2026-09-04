"""Unit tests for Crash Test schema field-binding validation."""
from crash_test_analyzer import CrashTestAnalyzer


def _analyzer():
    return CrashTestAnalyzer(
        workspace_id="ws",
        report_id="rpt",
        dataset_id="ds",
        access_token="token",
    )


def test_schema_validation_flags_missing_fields():
    a = _analyzer()
    a.dataset_schema = {
        "tables": ["Sales"],
        "columns": {
            "Sales": [
                {"name": "Amount"},
                {"name": "Region"},
            ]
        },
        "measures": {"Total Sales": {"name": "Total Sales", "table": "Sales"}},
    }
    a.visual_metadata = [
        {
            "displayName": "Overview",
            "visuals": [
                {
                    "title": "Broken Chart",
                    "type": "clusteredColumnChart",
                    "fields": [
                        {"name": "Amount", "table": "Sales", "type": "Column"},
                        {"name": "Deleted Col", "table": "Sales", "type": "Column"},
                        {"name": "Gone Measure", "table": "Sales", "type": "Measure"},
                    ],
                },
                {
                    "title": "Healthy Chart",
                    "type": "clusteredColumnChart",
                    "fields": [
                        {"name": "Amount", "table": "Sales", "type": "Column"},
                        {"name": "Total Sales", "table": "Sales", "type": "Measure"},
                    ],
                },
                {
                    "title": "Decoration",
                    "type": "shape",
                    "fields": [{"name": "X", "table": "Sales", "type": "Column"}],
                },
            ],
        }
    ]

    n = a._validate_visual_bindings_against_schema()
    assert n == 1
    broken = [i for i in a.issues if i.get("category") == "Broken Visual"]
    assert len(broken) == 1
    assert broken[0]["visual"] == "Broken Chart"
    missing_names = {f["field"] for f in broken[0]["missing_fields"]}
    assert "Deleted Col" in missing_names
    assert "Gone Measure" in missing_names
    assert "Amount" not in missing_names


def test_schema_validation_dedupes_existing_runtime_issues():
    a = _analyzer()
    a.dataset_schema = {
        "tables": ["T"],
        "columns": {"T": [{"name": "A"}]},
        "measures": {},
    }
    a.visual_metadata = [
        {
            "displayName": "P1",
            "visuals": [
                {
                    "title": "Already Flagged",
                    "type": "card",
                    "fields": [{"name": "Missing", "table": "T", "type": "Column"}],
                }
            ],
        }
    ]
    a.issues.append(
        {
            "category": "Broken Visual",
            "page": "P1",
            "visual": "Already Flagged",
            "visual_name": "Already Flagged",
        }
    )
    n = a._validate_visual_bindings_against_schema()
    assert n == 0
    assert len([i for i in a.issues if i.get("category") == "Broken Visual"]) == 1


def test_verify_field_missing_case_insensitive_and_global_search():
    a = _analyzer()
    a.dataset_schema = {
        "tables": ["Fact"],
        "columns": {"Fact": [{"name": "OrderID"}]},
        "measures": {"Revenue": {"name": "Revenue", "table": "Fact"}},
    }
    assert a._verify_field_missing("Fact", "orderid") is False
    assert a._verify_field_missing("", "OrderID") is False
    assert a._verify_field_missing("Fact", "Revenue") is False
    assert a._verify_field_missing("Fact", "NoSuch") is True


def test_set_visual_field_bindings_dict_shape():
    a = _analyzer()
    a.set_visual_field_bindings({
        "v1": {
            "page": "Overview",
            "title": "Chart 1",
            "type": "card",
            "fields": [{"name": "X", "table": "T", "type": "Column"}],
        }
    })
    assert len(a.visual_metadata) == 1
    assert a.visual_metadata[0]["displayName"] == "Overview"
    assert a.visual_metadata[0]["visuals"][0]["title"] == "Chart 1"


if __name__ == "__main__":
    test_schema_validation_flags_missing_fields()
    test_schema_validation_dedupes_existing_runtime_issues()
    test_verify_field_missing_case_insensitive_and_global_search()
    test_set_visual_field_bindings_dict_shape()
    print("OK")
