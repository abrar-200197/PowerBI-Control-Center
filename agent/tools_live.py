"""
agent/tools_live.py — the Data Agent's live plane.

EVERY call here carries the END USER's OBO token, so Power BI enforces RLS/OLS
server-side. This module must never be called with an app-only token, and never
falls back to the snapshot -- see enforce_plane() in context.py.

Power BI REST executeQueries limits (hard, documented):
  * 1 query and 1 result table per request
  * max 100,000 rows and 1,000,000 values per query
  * 120 requests per minute per user
  * no pagination -- you must shape the DAX (TOPN/SUMMARIZE) to fit
  * DAX errors return HTTP 400 with the message in the body
  * unsupported for AAS-hosted models / on-prem AAS live connections

On Premium/Fabric capacity you also get executeDaxQueries (Arrow IPC) with no
fixed row cap -- set USE_ARROW=1 once you have pyarrow installed.

Set DEMO_MODE=1 (the default in the shipped demo) to use the offline stub so
you can see the whole flow without a tenant.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PBI_API = os.getenv("PBI_API_BASE", "https://api.powerbi.com/v1.0/myorg")
MAX_ROWS_HINT = 100_000
REQUESTS_PER_MIN = 120


class DaxError(Exception):
    """HTTP 400 from executeQueries -- the DAX itself is wrong. Repairable."""


class LiveUnavailable(Exception):
    """Transport/auth failure. NOT repairable by rewriting DAX."""


@dataclass
class QueryResult:
    rows: List[Dict[str, Any]]
    dax: str
    dataset_id: str
    elapsed_ms: int = 0
    truncated: bool = False
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DAX safety net. Not a security boundary (RLS is), but stops obvious footguns.
# ---------------------------------------------------------------------------
_FORBIDDEN = re.compile(
    r"\b(DEFINE\s+MEASURE|EVALUATEANDLOG)\b", re.I
)


def validate_dax(dax: str) -> str:
    d = (dax or "").strip()
    if not d:
        raise DaxError("empty DAX")
    if not re.match(r"^\s*(EVALUATE|DEFINE)\b", d, re.I):
        raise DaxError("DAX query must start with EVALUATE (or DEFINE ... EVALUATE)")
    if _FORBIDDEN.search(d):
        raise DaxError("DEFINE MEASURE / EVALUATEANDLOG are not allowed from the agent")
    if d.count(";") > 0:
        raise DaxError("multiple statements are not supported (1 query per request)")
    return d


def ensure_row_cap(dax: str, cap: int = 5000) -> str:
    """executeQueries has no pagination, so bound the result BEFORE sending.
    Wrapping in TOPN is cheaper than discovering the 100k limit at runtime."""
    if re.search(r"\bTOPN\s*\(", dax, re.I) or re.search(r"\bSAMPLE\s*\(", dax, re.I):
        return dax
    body = re.sub(r"^\s*EVALUATE\s+", "", dax, flags=re.I).strip()
    if body.upper().startswith("DEFINE"):
        return dax
    # ROW(...) already returns exactly one row; TOPN around it is pointless
    # and changes the shape for no benefit.
    if re.match(r"^ROW\s*\(", body, re.I):
        return dax
    return f"EVALUATE\nTOPN({cap},\n{body}\n)"


class RateLimiter:
    """120 req/min/user is per-user; keep one limiter per UPN."""

    def __init__(self, per_min: int = REQUESTS_PER_MIN):
        self.per_min = per_min
        self._hits: List[float] = []

    def check(self) -> None:
        now = time.time()
        self._hits = [t for t in self._hits if now - t < 60]
        if len(self._hits) >= self.per_min:
            wait = 60 - (now - self._hits[0])
            raise LiveUnavailable(
                f"Power BI allows {self.per_min} queries/min per user; "
                f"retry in {wait:.0f}s"
            )
        self._hits.append(now)


_limiters: Dict[str, RateLimiter] = {}


def _limiter(upn: str) -> RateLimiter:
    return _limiters.setdefault(upn, RateLimiter())


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def execute_dax(dataset_id: str, dax: str, obo_token: str, user_upn: str,
                group_id: Optional[str] = None,
                impersonate_upn: Optional[str] = None) -> QueryResult:
    """POST /datasets/{id}/executeQueries with the USER's token."""
    if not obo_token:
        raise LiveUnavailable(
            "no delegated token: refusing to query business data without RLS"
        )
    dax = ensure_row_cap(validate_dax(dax))
    _limiter(user_upn).check()

    if os.getenv("DEMO_MODE", "1") == "1":
        return _demo_execute(dataset_id, dax)

    import requests  # imported lazily so DEMO_MODE needs no network stack

    base = f"{PBI_API}/groups/{group_id}" if group_id else PBI_API
    url = f"{base}/datasets/{dataset_id}/executeQueries"
    payload: Dict[str, Any] = {
        "queries": [{"query": dax}],          # exactly ONE query per request
        "serializerSettings": {"includeNulls": True},
    }
    if impersonate_upn:
        # Only valid for service principals with dataset admin rights.
        payload["impersonatedUserName"] = impersonate_upn

    t0 = time.time()
    try:
        resp = requests.post(
            url, json=payload, timeout=int(os.getenv("PBI_TIMEOUT", "180")),
            headers={"Authorization": f"Bearer {obo_token}",
                     "Content-Type": "application/json"},
        )
    except Exception as exc:
        raise LiveUnavailable(f"executeQueries transport error: {exc}") from exc
    elapsed = int((time.time() - t0) * 1000)

    if resp.status_code == 400:
        # DAX is wrong -> feed the message back to the generator and retry.
        raise DaxError(_extract_error(resp))
    if resp.status_code in (401, 403):
        raise LiveUnavailable(
            f"not authorized for dataset {dataset_id} ({resp.status_code}). "
            f"The user needs Build permission on the semantic model."
        )
    if resp.status_code == 429:
        raise LiveUnavailable(
            f"throttled by Power BI; retry-after="
            f"{resp.headers.get('Retry-After', '?')}s"
        )
    if resp.status_code >= 400:
        raise LiveUnavailable(f"executeQueries HTTP {resp.status_code}: {resp.text[:400]}")

    body = resp.json()
    try:
        rows = body["results"][0]["tables"][0]["rows"]
    except (KeyError, IndexError):
        rows = []
    warnings = []
    if len(rows) >= MAX_ROWS_HINT:
        warnings.append(
            f"hit the {MAX_ROWS_HINT:,}-row executeQueries ceiling; "
            f"result is truncated -- aggregate further"
        )
    return QueryResult(rows=rows, dax=dax, dataset_id=dataset_id,
                       elapsed_ms=elapsed, truncated=bool(warnings),
                       warnings=warnings)


def _extract_error(resp) -> str:
    try:
        body = resp.json()
    except Exception:
        return resp.text[:400]
    err = body.get("error", {})
    detail = err.get("pbi.error", {}).get("details", [])
    if detail:
        return str(detail[0].get("detail", {}).get("value") or err.get("message"))
    return str(err.get("message") or body)[:400]


# ---------------------------------------------------------------------------
# Offline demo executor -- lets you watch the full loop with no tenant.
# ---------------------------------------------------------------------------
_DEMO_STORES = [
    {"DIM_STORE[StoreName]": "Bengaluru Central", "[Net Sales]": 8_112_400.10},
    {"DIM_STORE[StoreName]": "Hyderabad Gachibowli", "[Net Sales]": 7_004_233.75},
    {"DIM_STORE[StoreName]": "Chennai OMR", "[Net Sales]": 6_551_902.40},
    {"DIM_STORE[StoreName]": "Pune Kharadi", "[Net Sales]": 5_980_115.05},
    {"DIM_STORE[StoreName]": "Delhi Aerocity", "[Net Sales]": 5_233_887.20},
]
_DEMO_SCALARS = [
    ("gross margin", {"[Gross Margin %]": 0.3417}),
    ("margin", {"[Gross Margin %]": 0.3417}),
    ("cogs", {"[COGS]": 31_735_402.11}),
    ("on hand", {"[On Hand Qty]": 184_902}),
    ("total expense", {"[Total Expense]": 12_004_551.90}),
    ("net sales", {"[Net Sales]": 48_213_904.55}),
    ("sales", {"[Net Sales]": 48_213_904.55}),
]
_DEMO_WARN = "DEMO_MODE: synthetic result, not your tenant"


def _demo_execute(dataset_id: str, dax: str) -> QueryResult:
    low = dax.lower()
    # deliberately mimic the HTTP 400 repair loop for an unknown column
    if "[nonexistent" in low or "badcolumn" in low:
        raise DaxError(
            "The column 'BadColumn' could not be found or may not be used in "
            "this expression."
        )

    def done(rows):
        return QueryResult(rows=rows, dax=dax, dataset_id=dataset_id,
                           elapsed_ms=42, warnings=[_DEMO_WARN])

    # Grouped queries first: SUMMARIZECOLUMNS returns many rows, not a scalar.
    if "summarizecolumns" in low or "values(" in low:
        n = 3
        m = re.search(r"topn\s*\(\s*(\d+)", low)
        if m:
            n = min(int(m.group(1)), len(_DEMO_STORES))
        return done(_DEMO_STORES[:n])

    for key, row in _DEMO_SCALARS:
        if key in low:
            return done([row])
    return done([{"[Value]": 0}])
