"""
The tool registry: what the agent is allowed to do, and under what conditions.

This is the file that makes an LLM-driven loop safe to run.

THE KEY IDEA
    In the old orchestrator, ROUTING_TABLE was the *brain*: a regex picked an
    intent, the table picked a handler, and that was the whole decision. That
    is a decision tree, not an agent.

    Here the LLM is the brain -- it decides which tools to call, in what order,
    and when it has enough to answer. The routing table is *demoted to a
    guardrail*: every tool declares the plane it runs on, and the plane's rules
    are enforced HERE, at invoke time, before the tool function is reached.

    So the LLM chooses freely, and still cannot:
      - read business facts without the user's delegated token (RLS bypass)
      - write anything without an explicit approval token
      - answer a fact question from the no-RLS governance snapshot

    A hallucinating, confused, or prompt-injected model hits the same wall as a
    well-behaved one. That is the property worth having: safety that does not
    depend on the model behaving.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import tools_governance as G
from . import tools_live as L

SNAPSHOT, LIVE, WRITE = "snapshot", "live", "write"


class PlaneViolation(Exception):
    """Raised when a tool call is refused by the guardrail, not by the tool."""


@dataclass
class Tool:
    name: str
    plane: str
    description: str
    params: Dict[str, Any]
    fn: Callable
    required: List[str] = field(default_factory=list)

    def schema(self) -> Dict[str, Any]:
        """OpenAI / Azure OpenAI tool-calling JSON schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.params,
                    "required": self.required,
                },
            },
        }


_S = {"type": "string"}


def _d(desc: str) -> Dict[str, str]:
    return {"type": "string", "description": desc}


# ---------------------------------------------------------------------------
# Tool catalogue. Descriptions are written FOR THE MODEL -- they are prompt
# surface, not documentation. Vague descriptions are the single biggest cause
# of a tool-calling agent picking the wrong tool.
# ---------------------------------------------------------------------------
TOOLS: Dict[str, Tool] = {}


def register(t: Tool) -> Tool:
    TOOLS[t.name] = t
    return t


register(Tool(
    "find_dataset", SNAPSHOT,
    "Find semantic models (datasets) by name. Use this FIRST when the user "
    "mentions a model by name and you need its dataset_id. Returns dataset_id, "
    "name, workspace.",
    {"name": _d("Full or partial model name, e.g. 'Retail Sales'")},
    lambda ctx, name: G.find_dataset(ctx.conn, name), ["name"]))

register(Tool(
    "model_schema", SNAPSHOT,
    "List the tables, columns and measures of one semantic model. Use this to "
    "discover what fields exist BEFORE writing DAX or building a report, so "
    "you reference real field names instead of guessing.",
    {"dataset_id": _d("The dataset_id from find_dataset")},
    lambda ctx, dataset_id: G.model_schema(ctx.conn, dataset_id), ["dataset_id"]))

register(Tool(
    "find_measure", SNAPSHOT,
    "Look up a measure's DAX definition by name. Use for 'how is X "
    "calculated?' questions. Returns the expression, its table and model. This "
    "returns the FORMULA, never a value -- for a value you must run a query.",
    {"name": _d("Measure name, e.g. 'Net Sales'"),
     "dataset_id": _d("Optional: restrict to one model")},
    lambda ctx, name, dataset_id=None: G.find_measure(ctx.conn, name, dataset_id),
    ["name"]))

register(Tool(
    "model_sources", SNAPSHOT,
    "List the upstream datasources (SQL servers, databases, files, gateways) "
    "feeding a semantic model. Use for lineage / 'where does this data come "
    "from' / 'what sources are used' questions.",
    {"dataset_id": _d("The dataset_id")},
    lambda ctx, dataset_id: G.model_sources(ctx.conn, dataset_id), ["dataset_id"]))

register(Tool(
    "impact_of", SNAPSHOT,
    "Blast-radius analysis: given a table or column, list every downstream "
    "model and report that would break if it changed. Use for 'what breaks "
    "if I drop X' / 'what depends on X'.",
    {"table_key": _d("Table or column name, e.g. 'FACT_SALES'")},
    lambda ctx, table_key: G.impact_of(ctx.conn, table_key), ["table_key"]))

register(Tool(
    "refresh_status", SNAPSHOT,
    "Dataset refresh history and failures. Use for 'which refreshes failed', "
    "'is X stale', 'when was X last updated'.",
    {"only_failed": {"type": "boolean",
                     "description": "True to return only failures"}},
    lambda ctx, only_failed=False: G.refresh_status(ctx.conn, only_failed)))

register(Tool(
    "usage_stats", SNAPSHOT,
    "Report view counts and distinct viewers. Use for 'most/least used "
    "reports', 'adoption', 'what can we decommission'.",
    {"workspace_id": _d("Optional: restrict to one workspace")},
    lambda ctx, workspace_id=None: G.usage_stats(ctx.conn, workspace_id)))

register(Tool(
    "relationships", SNAPSHOT,
    "The relationships (joins) between tables in a model, with cardinality "
    "and filter direction. Use before writing DAX that spans tables.",
    {"dataset_id": _d("The dataset_id")},
    lambda ctx, dataset_id: G.relationships(ctx.conn, dataset_id), ["dataset_id"]))

register(Tool(
    "run_dax", LIVE,
    "Execute a DAX query against a semantic model and return actual DATA "
    "VALUES. This is the ONLY way to answer 'what is <number>' questions -- "
    "the metadata tools return definitions, never values. Always call "
    "model_schema first so you reference real measures and columns. Runs as "
    "the signed-in user, so row-level security applies.",
    {"dataset_id": _d("The dataset_id to query"),
     "dax": _d("A complete DAX query starting with EVALUATE")},
    lambda ctx, dataset_id, dax: L.execute_dax(
        dataset_id, dax, ctx.user_token, ctx.user_upn),
    ["dataset_id", "dax"]))

register(Tool(
    "build_report", WRITE,
    "Generate a Power BI report (.pbip) the user can download and open in "
    "Power BI Desktop. It live-connects to the model and contains no data. "
    "Use when the user asks to create/build/make a report or dashboard.",
    {"dataset_id": _d("The model the report connects to"),
     "question": _d("What the report should show, in plain words"),
     "report_name": _d("Optional report title")},
    lambda ctx, dataset_id, question, report_name=None:
        _build_report(ctx, dataset_id, question, report_name),
    ["dataset_id", "question"]))


def _build_report(ctx, dataset_id, question, report_name):
    from . import report_builder
    blob, meta = report_builder.generate(ctx.conn, dataset_id, question,
                                         report_name)
    ctx.artifacts.append({"kind": "pbip", "bytes": blob, "meta": meta})
    return {
        "status": "report generated and ready for download",
        "file_name": meta["file_name"],
        "visual_count": meta["visual_count"],
        "fields_used": meta["fields_used"],
    }


# ---------------------------------------------------------------------------
# The guardrail
# ---------------------------------------------------------------------------
def invoke(name: str, args: Dict[str, Any], ctx) -> Any:
    """Run a tool the LLM asked for, enforcing plane rules first.

    Order matters: every check here happens BEFORE the tool function runs.
    """
    tool = TOOLS.get(name)
    if tool is None:
        # Hallucinated tool name. Tell the model plainly so it can recover on
        # the next turn rather than repeating itself.
        raise PlaneViolation(
            f"no such tool '{name}'. Available: {', '.join(sorted(TOOLS))}")

    if tool.plane == LIVE and not ctx.user_token:
        raise PlaneViolation(
            "REFUSED: reading business data requires the signed-in user's "
            "delegated token so row-level security applies. There is no token "
            "on this request. Do NOT substitute metadata from the governance "
            "snapshot -- it has no RLS. Tell the user to sign in.")

    if tool.plane == WRITE and not ctx.allow_write:
        raise PlaneViolation(
            f"REFUSED: '{name}' creates something and needs explicit user "
            "approval, which has not been given. Describe what you would "
            "create and ask the user to confirm.")

    unknown = set(args) - set(tool.params)
    if unknown:
        raise PlaneViolation(
            f"unknown argument(s) {sorted(unknown)} for '{name}'. "
            f"Valid: {sorted(tool.params)}")

    missing = [r for r in tool.required if r not in args]
    if missing:
        raise PlaneViolation(f"missing required argument(s) {missing} for '{name}'")

    ctx.tool_calls.append({"tool": name, "args": args, "plane": tool.plane})
    result = tool.fn(ctx, **args)
    ctx.planes_used.add(tool.plane)
    return result


def schemas(allow_write: bool = True, allow_live: bool = True
            ) -> List[Dict[str, Any]]:
    """Tool schemas to advertise to the model.

    We hide tools the caller cannot use rather than letting the model call them
    and fail. Fewer wasted turns, and the model stops promising things it
    cannot deliver. The invoke() guardrail still enforces this independently --
    hiding is a UX optimisation, never the security boundary.
    """
    out = []
    for t in TOOLS.values():
        if t.plane == WRITE and not allow_write:
            continue
        if t.plane == LIVE and not allow_live:
            continue
        out.append(t.schema())
    return out


def result_to_text(result: Any, limit: int = 4000) -> str:
    """Serialise a tool result for the model's context window.

    Truncation is explicit and announced. A silently truncated result makes a
    model confidently report a partial answer as complete.
    """
    if isinstance(result, G.ToolResult):
        payload = {"tool": result.tool, "row_count": len(result.rows),
                   "as_of_utc": result.as_of_utc, "rows": result.rows}
    elif isinstance(result, L.QueryResult):
        payload = {"row_count": len(result.rows), "dax": result.dax,
                   "rows": result.rows, "warnings": result.warnings}
    else:
        payload = result

    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > limit:
        text = text[:limit] + f'... [TRUNCATED at {limit} chars — ' \
                              f'narrow your query if you need the rest]'
    return text
