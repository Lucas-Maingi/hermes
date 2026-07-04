"""Select an LLM provider from environment configuration.

Resolution order:
  1. ``HERMES_LLM_PROVIDER=groq|openai|openai_compatible`` with the matching
     API key set  -> a real model.
  2. Nothing configured  -> the deterministic MockLLM.

This is the seam that lets the same codebase run credential-free in CI and the
demo, and on a real model in production, with no code change.

Environment variables:
  HERMES_LLM_PROVIDER   groq | openai | mock (default: auto)
  HERMES_LLM_MODEL      model id (sensible per-provider default otherwise)
  GROQ_API_KEY          for provider=groq
  OPENAI_API_KEY        for provider=openai
  HERMES_LLM_BASE_URL   override the OpenAI-compatible endpoint
  HERMES_LLM_API_KEY    generic key for a custom openai_compatible endpoint
"""

from __future__ import annotations

import os

from hermes.llm.base import LLMClient
from hermes.llm.mock import MockLLM

_PROVIDER_DEFAULTS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "key_env": "GROQ_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
    },
}


def get_llm_client() -> LLMClient:
    provider = os.getenv("HERMES_LLM_PROVIDER", "auto").lower()

    if provider in ("mock", ""):
        return MockLLM()

    # Auto: use the first provider whose key is present, else the mock.
    if provider == "auto":
        for name, cfg in _PROVIDER_DEFAULTS.items():
            if os.getenv(cfg["key_env"]):
                provider = name
                break
        else:
            return MockLLM()

    # Import lazily so httpx isn't required for the mock-only path.
    from hermes.llm.openai_compatible import OpenAICompatibleLLM

    if provider in _PROVIDER_DEFAULTS:
        cfg = _PROVIDER_DEFAULTS[provider]
        api_key = os.getenv(cfg["key_env"])
        if not api_key:
            return MockLLM()
        return OpenAICompatibleLLM(
            api_key=api_key,
            model=os.getenv("HERMES_LLM_MODEL", cfg["model"]),
            base_url=os.getenv("HERMES_LLM_BASE_URL", cfg["base_url"]),
        )

    # A custom OpenAI-compatible endpoint.
    api_key = os.getenv("HERMES_LLM_API_KEY")
    base_url = os.getenv("HERMES_LLM_BASE_URL")
    if api_key and base_url:
        from hermes.llm.openai_compatible import OpenAICompatibleLLM

        return OpenAICompatibleLLM(
            api_key=api_key,
            model=os.getenv("HERMES_LLM_MODEL", "llama-3.3-70b-versatile"),
            base_url=base_url,
        )

    return MockLLM()
