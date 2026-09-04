"""Tests for PBIR-ish field walking and deep field extraction."""
from visual_metadata_extractor import VisualMetadataExtractor


def test_walk_field_refs_finds_column_and_queryref():
    ex = VisualMetadataExtractor()
    node = {
        "query": {
            "queryState": {
                "Values": {
                    "projections": [
                        {
                            "field": {
                                "Column": {
                                    "Expression": {"SourceRef": {"Entity": "Sales"}},
                                    "Property": "Amount",
                                }
                            }
                        }
                    ]
                }
            }
        },
        "other": {"queryRef": "DimProduct.ProductName"},
    }
    found = ex._walk_field_refs(node)
    names = {(f.get("table"), f.get("name")) for f in found}
    assert ("Sales", "Amount") in names
    assert ("DimProduct", "ProductName") in names


def test_extract_fields_from_definition_deep_walk_fallback():
    ex = VisualMetadataExtractor()
    layout = {
        "sections": [
            {
                "displayName": "Page1",
                "visualContainers": [
                    {
                        "name": "v1",
                        "config": {
                            "singleVisual": {
                                "visualType": "card",
                                "vcObjects": {
                                    "title": [
                                        {
                                            "properties": {
                                                "text": {
                                                    "expr": {"Literal": {"Value": "'Revenue Card'"}}
                                                }
                                            }
                                        }
                                    ]
                                },
                                # No queryState — only nested queryRef (PBIR-like)
                                "payload": {"queryRef": "Fact.Revenue"},
                            }
                        },
                    }
                ],
            }
        ]
    }
    # config as dict is ok — extractor handles non-string config
    vf = ex.extract_fields_from_definition(layout)
    assert "v1" in vf
    assert vf["v1"]["title"] == "Revenue Card"
    fields = vf["v1"]["fields"]
    assert any(f.get("name") == "Revenue" for f in fields)


def test_canvas_error_patterns_include_common_banners():
    patterns = VisualMetadataExtractor.CANVAS_ERROR_PATTERNS
    assert "See details" in patterns
    assert any("wrong with one or more fields" in p for p in patterns)


if __name__ == "__main__":
    test_walk_field_refs_finds_column_and_queryref()
    test_extract_fields_from_definition_deep_walk_fallback()
    test_canvas_error_patterns_include_common_banners()
    print("OK")
