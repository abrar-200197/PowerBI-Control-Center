"""
agent/narrator.py — turns a TurnContext into a user-facing answer.

Three rules, all of them non-negotiable:
  1. Every snapshot answer carries a freshness stamp. Metadata is up to 6h old
     (24h for schema, since the full Scanner rebuild is weekly) and the user
     must be able to see that.
  2. Every claim keeps its provenance. If we cannot cite it, we do not assert it.
  3. Write-plane answers show the plan and say plainly that nothing was applied.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .context import Intent, Plane, TurnContext

PLANE_LABEL = {
    Plane.SNAPSHOT: "governance snapshot (metadata; no row-level security applied)",
    Plane.LIVE: "live query (your permissions and RLS applied)",
    Plane.WRITE: "proposed change (nothing applied)",
}


def _fmt_value(v: Any) -> str:
    if isinstance(v, float):
        if abs(v) < 1:
            return f"{v:.2%}"
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _rows_table(rows: List[Dict[str, Any]], limit: int = 15) -> str:
    if not rows:
        return ""
    cols = list(rows[0].keys())[:6]
    head = " | ".join(cols)
    sep = " | ".join("---" for _ in cols)
    body = [
        " | ".join(_fmt_value(r.get(c)) for c in cols)
        for r in rows[:limit]
    ]
    more = f"\n_({len(rows) - limit} more rows)_" if len(rows) > limit else ""
    return f"\n{head}\n{sep}\n" + "\n".join(body) + more


def narrate(ctx: TurnContext) -> Dict[str, Any]:
    rows = ctx.entities.get("rows") or []
    lines: List[str] = []

    if ctx.intent is Intent.CLARIFY:
        lines.append(
            "I need one clarification before I answer, because this could be "
            "either a **data** question (which I run live against the model, "
            "with your permissions) or a **governance** question (which I "
            "answer from the metadata snapshot)."
        )
        lines.append("\nWhich did you mean? You can also name the semantic model.")

    elif ctx.plane is Plane.LIVE:
        auth_err = next((e for e in ctx.errors if "delegated token" in e
                         or "not authorized" in e), None)
        if auth_err and not rows:
            lines.append(
                "**Sign-in required.** This is a data question, so I have to run "
                "it live against the semantic model using *your* credentials — "
                "that is what makes row-level security apply to the result.\n\n"
                "I will not answer it from the governance snapshot: that snapshot "
                "is admin-collected metadata with no RLS, so using it here would "
                "silently bypass your organisation's data access rules."
            )
        elif rows:
            if len(rows) == 1 and len(rows[0]) == 1:
                k, v = next(iter(rows[0].items()))
                lines.append(f"**{_fmt_value(v)}**  ({k.strip('[]')})")
            else:
                lines.append(_rows_table(rows))
        else:
            lines.append("The query returned no rows.")

    elif ctx.plane is Plane.WRITE:
        plan = ctx.entities.get("plan") or {}

        if plan.get("action") == "generate_pbip":
            f = plan.get("fields_used") or {}
            lines.append(
                f"**{plan.get('report_name')}** — ready to download "
                f"({plan.get('visual_count')} visuals)."
            )
            lines.append(
                f"\nLive-connects to **{plan.get('dataset_name')}** in "
                f"*{plan.get('workspace_name')}*. Nothing is published to the "
                f"service; the file holds no data, so row-level security still "
                f"applies when you open it."
            )
            if f.get("measures"):
                lines.append("\nFields used:")
                lines.append("- measures: " + ", ".join(
                    f"`{m}`" for m in f["measures"]))
                if f.get("group_by"):
                    lines.append(f"- grouped by: `{f['group_by']}`")
                if f.get("date"):
                    lines.append(f"- trended on: `{f['date']}`")
            lines.append(
                "\nOpen the `.pbip` in Power BI Desktop, adjust it, and publish "
                "it yourself when you are happy with it."
            )
        else:
            lines.append(f"**Proposed: {plan.get('action')}** — not applied.")
            if plan.get("measure_name"):
                lines.append(
                    f"\n`[{plan['measure_name']}] = {plan.get('expression')}`")
            if plan.get("steps"):
                lines.append("\nSteps:\n" + "\n".join(
                    f"{i+1}. {s}" for i, s in enumerate(plan["steps"])))
            if plan.get("script"):
                lines.append(f"\n```python\n{plan['script']}\n```")

    else:  # snapshot
        if ctx.evidence:
            lines.extend(f"- {e.cite()}" for e in ctx.evidence[:20])
        elif rows:
            lines.append(_rows_table(rows))
        else:
            lines.append(
                "I found nothing matching that in the snapshot. It may exist "
                "but not be covered by the last scan, or the name may differ."
            )

    # --- provenance block, always present ---------------------------------
    as_of = next((e.as_of_utc for e in ctx.evidence if e.as_of_utc), None)
    src = PLANE_LABEL.get(ctx.plane, "unknown")
    foot = [f"_Source: {src}._"]
    if as_of:
        foot.append(f"_Snapshot built {as_of}._")
    if ctx.dax:
        foot.append(f"\n<details><summary>DAX executed</summary>\n\n```dax\n{ctx.dax}\n```\n</details>")

    return {
        "turn_id": ctx.turn_id,
        "answer": "\n".join(lines).strip(),
        "footer": " ".join(foot),
        "plane": ctx.plane.value if ctx.plane else None,
        "intent": ctx.intent.value if ctx.intent else None,
        "confidence": ctx.confidence,
        "citations": [e.cite() for e in ctx.evidence],
        "dax": ctx.dax,
        "rows": rows[:50],
        "row_count": len(rows),
        "requires_approval": ctx.requires_approval,
        "plan": ctx.entities.get("plan"),
        "warnings": ctx.errors,
    }
