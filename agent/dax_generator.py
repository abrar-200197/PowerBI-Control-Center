"""
agent/dax_generator.py — NL -> DAX with a grounded, self-repairing loop.

The loop that matters:

    generate(question, schema) -> DAX
        -> executeQueries
        -> HTTP 400 ? feed the error text back in and regenerate (max 2 retries)
        -> still failing ? escalate to the user with the DAX we tried

Grounding is what actually fixes bad DAX. An ungrounded model invents column
names; one fed the real table/column/measure list from the snapshot rarely does.
That grounding comes from tools_governance.model_schema() + relationships() --
i.e. the Governance Agent runs BEFORE the Data Agent on every live turn.

Two backends:
  * AzureOpenAIGenerator - production. Reads config from env; no hardcoded
    endpoints (the current app.py hardcodes a DEV endpoint in prod, finding F12).
  * TemplateGenerator     - offline fallback used by the demo and tests.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Protocol

MAX_REPAIRS = 2

SYSTEM_PROMPT = """You write DAX queries for Power BI semantic models.

RULES:
- Return ONLY a DAX query. No markdown fences, no prose, no explanation.
- The query MUST start with EVALUATE.
- Use ONLY tables, columns and measures from the SCHEMA given below.
- Prefer existing measures over re-deriving arithmetic from columns.
- Reference measures as [Measure Name]; columns as 'Table'[Column].
- Always bound the result: wrap in TOPN(...) unless returning a single scalar.
- For a single scalar, use: EVALUATE ROW("Value", [Measure])
- Never use DEFINE MEASURE. Never emit more than one statement.
"""


class Generator(Protocol):
    def generate(self, question: str, schema_text: str,
                 prior_error: Optional[str] = None,
                 prior_dax: Optional[str] = None) -> str: ...


def schema_to_prompt(schema_rows: List[Dict[str, Any]],
                     measure_rows: List[Dict[str, Any]],
                     rel_rows: List[Dict[str, Any]]) -> str:
    """Compact grounding context. Keep it tight -- this rides in every prompt."""
    tables: Dict[str, List[str]] = {}
    for r in schema_rows:
        t = r.get("table_name")
        c = r.get("column_name")
        if t and c:
            tables.setdefault(t, []).append(f"{c} ({r.get('data_type') or '?'})")

    out = ["TABLES AND COLUMNS:"]
    for t, cols in sorted(tables.items()):
        out.append(f"  '{t}': {', '.join(cols[:40])}")

    if measure_rows:
        out.append("\nMEASURES (prefer these):")
        for m in measure_rows[:60]:
            desc = f"  -- {m['description']}" if m.get("description") else ""
            out.append(f"  [{m.get('measure_name')}] = {m.get('expression')}{desc}")

    if rel_rows:
        out.append("\nRELATIONSHIPS:")
        for r in rel_rows[:40]:
            out.append(
                f"  '{r.get('from_table')}'[{r.get('from_column')}] -> "
                f"'{r.get('to_table')}'[{r.get('to_column')}] ({r.get('cardinality')})"
            )
    return "\n".join(out)


def strip_fences(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```(?:dax|DAX)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


class AzureOpenAIGenerator:
    """Config comes from env only. Never hardcode the endpoint (see F12)."""

    def __init__(self) -> None:
        self.endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        self.api_key = os.getenv("AZURE_OPENAI_API_KEY")
        self.deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        self.api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
        if not self.endpoint or not self.api_key:
            raise RuntimeError(
                "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set "
                "(or use TemplateGenerator / DEMO_MODE=1)"
            )

    def generate(self, question: str, schema_text: str,
                 prior_error: Optional[str] = None,
                 prior_dax: Optional[str] = None) -> str:
        from openai import AzureOpenAI  # lazy import

        client = AzureOpenAI(azure_endpoint=self.endpoint, api_key=self.api_key,
                             api_version=self.api_version)
        user = f"SCHEMA:\n{schema_text}\n\nQUESTION: {question}"
        if prior_error:
            user += (
                f"\n\nYour previous query FAILED. Fix it.\n"
                f"PREVIOUS DAX:\n{prior_dax}\n"
                f"POWER BI ERROR:\n{prior_error}\n"
                f"Return corrected DAX only."
            )
        resp = client.chat.completions.create(
            model=self.deployment,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": user}],
            temperature=0,      # determinism matters more than creativity here
            max_tokens=800,
        )
        return strip_fences(resp.choices[0].message.content or "")


class TemplateGenerator:
    """Offline generator: enough to demo the repair loop without a model."""

    def generate(self, question: str, schema_text: str,
                 prior_error: Optional[str] = None,
                 prior_dax: Optional[str] = None) -> str:
        q = (question or "").lower()
        measures = re.findall(r"\[([^\]]+)\] =", schema_text)

        def pick(*words: str) -> Optional[str]:
            for m in measures:
                if any(w in m.lower() for w in words):
                    return m
            return measures[0] if measures else None

        # Measure-authoring asks for a bare expression, not a query.
        if "measure named" in q or "expression body" in q:
            base = pick("net sales", "sales", "revenue") or "Net Sales"
            if "return" in q:
                return f"DIVIDE(SUM(FACT_SALES[ReturnAmount]), [{base}])"
            if "rate" in q or "%" in q or "percent" in q:
                return f"DIVIDE([{base}], CALCULATE([{base}], ALL(FACT_SALES)))"
            return f"CALCULATE([{base}])"

        if prior_error and prior_dax:
            # crude repair: drop the offending column the error names
            bad = re.search(r"'?([A-Za-z0-9_ ]+)'? could not be found", prior_error)
            if bad:
                cleaned = prior_dax.replace(bad.group(1), measures[0] if measures else "1")
                return cleaned

        m = pick("net sales", "sales", "revenue") or "Net Sales"
        if re.search(r"\btop\s+(\d+)\b", q):
            n = re.search(r"\btop\s+(\d+)\b", q).group(1)
            dim = "DIM_STORE" if "store" in q else "DIM_STORE"
            return (f"EVALUATE\nTOPN({n},\n  SUMMARIZECOLUMNS('{dim}'[StoreName],\n"
                    f'    "{m}", [{m}]\n  ),\n  [{m}], DESC\n)')
        return f'EVALUATE\nROW("{m}", [{m}])'


def get_generator() -> Generator:
    if os.getenv("DEMO_MODE", "0") == "1" or not os.getenv("AZURE_OPENAI_ENDPOINT"):
        return TemplateGenerator()
    return AzureOpenAIGenerator()
