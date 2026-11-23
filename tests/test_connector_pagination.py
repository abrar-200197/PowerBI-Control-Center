"""Regression test: PowerBIConnector list endpoints follow OData pagination.

The tenant has ~550 workspaces; Power BI caps list responses at ~100 per
page. get_workspaces/get_all_reports used to fetch a single page and
silently truncated. _paged_get now follows @odata.nextLink.

Run standalone:  python tests/test_connector_pagination.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import powerbi_connector as pc  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _run_with_pages(pages, method, *args):
    """Monkeypatch requests.get with a scripted page sequence (offline)."""
    calls = []
    queue = list(pages)

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _FakeResp(queue.pop(0) if queue else {"value": []})

    orig = pc.requests.get
    pc.requests.get = fake_get
    try:
        pb = pc.PowerBIConnector()
        result = getattr(pb, method)(*args)
    finally:
        pc.requests.get = orig
    return result, calls


def test_workspaces_follow_nextlink():
    page1 = {"value": [{"id": str(i), "name": f"WS{i}"} for i in range(100)],
             "@odata.nextLink": "https://api.powerbi.com/v1.0/myorg/groups?$skiptoken=100"}
    page2 = {"value": [{"id": str(i), "name": f"WS{i}"} for i in range(100, 150)]}
    ws, calls = _run_with_pages([page1, page2], "get_workspaces")
    assert len(ws) == 150, len(ws)
    assert len(calls) == 2, calls
    assert calls[1] == page1["@odata.nextLink"], calls


def test_reports_single_page_no_nextlink():
    page1 = {"value": [{"id": "r1", "name": "R1"}]}
    reports, calls = _run_with_pages([page1], "get_all_reports", "ws-1")
    assert len(reports) == 1, reports
    assert len(calls) == 1, calls


def test_max_pages_guard():
    # Endless nextLink chain: the PBI_MAX_PAGES guard stops the loop and the
    # call still returns everything collected so far (bounded, no hang).
    def fake_get(url, headers=None, timeout=None):
        return _FakeResp({"value": [{"id": "x", "name": "WS-x"}],
                          "@odata.nextLink": url + "&next=1"})

    orig = pc.requests.get
    pc.requests.get = fake_get
    try:
        import os
        os.environ["PBI_MAX_PAGES"] = "5"
        pb = pc.PowerBIConnector()
        ws = pb.get_workspaces()
    finally:
        pc.requests.get = orig
        os.environ.pop("PBI_MAX_PAGES", None)
    assert len(ws) == 5, len(ws)


if __name__ == "__main__":
    test_workspaces_follow_nextlink()
    test_reports_single_page_no_nextlink()
    test_max_pages_guard()
    print("All tests passed.")
