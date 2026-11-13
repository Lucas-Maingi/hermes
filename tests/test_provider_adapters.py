import pytest

from hermes.llm.base import ToolSpec
from hermes.llm.openai_compatible import OpenAICompatibleLLM
from hermes.models import PaymentStatus
from hermes.mpesa.daraja import SANDBOX_BASE, DarajaClient


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        return None


def test_openai_compatible_adapter_sends_tools_and_parses_tool_calls(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "lookup_knowledge",
                                        "arguments": '{"query":"price of sugar"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("hermes.llm.openai_compatible.httpx.post", fake_post)
    llm = OpenAICompatibleLLM(api_key="test-key", model="test-model")
    response = llm.complete(
        "You are a shop assistant",
        [{"role": "user", "content": "How much is sugar?"}],
        [ToolSpec(name="lookup_knowledge", description="Look up facts", parameters={"query": {"type": "string"}})],
    )

    assert calls[0][0] == "https://api.groq.com/openai/v1/chat/completions"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-key"
    assert calls[0][1]["json"]["tools"][0]["function"]["name"] == "lookup_knowledge"
    assert response.tool_calls[0].arguments == {"query": "price of sugar"}


def test_daraja_adapter_requests_token_pushes_and_queries(monkeypatch):
    get_calls = []
    post_calls = []

    def fake_get(url, **kwargs):
        get_calls.append((url, kwargs))
        return FakeResponse({"access_token": "test-token", "expires_in": "3600"})

    def fake_post(url, **kwargs):
        post_calls.append((url, kwargs))
        if url.endswith("/processrequest"):
            return FakeResponse(
                {
                    "ResponseCode": "0",
                    "CheckoutRequestID": "checkout_1",
                    "MerchantRequestID": "merchant_1",
                }
            )
        return FakeResponse({"ResultCode": "0"})

    monkeypatch.setattr("hermes.mpesa.daraja.httpx.get", fake_get)
    monkeypatch.setattr("hermes.mpesa.daraja.httpx.post", fake_post)
    daraja = DarajaClient(
        consumer_key="consumer-key",
        consumer_secret="consumer-secret",
        shortcode="174379",
        passkey="passkey",
        callback_url="https://example.test/mpesa/callback",
    )

    push = daraja.stk_push(
        phone="0712345678",
        amount=180,
        account_reference="order_1",
        description="Shop payment",
    )
    status = daraja.query_status(push.checkout_request_id)

    assert get_calls[0][0] == f"{SANDBOX_BASE}/oauth/v1/generate"
    assert push.success is True
    assert post_calls[0][0] == f"{SANDBOX_BASE}/mpesa/stkpush/v1/processrequest"
    assert post_calls[0][1]["headers"]["Authorization"] == "Bearer test-token"
    assert post_calls[0][1]["json"]["PhoneNumber"] == "254712345678"
    assert post_calls[0][1]["json"]["Amount"] == 180
    assert status == PaymentStatus.SUCCESS


def test_daraja_rejects_unknown_environment():
    with pytest.raises(ValueError, match="MPESA_ENVIRONMENT"):
        DarajaClient(
            consumer_key="consumer-key",
            consumer_secret="consumer-secret",
            shortcode="174379",
            passkey="passkey",
            callback_url="https://example.test/mpesa/callback",
            environment="prodution",
        )
