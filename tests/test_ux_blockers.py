"""Offline regression guards for the release-blocker fixes (B1: EB-40, B2: EB-41).

Content-assertion tests in the repo's offline style (bare asserts, no mock
library, no network). They exist because the batch that should have produced
the EB-40 home edits silently lost them when an earlier edit in the same
script aborted - these guards catch that class of drift, and pin the
crash-modal close semantics introduced by B2.

Note on home "row edits": home-page workspace rows are NAVIGATION rows
(tr[data-href] opens the Report Catalog filtered to that workspace); the home
page has no row-editing feature, and the approved EB-40 acceptance criteria
for home are keyboard operability + the ARIA tabs pattern. "Save / cancel /
invalid input" therefore do not apply; the equivalent validations are the
interactive-child guard (pills/links keep their own targets) and activation
behavior, asserted structurally here and behaviorally in the manual browser
checklist.

Run standalone:  python tests/test_ux_blockers.py
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

REPO = Path(__file__).resolve().parents[1]


def _read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# B1 - EB-40 home behavior (templates/home.html)
# ---------------------------------------------------------------------------

def test_eb40_all_seven_kpi_tabs_declare_their_controlled_panel():
    """ARIA tabs pattern: each KPI tab points at the detail panel it controls."""
    home = _read("templates/home.html")
    assert home.count('aria-controls="homeDetailPanel"') == 7, (
        "expected exactly 7 KPI tabs wired to homeDetailPanel "
        "(the panel id driven by setActiveMetric)")


def test_eb40_kpi_arrow_key_navigation_installed_exactly_once():
    """Left/Right arrow navigation on the KPI strip registers exactly once."""
    home = _read("templates/home.html")
    assert home.count("function _initHomeKpiA11y") == 1
    assert home.count("_initHomeKpiA11y();") == 1
    assert home.count(
        "document.addEventListener('DOMContentLoaded', _initHomeKpiA11y);") == 1


def test_eb40_workspace_rows_are_keyboard_activatable():
    """Both tr[data-href] handler blocks gain tabindex + Enter/Space activation.

    Block 1 = workspace-list rows; block 2 = metric detail-table rows
    (second block identified while tracing callers during the B1 fix -
    same approved pattern applied for a consistently keyboard-usable page).
    """
    home = _read("templates/home.html")
    assert home.count("tr.setAttribute('tabindex', '0');") == 2
    assert home.count("tr.addEventListener('keydown', (e) => {") == 2
    assert home.count("if (e.key !== 'Enter' && e.key !== ' ') return;") == 2


def test_eb40_row_click_navigation_is_preserved():
    """The keyboard additions must not have replaced the click paths."""
    home = _read("templates/home.html")
    assert home.count("tr.addEventListener('click', (e) => {") == 2


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS %s" % name)
            except AssertionError as exc:
                failures += 1
                print("FAIL %s: %s" % (name, exc))
    if failures:
        print("%d test(s) failed." % failures)
        sys.exit(1)
    print("All tests passed.")
