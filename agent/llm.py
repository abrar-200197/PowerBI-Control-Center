"""
The LLM client behind the agent loop.

Two implementations behind one interface:

  AzureOpenAILLM  the real thing -- Azure OpenAI chat completions with
                  tool calling. This is the brain in production.

  ScriptedLLM     a deterministic fake that replays a fixed list of turns.
                  This is what makes an agent loop testable at all: you can
                  assert "given this sequence of model decisions, the loop
                  called these tools, recovered from that error, and produced
                  this answer" without ever hitting a network.

Testing an LLM agent by calling a real LLM gives you a flaky test that tells
you about the model. Testing with a scripted model tells you about YOUR LOOP,
which is the part you actually wrote and the part that can be wrong.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMReply:
    """One model turn: either tool calls, or a final answer, or both."""
    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMUnavailable(Exception):
    pass


# ---------------------------------------------------------------------------
class ScriptedLLM:
    """Replays a fixed script of turns. Each entry is either:
        {"tools": [(name, args), ...]}   -> the model asks for tool calls
        {"content": "final answer"}      -> the model answers
    """

    def __init__(self, script: List[Dict[str, Any]]):
        self.script = list(script)
        self.calls: List[List[Dict[str, Any]]] = []   # messages seen per turn
        self.turn = 0

    def chat(self, messages, tools=None, **kw) -> LLMReply:
        self.calls.append(list(messages))
        if self.turn >= len(self.script):
            # Script exhausted: behave like a model that decides it is done.
            return LLMReply(content="(scripted model ran out of turns)")
        step = self.script[self.turn]
        self.turn += 1
        if "tools" in step:
            return LLMReply(tool_calls=[
                ToolCall(id=f"call_{self.turn}_{i}", name=n, arguments=a)
                for i, (n, a) in enumerate(step["tools"])])
        return LLMReply(content=step.get("content", ""))


# ---------------------------------------------------------------------------
class AzureOpenAILLM:
    """Azure OpenAI chat completions with tool calling.

    Uses `requests` directly rather than the openai SDK: your app already
    depends on requests, and this is ~30 lines. One less package to get
    approved, one less version to pin.
    """

    def __init__(self, endpoint=None, deployment=None, api_key=None,
                 api_version=None, timeout=60):
        self.endpoint = (endpoint or os.getenv("AZURE_OPENAI_ENDPOINT") or "").rstrip("/")
        self.deployment = deployment or os.getenv("AZURE_OPENAI_DEPLOYMENT")
        self.api_key = api_key or os.getenv("AZURE_OPENAI_API_KEY")
        self.api_version = (api_version
                            or os.getenv("AZURE_OPENAI_API_VERSION")
                            or "2024-10-21")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.deployment and self.api_key)

    def chat(self, messages, tools=None, temperature=0.0, **kw) -> LLMReply:
        if not self.configured:
            raise LLMUnavailable(
                "Azure OpenAI is not configured. Set AZURE_OPENAI_ENDPOINT, "
                "AZURE_OPENAI_DEPLOYMENT and AZURE_OPENAI_API_KEY.")
        try:
            import requests
        except ImportError as exc:
            raise LLMUnavailable("requests is not installed") from exc

        url = (f"{self.endpoint}/openai/deployments/{self.deployment}"
               f"/chat/completions?api-version={self.api_version}")
        body: Dict[str, Any] = {"messages": messages, "temperature": temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        resp = requests.post(url, headers={"api-key": self.api_key,
                                           "Content-Type": "application/json"},
                             json=body, timeout=self.timeout)
        if resp.status_code >= 400:
            raise LLMUnavailable(f"Azure OpenAI {resp.status_code}: "
                                 f"{resp.text[:400]}")
        msg = resp.json()["choices"][0]["message"]

        calls = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                # Models occasionally emit malformed JSON arguments. Surface it
                # as an empty call so the loop can feed the error back and let
                # the model retry, rather than crashing the request.
                args = {}
            calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""),
                                  arguments=args))
        return LLMReply(content=msg.get("content"), tool_calls=calls)


def default_llm():
    """The real LLM if configured, else None (caller falls back to rules)."""
    llm = AzureOpenAILLM()
    return llm if llm.configured else None
