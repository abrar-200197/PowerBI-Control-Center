"""Unit tests for Crash Test render-scan merge (export + Playwright)."""
from visual_metadata_extractor import VisualMetadataExtractor


def test_merge_overlays_page_errors_and_keeps_export_fields():
    extractor = VisualMetadataExtractor()
    export_result = {
        "success": True,
        "pages": [
            {
                "name": "ReportSection1",
                "displayName": "Overview",
                "visuals": [
                    {
                        "name": "VisualContainer1",
                        "type": "clusteredBarChart",
                        "title": "SOP Compliance",
                        "fields": [{"name": "Status", "table": "Fact", "type": "Column"}],
                    }
                ],
            }
        ],
        "totalPages": 1,
        "totalVisuals": 1,
        "method": "report_definition_export",
    }
    playwright_result = {
        "success": True,
        "pages": [
            {
                "name": "ReportSection1",
                "displayName": "Overview",
                "hasErrors": True,
                "errors": [
                    {
                        "visualTitle": "SOP Compliance",
                        "message": "Missing_References",
                        "detailedMessage": "Could not render a report visual titled: SOP Compliance",
                    }
                ],
                "visuals": [
                    {"name": "VisualContainer1", "title": "SOP Compliance", "type": "clusteredBarChart"}
                ],
            }
        ],
    }

    merged = extractor._merge_render_scan(export_result, playwright_result)
    page = merged["pages"][0]
    assert page["hasErrors"] is True
    assert page["errors"][0]["message"] == "Missing_References"
    assert page["visuals"][0]["fields"][0]["name"] == "Status"
    assert merged["method"] == "report_definition_export+playwright_render_scan"
    assert merged["render_scan_performed"] is True


def test_merge_keeps_unmatched_export_pages():
    extractor = VisualMetadataExtractor()
    export_result = {
        "success": True,
        "pages": [
            {"name": "PageA", "displayName": "A", "visuals": [{"title": "Chart A", "fields": []}]},
            {"name": "PageB", "displayName": "B", "visuals": [{"title": "Chart B", "fields": []}]},
        ],
    }
    playwright_result = {
        "success": True,
        "pages": [
            {
                "name": "PageA",
                "displayName": "A",
                "hasErrors": True,
                "errors": [{"visualTitle": "Chart A", "message": "See details"}],
                "visuals": [{"title": "Chart A"}],
            }
        ],
    }
    merged = extractor._merge_render_scan(export_result, playwright_result)
    assert len(merged["pages"]) == 2
    names = {p["name"] for p in merged["pages"]}
    assert names == {"PageA", "PageB"}


def test_extract_visuals_default_does_not_require_playwright_flag():
    import inspect
    sig = inspect.signature(VisualMetadataExtractor.extract_visuals)
    assert sig.parameters["detect_render_errors"].default is False


if __name__ == "__main__":
    test_merge_overlays_page_errors_and_keeps_export_fields()
    test_merge_keeps_unmatched_export_pages()
    test_extract_visuals_default_does_not_require_playwright_flag()
    print("OK")
