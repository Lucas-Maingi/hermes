"""A real LLM adapter for any OpenAI-compatible chat-completions API.

This one class covers several providers by configuration alone -- notably
**Groq** (free tier, fast, supports tool calling) and OpenAI itself -- because
they share the ``/chat/completions`` request/response shape and the same
tool-calling schema. This is what makes Hermes a real product and not a
demo: point it at a provider with a key and the agent runs on a real model.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from hermes.llm.base import LLMResponse, ToolCall, ToolSpec


class OpenAICompatibleLLM:
    """LLMClient backed by an OpenAI-compatible /chat/completions endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.groq.com/openai/v1",
        timeout: float = 30.0,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *self._to_api_messages(messages)],
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = [self._tool_to_api(t) for t in tools]
            payload["tool_choice"] = "auto"

        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return self._parse(resp.json())

    @staticmethod
    def _to_api_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
        # Our internal "tool" role carries a result string; OpenAI-compatible
        # APIs accept a plain tool message, but to stay robust across providers
        # we fold tool results into a user-visible context message.
        out = []
        for m in messages:
            if m["role"] == "tool":
                out.append({"role": "user", "content": f"[tool result] {m['content']}"})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    @staticmethod
    def _tool_to_api(tool: ToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": {
                    "type": "object",
                    "properties": tool.parameters,
                    "required": tool.required,
                },
            },
        }

    @staticmethod
    def _parse(data: dict[str, Any]) -> LLMResponse:
        choice = data["choices"][0]["message"]
        text = choice.get("content") or ""
        tool_calls = []
        for tc in choice.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(name=fn.get("name", ""), arguments=args, call_id=tc.get("id", "")))
        return LLMResponse(text=text, tool_calls=tool_calls)
