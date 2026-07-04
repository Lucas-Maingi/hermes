import pytest

from hermes.llm.factory import get_llm_client
from hermes.llm.mock import MockLLM

PROVIDER_ENV_KEYS = ["HERMES_LLM_PROVIDER", "GROQ_API_KEY", "OPENAI_API_KEY", "HERMES_LLM_API_KEY", "HERMES_LLM_BASE_URL"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestFactory:
    def test_defaults_to_mock_when_nothing_configured(self):
        assert isinstance(get_llm_client(), MockLLM)

    def test_explicit_mock_provider(self, monkeypatch):
        monkeypatch.setenv("HERMES_LLM_PROVIDER", "mock")
        assert isinstance(get_llm_client(), MockLLM)

    def test_auto_selects_groq_when_key_present(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        client = get_llm_client()
        assert type(client).__name__ == "OpenAICompatibleLLM"
        assert client.model  # a default model is set

    def test_provider_named_without_key_falls_back_to_mock(self, monkeypatch):
        monkeypatch.setenv("HERMES_LLM_PROVIDER", "groq")
        # no GROQ_API_KEY set
        assert isinstance(get_llm_client(), MockLLM)

    def test_custom_openai_compatible_endpoint(self, monkeypatch):
        monkeypatch.setenv("HERMES_LLM_PROVIDER", "openai_compatible")
        monkeypatch.setenv("HERMES_LLM_API_KEY", "k")
        monkeypatch.setenv("HERMES_LLM_BASE_URL", "https://example.test/v1")
        client = get_llm_client()
        assert type(client).__name__ == "OpenAICompatibleLLM"
        assert client.base_url == "https://example.test/v1"
