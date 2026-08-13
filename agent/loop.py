"""
The agent loop. This is the part that was missing.

    while not done and steps < budget:
        reply = llm.chat(history, tools=schemas)
        if reply.wants_tools:
            for call in reply.tool_calls:
                history += run(call)      # errors go back in as text
            continue
        return reply.content

That is the whole idea, and it is the difference between an agent and a
decision tree. The old orchestrator classified once with a regex, dispatched to
one handler, and returned. It could not:

  - chain          find the model, THEN read its schema, THEN query it
  - recover        see a DAX error and rewrite the query
  - decompose      answer "what is net sales and how is it calculated?" by
                   using both the live plane and the snapshot in one turn
  - decide to stop  it always did exactly one thing

The loop does all four, because the model sees each result and chooses again.

WHAT THE LOOP KEEPS FROM THE OLD DESIGN
    The security invariant. Tool calls go through tool_registry.invoke(), which
    enforces the plane rules before the tool runs. The LLM picks tools; it
    cannot pick permissions. A prompt-injected or hallucinating model gets
    refused exactly like a well-behaved one -- and the refusal text is fed back
    as a tool result so it can adapt instead of looping.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from . import tool_registry as R
from .llm import LLMReply, LLMUnavailable, ScriptedLLM

MAX_STEPS = 8          # tool-calling rounds before we force an answer
MAX_TOOL_CALLS = 16    # total tools per turn, across rounds

SYSTEM_PROMPT = """\
You are the Power BI Control Center agent. You answer questions about an \
organisation's Power BI estate and about the data inside its semantic models, \
and you can generate reports.

You work by calling tools. Think about what you need, call tools to get it, \
look at the results, and call more tools if needed. Do not guess values, model \
names, field names, or DAX -- look them up.

TWO KINDS OF QUESTION, AND THE DIFFERENCE MATTERS:
- METADATA questions ("how is Net Sales calculated?", "what sources feed this \
model?", "which refreshes failed?") are answered from the governance snapshot \
using the metadata tools. These return DEFINITIONS and STRUCTURE.
- DATA questions ("what is net sales?", "top 5 stores by revenue") need actual \
numbers. You MUST get those with run_dax. The metadata tools never return \
values. Never infer, estimate, or state a number that did not come from \
run_dax.

WORKING WITH MODELS:
- If you do not have a dataset_id, call find_dataset first.
- Before writing DAX, call model_schema so you use real table, column and \
measure names. Use relationships if your query spans tables.
- If a DAX query fails, read the error, fix the query, and try again.

WHEN A TOOL REFUSES:
A tool result beginning with REFUSED is a policy decision, not a bug. Do not \
retry it and do not work around it with a different tool. Explain the \
situation to the user plainly.

ANSWERING:
Be concise and concrete. Give the actual numbers you retrieved. Say which \
model an answer came from. If the snapshot supplied the answer, mention how \
fresh it is. If you could not answer something, say so rather than \
speculating."""


@dataclass
class AgentContext:
    """Everything a tool might need, plus what the turn accumulated.

    Carries the credentials rather than letting tools reach for globals, so a
    test can construct a tokenless context and prove the guardrail holds.
    """
    conn: Any
    user_upn: str = "unknown@local"
    user_token: Optional[str] = None
    allow_write: bool = True
    dataset_id: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    planes_used: Set[str] = field(default_factory=set)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentAnswer:
    answer: str
    tool_calls: List[Dict[str, Any]]
    planes_used: List[str]
    artifacts: List[Dict[str, Any]]
    steps: int
    stopped_because: str
    transcript: List[Dict[str, Any]]

    @property
    def plane(self) -> str:
        """The strongest plane touched -- what the UI badges the answer with."""
        for p in (R.WRITE, R.LIVE, R.SNAPSHOT):
            if p in self.planes_used:
                return p
        return R.SNAPSHOT


def run_agent(llm, ctx: AgentContext, question: str,
              max_steps: int = MAX_STEPS,
              system: str = SYSTEM_PROMPT) -> AgentAnswer:
    """Run the loop until the model answers, or we hit the budget."""
    if not (question or "").strip():
        raise ValueError("question is empty")

    user_msg = question
    if ctx.dataset_id:
        # The user already picked a model in the UI. Telling the model this
        # saves a find_dataset round-trip and stops it choosing a different
        # model than the one on screen.
        user_msg = (f"[The user is currently working with dataset_id="
                    f"{ctx.dataset_id}. Use it unless they name another "
                    f"model.]\n\n{question}")

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    schemas = R.schemas(allow_write=ctx.allow_write,
                        allow_live=bool(ctx.user_token))

    stopped, steps = "answered", 0

    for step in range(max_steps):
        steps = step + 1
        try:
            reply: LLMReply = llm.chat(messages, tools=schemas)
        except LLMUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

        if not reply.wants_tools:
            return AgentAnswer(
                answer=(reply.content or "").strip() or
                       "I could not produce an answer for that.",
                tool_calls=ctx.tool_calls, planes_used=sorted(ctx.planes_used),
                artifacts=ctx.artifacts, steps=steps,
                stopped_because=stopped, transcript=messages)

        messages.append({
            "role": "assistant",
            "content": reply.content,
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name,
                              "arguments": json.dumps(c.arguments)}}
                for c in reply.tool_calls],
        })

        for call in reply.tool_calls:
            if len(ctx.tool_calls) >= MAX_TOOL_CALLS:
                messages.append(_tool_msg(
                    call.id, "REFUSED: tool-call budget for this turn is "
                             "exhausted. Answer with what you already have."))
                stopped = "tool_budget"
                continue
            messages.append(_tool_msg(call.id, _run_one(call, ctx)))

    # Budget exhausted while still calling tools. Ask for a final answer with
    # tools switched off, so the user gets the partial findings rather than
    # nothing at all.
    messages.append({
        "role": "user",
        "content": "Stop calling tools now and answer using only what you have "
                   "gathered. If it is incomplete, say what is missing."})
    try:
        final = llm.chat(messages, tools=None)
        answer = (final.content or "").strip()
    except Exception:  # noqa: BLE001
        answer = ""

    return AgentAnswer(
        answer=answer or "I ran out of steps before I could answer that.",
        tool_calls=ctx.tool_calls, planes_used=sorted(ctx.planes_used),
        artifacts=ctx.artifacts, steps=steps,
        stopped_because="step_budget", transcript=messages)


def _run_one(call, ctx: AgentContext) -> str:
    """Invoke one tool. Every failure becomes text the model can act on.

    Nothing raised by a tool is allowed to kill the turn: a model that sees
    'ERROR: column X does not exist' will usually fix its query on the next
    step, which is precisely the self-correction the old code could not do.
    """
    try:
        return R.result_to_text(R.invoke(call.name, call.arguments, ctx))
    except R.PlaneViolation as exc:
        return str(exc)                       # already phrased for the model
    except Exception as exc:                  # noqa: BLE001
        return f"ERROR from {call.name}: {type(exc).__name__}: {exc}"


def _tool_msg(call_id: str, content: str) -> Dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}
