"""Self-check for smart timer mode (no Azure host required)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MOD_PATH = ROOT / "CatalogExtractTimer" / "__init__.py"


def _load():
    import types
    # Stub azure.functions so decide_mode can load without the Functions runtime
    if "azure.functions" not in sys.modules:
        stub = types.ModuleType("azure.functions")
        stub.TimerRequest = object  # type: ignore
        sys.modules["azure"] = types.ModuleType("azure")
        sys.modules["azure.functions"] = stub
    spec = importlib.util.spec_from_file_location("catalog_extract_timer", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mod = _load()
    cases = [
        # (utc datetime, expected mode)
        (datetime(2026, 8, 2, 6, 30, tzinfo=timezone.utc), "fresh"),   # Sunday
        (datetime(2026, 8, 2, 6, 35, tzinfo=timezone.utc), "fresh"),   # Sunday skew
        (datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc), "ops-only"),  # Sunday noon
        (datetime(2026, 8, 2, 18, 30, tzinfo=timezone.utc), "ops-only"),
        (datetime(2026, 8, 2, 0, 30, tzinfo=timezone.utc), "ops-only"),
        (datetime(2026, 8, 3, 6, 30, tzinfo=timezone.utc), "ops-only"),  # Monday
        (datetime(2026, 8, 9, 6, 30, tzinfo=timezone.utc), "fresh"),   # next Sunday
    ]
    fails = 0
    for when, expected in cases:
        mode, argv = mod.decide_mode(when)
        ok = mode == expected
        flag = "OK" if ok else "FAIL"
        print(f"{flag} {when.isoformat()} → {mode} {argv} (want {expected})")
        if not ok:
            fails += 1
        if expected == "fresh" and "--fresh" not in argv:
            print("  FAIL missing --fresh flag")
            fails += 1
        if expected == "ops-only" and "--ops-only" not in argv:
            print("  FAIL missing --ops-only flag")
            fails += 1
    print(f"TOTAL_FAIL={fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
