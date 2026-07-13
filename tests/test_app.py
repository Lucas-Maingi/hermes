import pytest
from fastapi.testclient import TestClient

from hermes.app import create_app
from hermes.llm.mock import MockLLM
from hermes.mpesa.simulator import MpesaSimulator
from hermes.runtime import Runtime


@pytest.fixture
def sim():
    return MpesaSimulator()


@pytest.fixture
def client(tmp_path, sim):
    rt = Runtime(db_path=str(tmp_path / "hermes.db"), mpesa=sim, llm=MockLLM())
    return TestClient(create_app(rt)), rt, sim


class TestHealth:
    def test_health_reports_providers(self, client):
        c, _, _ = client
        body = c.get("/health").json()
        assert body["status"] == "healthy"
        assert body["llm_provider"] == "MockLLM"
        assert body["mpesa_provider"] == "MpesaSimulator"


class TestChat:
    def test_chat_returns_reply_and_conversation_id(self, client):
        c, _, _ = client
        r = c.post("/chat", json={"text": "How much is sugar?", "phone": "254712345678"})
        assert r.status_code == 200
        body = r.json()
        assert "160" in body["reply"]
        assert body["conversation_id"]

    def test_chat_order_then_pay_returns_checkout_id(self, client):
        c, _, _ = client
        c.post("/chat", json={"text": "I want 2 maize flour", "phone": "254712345678"})
        r = c.post("/chat", json={"text": "pay with mpesa", "phone": "254712345678"})
        body = r.json()
        assert body["order_total"] == 360
        assert body["order_status"] == "awaiting_payment"
        assert body["checkout_request_id"]


class TestWhatsAppVerification:
    def test_verify_echoes_challenge_with_correct_token(self, client, monkeypatch):
        c, _, _ = client
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "s3cret")
        r = c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "s3cret", "hub.challenge": "42"})
        assert r.status_code == 200
        assert r.text == "42"

    def test_verify_rejects_wrong_token(self, client, monkeypatch):
        c, _, _ = client
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "s3cret")
        r = c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
        assert r.status_code == 403


class TestWhatsAppInbound:
    def _meta_payload(self, phone, text):
        return {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "PN1"},
                                "messages": [
                                    {"from": phone, "id": "wamid.1", "type": "text", "text": {"body": text}}
                                ],
                            }
                        }
                    ]
                }
            ]
        }

    def test_inbound_message_creates_conversation(self, client):
        c, rt, _ = client
        r = c.post("/webhook", json=self._meta_payload("254712345678", "How much is bread?"))
        assert r.status_code == 200
        conv = rt.store.get_latest_by_phone("254712345678")
        assert conv is not None
        assert conv.channel == "whatsapp"
        # The agent answered from the catalog (bread = 70).
        assert any("70" in m.text for m in conv.messages)

    def test_status_events_are_ignored(self, client):
        c, rt, _ = client
        # A delivery-status webhook (no messages) must not error.
        r = c.post("/webhook", json={"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]})
        assert r.status_code == 200


class TestWebhookSignature:
    """Meta signs webhook POSTs with X-Hub-Signature-256 (HMAC-SHA256 of the
    raw body, keyed with the app secret). With a secret configured, unsigned
    or badly-signed payloads must be rejected."""

    def _sign(self, secret: str, body: bytes) -> str:
        import hashlib
        import hmac

        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_unsigned_payload_rejected_when_secret_set(self, client, monkeypatch):
        c, _, _ = client
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
        r = c.post("/webhook", json={"entry": []})
        assert r.status_code == 403

    def test_bad_signature_rejected(self, client, monkeypatch):
        c, _, _ = client
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
        r = c.post("/webhook", json={"entry": []}, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
        assert r.status_code == 403

    def test_valid_signature_accepted(self, client, monkeypatch):
        import json

        c, _, _ = client
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
        body = json.dumps({"entry": []}).encode()
        r = c.post(
            "/webhook",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": self._sign("app-secret", body),
            },
        )
        assert r.status_code == 200

    def test_no_secret_skips_verification(self, client, monkeypatch):
        c, _, _ = client
        monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
        r = c.post("/webhook", json={"entry": []})
        assert r.status_code == 200


class TestMpesaCallback:
    def test_success_callback_marks_order_paid(self, client):
        c, rt, sim = client
        c.post("/chat", json={"text": "2 maize flour", "phone": "254712345678"})
        pay = c.post("/chat", json={"text": "pay", "phone": "254712345678"}).json()
        checkout_id = pay["checkout_request_id"]

        # Customer pays -> Daraja posts the callback.
        callback_body = sim.complete_payment(checkout_id, success=True)
        r = c.post("/mpesa/callback", json=callback_body)
        assert r.json()["ResultCode"] == 0

        conv = rt.store.get_by_checkout(checkout_id)
        assert conv.payment.status.value == "success"
        assert conv.payment.receipt  # populated from the callback metadata
        assert conv.order.status.value == "paid"

    def test_non_callback_body_is_ignored_gracefully(self, client):
        c, _, _ = client
        r = c.post("/mpesa/callback", json={"something": "else"})
        assert r.status_code == 200
        assert r.json()["ResultCode"] == 0
