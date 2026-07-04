"""Provider-agnostic LLM layer.

The agent orchestrator depends only on the ``LLMClient`` protocol, so the
brain can be a real hosted model or a deterministic mock without any change
upstream. ``get_llm_client()`` selects a provider from environment config,
falling back to the mock when no key is configured -- which is what keeps
tests and the credential-free demo working.
"""

from hermes.llm.base import LLMClient, LLMResponse, ToolCall, ToolSpec
from hermes.llm.factory import get_llm_client
from hermes.llm.mock import MockLLM

__all__ = [
    "LLMClient",
    "LLMResponse",
    "ToolCall",
    "ToolSpec",
    "MockLLM",
    "get_llm_client",
]
