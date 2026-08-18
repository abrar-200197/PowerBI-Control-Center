"""
agent/router.py — deterministic intent -> plane routing.

The LLM may classify the INTENT. It may never pick the PLANE.
ROUTING_TABLE is the only thing that maps intent -> plane, so an LLM
hallucination can misroute a question between two metadata tools but can
NEVER move a fact query off the RLS-enforced live plane.

classify() below is a cheap deterministic pre-filter. In production, call the
LLM for classification and pass its label through `route()` -- the guarantees
live in route(), not in the classifier.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from .context import Intent, Plane, TurnContext

# ---------------------------------------------------------------------------
# The security-critical table. Reviewed as security code, not application code.
# ---------------------------------------------------------------------------
ROUTING_TABLE: dict[Intent, Plane] = {
    Intent.MEASURE_DEFINITION: Plane.SNAPSHOT,
    Intent.LINEAGE_SOURCES:    Plane.SNAPSHOT,
    Intent.IMPACT_ANALYSIS:    Plane.SNAPSHOT,
    Intent.REFRESH_STATUS:     Plane.SNAPSHOT,
    Intent.USAGE_STATS:        Plane.SNAPSHOT,
    Intent.MODEL_INVENTORY:    Plane.SNAPSHOT,
    Intent.FACT_QUERY:         Plane.LIVE,     # <-- never anything else
    Intent.CREATE_MEASURE:     Plane.WRITE,
    Intent.CREATE_REPORT:      Plane.WRITE,
    Intent.CLARIFY:            Plane.SNAPSHOT,
}

WRITE_INTENTS = {Intent.CREATE_MEASURE, Intent.CREATE_REPORT}

CONFIDENCE_FLOOR = 0.55  # below this we ask rather than guess

# Order matters: the first pattern that matches wins, so the more specific
# governance/write patterns are checked before the broad fact-value patterns.
_PATTERNS: list[tuple[Intent, str]] = [
    (Intent.CREATE_MEASURE, r"\b(create|add|author|write|define)\b.{0,25}\bmeasure\b"),
    (Intent.CREATE_REPORT,  r"\b(create|build|generate|make)\b.{0,25}\b(report|dashboard|page)\b"),
    (Intent.IMPACT_ANALYSIS, r"\b(impact|blast radius|downstream|upstream|affected|"
                             r"what breaks|if i (drop|delete|change|rename)|depends on)\b"),
    (Intent.MEASURE_DEFINITION, r"\b(how is|how are|what is the (definition|formula|logic)|"
                                r"defined|calculated|computed|dax for|formula for|logic behind)\b"),
    (Intent.LINEAGE_SOURCES, r"\b(source|sources|lineage|where does .{0,30}come from|"
                             r"which (table|database|server|gateway)|feeds?|datasource)\b"),
    (Intent.REFRESH_STATUS,  r"\b(refresh|failed|failure|last updated|last refreshed|stale)\b"),
    (Intent.USAGE_STATS,     r"\b(usage|used|views?|viewers?|popular|unused|adoption)\b"),
    (Intent.MODEL_INVENTORY, r"\b(how many|list|inventory|catalog|which (models?|datasets?|"
                             r"workspaces?|reports?)|show me all)\b"),
    # Broadest last: bare aggregation questions over business facts.
    (Intent.FACT_QUERY, r"\b(what (is|was|were)|how much|how many|total|sum|average|avg|"
                        r"top \d+|bottom \d+|trend|ytd|mtd|qtd|yoy|vs last|compare)\b"),
]


def classify(question: str) -> Tuple[Intent, float]:
    """Cheap deterministic classifier. Replace/augment with an LLM call --
    but always feed the result through route()."""
    q = (question or "").lower().strip()
    if not q:
        return Intent.CLARIFY, 0.0

    hits = [intent for intent, pat in _PATTERNS if re.search(pat, q)]
    if not hits:
        return Intent.CLARIFY, 0.0

    top = hits[0]
    # Ambiguity across planes is the dangerous case: "what is total sales and
    # how is it calculated" is both LIVE and SNAPSHOT. Lower confidence so the
    # orchestrator decomposes it instead of picking one.
    planes = {ROUTING_TABLE[h] for h in hits}
    conf = 0.9 if len(hits) == 1 else (0.75 if len(planes) == 1 else 0.5)
    return top, conf


def route(ctx: TurnContext, intent: Optional[Intent] = None,
          confidence: Optional[float] = None) -> TurnContext:
    """Assign plane from the table and enforce the invariants. Single choke point."""
    if intent is None:
        intent, auto_conf = classify(ctx.question)
        confidence = auto_conf if confidence is None else confidence
    ctx.intent = intent
    ctx.confidence = 1.0 if confidence is None else confidence

    if ctx.confidence < CONFIDENCE_FLOOR and intent is not Intent.CLARIFY:
        # Ask, don't guess. Guessing across planes is how RLS gets bypassed.
        ctx.intent = Intent.CLARIFY
        ctx.plane = ROUTING_TABLE[Intent.CLARIFY]
        ctx.errors.append(
            f"low confidence ({ctx.confidence:.2f}) for '{intent.value}'; "
            f"asking user to disambiguate"
        )
        return ctx

    ctx.plane = ROUTING_TABLE[intent]          # table decides, not the model
    ctx.requires_approval = intent in WRITE_INTENTS
    ctx.enforce_plane()                        # raises AuthRequired if no OBO
    return ctx
