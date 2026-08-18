"""
agent/context.py — the single object threaded through every agent.

No agent talks to another directly; each returns the TurnContext to the Router.
This makes the whole turn auditable: plane, tokens-used, DAX executed, and
every fact's provenance live in one serializable place.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class Plane(str, Enum):
    """Which execution plane a turn is allowed to touch.

    SNAPSHOT = admin Scanner metadata in DuckDB. NO RLS. Metadata only.
    LIVE     = executeQueries with the user's OBO token. RLS enforced.
    WRITE    = XMLA / REST mutation. Requires OBO + explicit human approval.
    """
    SNAPSHOT = "snapshot"
    LIVE = "live"
    WRITE = "write"


class Intent(str, Enum):
    # --- snapshot plane (metadata; safe to answer from DuckDB) ---
    MEASURE_DEFINITION = "measure_definition"
    LINEAGE_SOURCES = "lineage_sources"
    IMPACT_ANALYSIS = "impact_analysis"
    REFRESH_STATUS = "refresh_status"
    USAGE_STATS = "usage_stats"
    MODEL_INVENTORY = "model_inventory"
    # --- live plane (fact values; MUST use OBO) ---
    FACT_QUERY = "fact_query"
    # --- write plane (mutations; approval-gated) ---
    CREATE_MEASURE = "create_measure"
    CREATE_REPORT = "create_report"
    # --- control ---
    CLARIFY = "clarify"


@dataclass
class Evidence:
    """One retrieved fact + where it came from. Drives citations."""
    claim: str
    source: str           # "duckdb:measures" | "executeQueries" | "xmla"
    detail: str = ""      # expression, dataset name, SQL/DAX, etc.
    build_id: Optional[str] = None      # snapshot provenance
    as_of_utc: Optional[str] = None     # freshness the user must see

    def cite(self) -> str:
        bits = [self.claim]
        if self.detail:
            bits.append(f"({self.detail})")
        if self.as_of_utc:
            bits.append(f"as of {self.as_of_utc}")
        return " ".join(bits)


class AuthRequired(Exception):
    """Raised when a live/write turn has no OBO token. Never downgrade."""


@dataclass
class TurnContext:
    user_upn: str
    question: str
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    obo_token: Optional[str] = None
    intent: Optional[Intent] = None
    plane: Optional[Plane] = None
    confidence: float = 0.0
    dataset_id: Optional[str] = None
    workspace_id: Optional[str] = None
    entities: Dict[str, Any] = field(default_factory=dict)
    evidence: List[Evidence] = field(default_factory=list)
    dax: Optional[str] = None
    result_rows: int = 0
    errors: List[str] = field(default_factory=list)
    requires_approval: bool = False
    approved_by: Optional[str] = None

    # ---- invariant enforcement -------------------------------------------
    def enforce_plane(self) -> None:
        """Fail CLOSED. A live/write turn without an OBO token must never
        silently fall back to the snapshot -- that is an RLS bypass."""
        if self.plane in (Plane.LIVE, Plane.WRITE) and not self.obo_token:
            raise AuthRequired(
                f"Intent '{self.intent.value if self.intent else '?'}' needs the "
                f"user's delegated token so RLS/OLS apply. Sign in to continue."
            )
        if self.plane is Plane.WRITE and not self.requires_approval:
            raise AssertionError("write-plane turns must set requires_approval")

    def add(self, claim: str, source: str, **kw: Any) -> "Evidence":
        ev = Evidence(claim=claim, source=source, **kw)
        self.evidence.append(ev)
        return ev

    def audit(self) -> Dict[str, Any]:
        """Structured log line. Never include the token."""
        d = asdict(self)
        d.pop("obo_token", None)
        d["has_obo_token"] = bool(self.obo_token)
        d["intent"] = self.intent.value if self.intent else None
        d["plane"] = self.plane.value if self.plane else None
        d["evidence"] = [e.claim for e in self.evidence]
        return d
