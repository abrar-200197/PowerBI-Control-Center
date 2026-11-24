"""Offline test: the 413 error handler returns the route's JSON error shape.

routes/report_diff.py performs the friendly per-file 413 with a JSON body, but
the process-wide MAX_CONTENT_LENGTH backstop (app.py) used to fall through to
Werkzeug's generic HTML 413 - which the upload page cannot parse (it calls
resp.json()), surfacing as a cryptic client-side parse error instead of a size
explanation. The UX enhancement added an app-level 413 handler that returns
{'success': False, 'error': <actionable message>} while keeping the per-file
checks as the primary path.

Run standalone:  python tests/test_ux_413.py
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


def test_413_handler_returns_json_shape():
    """The handler must return (Response, 413) with the JSON error contract."""
    with app.app.app_context():
        resp, status = app._upload_too_large(None)
    assert status == 413, status
    body = resp.get_json()
    assert body is not None, "413 must return JSON, not an HTML page"
    assert body.get("success") is False
    error = body.get("error", "")
    assert "1700 MB" in error, error
    assert "3500 MB" in error, error


def test_413_handler_is_registered():
    """The handler must be registered app-wide for HTTP 413."""
    found = False
    for _blueprint, handlers in app.app.error_handler_spec.items():
        if not handlers:
            continue
        for code, mapping in handlers.items():
            if code == 413 and mapping:
                found = True
    assert found, "no 413 error handler registered on the Flask app"


def test_max_content_length_backstop_unchanged():
    """The enhancement must not weaken the process-wide upload cap."""
    assert app.app.config.get("MAX_CONTENT_LENGTH") == 3500 * 1024 * 1024


if __name__ == "__main__":
    test_413_handler_returns_json_shape()
    test_413_handler_is_registered()
    test_max_content_length_backstop_unchanged()
    print("All tests passed.")
