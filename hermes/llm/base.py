"""The LLM abstraction the agent depends on.

Deliberately minimal: a single ``complete`` call that takes the running
message history plus the tools the agent is allowed to use, and returns either
some text to say to the customer, one or more tool calls to execute, or both.
This is the common shape across Claude, OpenAI, Gemini, and Groq tool-use APIs,
so a concrete provider is a thin translation layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSpec:
    """A tool the model may call, described in a provider-neutral way."""

    name: str
    description: str
    # JSON-schema-style parameter definition: {name: {type, description, ...}}
    parameters: dict[str, Any]
    required: list[str] = field(default_factory=list)


@dataclass
class ToolCall:
    """A model's request to invoke a tool with concrete arguments."""

    name: str
    arguments: dict[str, Any]
    call_id: str = ""


@dataclass
class LLMResponse:
    """One assistant turn: free-text to say, and/or tools to run."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    """Any object that can turn a conversation + toolset into a response."""

    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        """Given a system prompt, the message history (each ``{"role", "content"}``
        with role in {"user", "assistant", "tool"}), and the available tools,
        return the assistant's next turn."""
        ...
