import hashlib
import hmac
import json

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hermes.app import _validate_provider_url, create_app
from hermes.business import Business
from hermes.llm.mock import MockLLM
from hermes.mpesa.simulator import MpesaSimulator
from hermes.runtime import Runtime
from hermes.sample_business import sample_business


@pytest.fixture
def sim():
    return MpesaSimulator()


@pytest.fixture
def client(tmp_path, sim, monkeypatch):
    monkeypatch.setenv("HERMES_CHAT_API_TOKEN", "test-chat-token")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "test-app-secret")
    rt = Runtime(
        db_path=str(tmp_path / "hermes.db"),
        business=sample_business(),
        mpesa=sim,
        llm=MockLLM(),
    )
    return TestClient(create_app(rt)), rt, sim


def signed_json_post(client, path, payload, secret="test-app-secret"):
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )


@pytest.mark.parametrize(
    "url",
    ["http://api.example.test/v1", "https://127.0.0.1/v1", "https://localhost/v1"],
)
def test_external_provider_urls_require_public_https(url):
    with pytest.raises(HTTPException) as error:
        _validate_provider_url(url, "Provider URL")
    assert error.value.status_code == 400


class TestHealth:
    def test_health_reports_providers(self, client):
        c, _, _ = client
        body = c.get("/health").json()
        assert body["status"] == "healthy"
        assert body["llm_provider"] == "MockLLM"
        assert body["mpesa_provider"] == "MpesaSimulator"
        assert body["business_count"] == 1


class TestChat:
    def test_chat_returns_reply_and_conversation_id(self, client):
        c, _, _ = client
        r = c.post(
            "/chat",
            json={"text": "How much is sugar?", "phone": "254712345678"},
            headers={"Authorization": "Bearer test-chat-token"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "160" in body["reply"]
        assert body["conversation_id"]

    def test_chat_order_then_pay_returns_checkout_id(self, client):
        c, _, _ = client
        headers = {"Authorization": "Bearer test-chat-token"}
        c.post("/chat", json={"text": "I want 2 maize flour", "phone": "254712345678"}, headers=headers)
        r = c.post("/chat", json={"text": "pay with mpesa", "phone": "254712345678"}, headers=headers)
        body = r.json()
        assert body["order_total"] == 360
        assert body["order_status"] == "awaiting_payment"
        assert body["checkout_request_id"]

    def test_chat_api_fails_closed_without_configured_token(self, client, monkeypatch):
        c, _, _ = client
        monkeypatch.delenv("HERMES_CHAT_API_TOKEN")
        response = c.post("/chat", json={"text": "hello"})
        assert response.status_code == 503

    def test_chat_api_rejects_invalid_token(self, client):
        c, _, _ = client
        response = c.post(
            "/chat",
            json={"text": "hello"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert response.status_code == 401


class TestKnowledgeFileUpload:
    @staticmethod
    def authenticated_client(client):
        test_client, runtime, _ = client
        user_id, _ = runtime.platform.create_business(
            name="Upload shop",
            email="upload@example.test",
            password="long-upload-test-password",
        )
        verification_token = runtime.platform.create_email_verification_token(user_id)
        assert runtime.platform.verify_email_token(verification_token) is not None
        test_client.cookies.set("hermes_session", runtime.platform.create_session(user_id))
        return test_client, runtime

    def test_upload_extracts_and_indexes_csv(self, client):
        test_client, runtime = self.authenticated_client(client)
        response = test_client.post(
            "/api/knowledge/upload",
            files={"file": ("returns.csv", b"Product,Policy\nMaize flour,Returns within three days\n", "text/csv")},
            data={"consent_external_embedding": "false"},
        )

        assert response.status_code == 201
        result = response.json()
        assert result["title"] == "returns.csv"
        assert result["characters_indexed"] > 0
        business_id = runtime.platform.session_user(test_client.cookies["hermes_session"])["business_id"]
        indexed = runtime.platform.documents(business_id)
        assert indexed[0]["id"] == result["id"]
        from hermes.rag import HybridRetriever

        matches = HybridRetriever(runtime.platform).search(business_id, "returns within three days")
        assert matches and "Returns within three days" in matches[0]["text"]

    def test_upload_rejects_unsupported_and_unreadable_files(self, client):
        test_client, _ = self.authenticated_client(client)

        unsupported = test_client.post(
            "/api/knowledge/upload",
            files={"file": ("archive.zip", b"not a document", "application/zip")},
        )
        assert unsupported.status_code == 400
        assert "Unsupported file type" in unsupported.json()["detail"]

        unreadable = test_client.post(
            "/api/knowledge/upload",
            files={"file": ("scan.pdf", b"%PDF-1.4 no text", "application/pdf")},
        )
        assert unreadable.status_code == 400
        assert "PDF" in unreadable.json()["detail"]

    def test_upload_respects_external_embedding_consent(self, client, monkeypatch):
        test_client, _ = self.authenticated_client(client)
        indexed = []

        class EmbeddingRetriever:
            embedding_client = object()

            def index_document(self, business_id, title, text):
                indexed.append((business_id, title, text))
                return "indexed-document"

        monkeypatch.setattr("hermes.app.build_retriever", lambda store, business_id: EmbeddingRetriever())
        file = ("policy.txt", b"Returns are accepted within three days.", "text/plain")

        denied = test_client.post("/api/knowledge/upload", files={"file": file})
        assert denied.status_code == 400
        assert indexed == []

        allowed = test_client.post(
            "/api/knowledge/upload",
            files={"file": file},
            data={"consent_external_embedding": "true"},
        )
        assert allowed.status_code == 201
        assert indexed and indexed[0][1] == "policy.txt"

    def test_upload_requires_authentication_and_enforces_file_size(self, client):
        test_client, _, _ = client
        unauthorized = test_client.post(
            "/api/knowledge/upload",
            files={"file": ("policy.txt", b"Returns are accepted.", "text/plain")},
        )
        assert unauthorized.status_code == 401

        authenticated, _ = self.authenticated_client(client)
        oversized = authenticated.post(
            "/api/knowledge/upload",
            files={"file": ("large.txt", b"x" * (15 * 1024 * 1024 + 1), "text/plain")},
        )
        assert oversized.status_code == 413


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
        r = signed_json_post(c, "/webhook", self._meta_payload("254712345678", "How much is bread?"))
        assert r.status_code == 200
        conv = rt.store.get_latest_by_phone(
            "254712345678",
            business_id=rt.default_business.id,
        )
        assert conv is not None
        assert conv.channel == "whatsapp"
        # The agent answered from the catalog (bread = 70).
        assert any("70" in m.text for m in conv.messages)

    def test_customer_stop_keyword_revokes_automated_followups(self, client):
        from cryptography.fernet import Fernet

        c, rt, _ = client
        rt.platform._fernet = Fernet(Fernet.generate_key())
        phone = "254712345678"
        rt.platform.record_whatsapp_consent(
            rt.default_business.id,
            phone,
            opted_in=True,
            source="Test opt-in",
        )
        response = signed_json_post(c, "/webhook", self._meta_payload(phone, "STOP"))
        assert response.status_code == 200
        with rt.platform._connect() as connection:
            consent = connection.execute(
                "SELECT status FROM whatsapp_consents WHERE business_id = ? AND phone_hash = ?",
                (rt.default_business.id, hashlib.sha256(phone.encode()).hexdigest()),
            ).fetchone()
        assert consent["status"] == "opted_out"
        with rt.platform._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0] == 0

    def test_status_events_are_ignored(self, client):
        c, rt, _ = client
        # A delivery-status webhook (no messages) must not error.
        r = signed_json_post(
            c, "/webhook", {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
        )
        assert r.status_code == 200

    def test_multitenant_routing_and_history_are_isolated(self, tmp_path, sim, monkeypatch):
        monkeypatch.setenv("HERMES_ENCRYPTION_KEY", Fernet.generate_key().decode())
        first = Business(
            id="shop_a",
            name="Shop A",
            info={"contact": "111"},
            whatsapp_phone_number_id="PN1",
        )
        second = Business(
            id="shop_b",
            name="Shop B",
            info={"contact": "222"},
            whatsapp_phone_number_id="PN2",
        )
        rt = Runtime(
            db_path=str(tmp_path / "multi.db"),
            businesses=[first, second],
            mpesa=sim,
            llm=MockLLM(),
        )
        for business_id, phone_number_id, secret in (
            ("shop_a", "PN1", "shop-a-app-secret"),
            ("shop_b", "PN2", "shop-b-app-secret"),
        ):
            rt.platform.save_integrations(
                business_id,
                whatsapp_phone_number_id=phone_number_id,
                whatsapp_token="wa-token",
                whatsapp_app_secret=secret,
                whatsapp_verify_token=f"{business_id}-verify-token",
                mpesa_credentials={},
                llm_credentials={},
            )
        c = TestClient(create_app(rt))

        signed_json_post(
            c, "/webhook", self._meta_payload("254712345678", "what is your contact?"), "shop-a-app-secret"
        )
        second_payload = self._meta_payload("254712345678", "what is your contact?")
        second_payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "PN2"
        signed_json_post(c, "/webhook", second_payload, "shop-b-app-secret")

        conv_a = rt.store.get_latest_by_phone("254712345678", business_id="shop_a")
        conv_b = rt.store.get_latest_by_phone("254712345678", business_id="shop_b")
        assert conv_a is not None and conv_a.business_id == "shop_a"
        assert conv_b is not None and conv_b.business_id == "shop_b"
        assert conv_a.id != conv_b.id

    def test_unknown_phone_number_id_does_not_fall_back_to_another_tenant(self, tmp_path, sim, monkeypatch):
        first = Business(id="shop_a", name="Shop A", whatsapp_phone_number_id="PN1")
        second = Business(id="shop_b", name="Shop B", whatsapp_phone_number_id="PN2")
        rt = Runtime(
            db_path=str(tmp_path / "multi.db"),
            businesses=[first, second],
            mpesa=sim,
            llm=MockLLM(),
        )
        c = TestClient(create_app(rt))
        payload = self._meta_payload("254712345678", "hello")
        payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "UNKNOWN"

        response = signed_json_post(c, "/webhook", payload)
        assert response.status_code == 403
        assert rt.store.list_conversations(business_id="shop_a") == []
        assert rt.store.list_conversations(business_id="shop_b") == []


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

    def test_missing_secret_rejects_webhook(self, client, monkeypatch):
        c, _, _ = client
        monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
        r = c.post("/webhook", json={"entry": []})
        assert r.status_code == 403


class TestMpesaCallback:
    def test_success_callback_marks_order_paid(self, client):
        c, rt, sim = client
        headers = {"Authorization": "Bearer test-chat-token"}
        c.post("/chat", json={"text": "2 maize flour", "phone": "254712345678"}, headers=headers)
        pay = c.post("/chat", json={"text": "pay", "phone": "254712345678"}, headers=headers).json()
        checkout_id = pay["checkout_request_id"]

        # Customer pays -> Daraja posts the callback.
        callback_body = sim.complete_payment(checkout_id, success=True)
        r = c.post("/mpesa/callback", json=callback_body)
        assert r.json()["ResultCode"] == 0

        conv = rt.store.get_by_checkout(checkout_id)
        assert conv.payment.status.value == "success"
        assert conv.payment.receipt  # populated from the callback metadata
        assert conv.order.status.value == "paid"

    def test_mismatched_amount_callback_cannot_mark_order_paid(self, client):
        c, rt, sim = client
        headers = {"Authorization": "Bearer test-chat-token"}
        c.post("/chat", json={"text": "2 maize flour", "phone": "254712345678"}, headers=headers)
        pay = c.post("/chat", json={"text": "pay", "phone": "254712345678"}, headers=headers).json()
        checkout_id = pay["checkout_request_id"]
        callback_body = sim.complete_payment(checkout_id, success=True)
        callback_body["Body"]["stkCallback"]["CallbackMetadata"]["Item"][0]["Value"] = 1

        c.post("/mpesa/callback", json=callback_body)

        conv = rt.store.get_by_checkout(checkout_id)
        assert conv.payment.status.value == "pending"
        assert conv.order.status.value == "awaiting_payment"

    def test_duplicate_failed_callback_does_not_downgrade_success(self, client):
        c, rt, sim = client
        headers = {"Authorization": "Bearer test-chat-token"}
        c.post("/chat", json={"text": "2 maize flour", "phone": "254712345678"}, headers=headers)
        pay = c.post("/chat", json={"text": "pay", "phone": "254712345678"}, headers=headers).json()
        checkout_id = pay["checkout_request_id"]
        c.post("/mpesa/callback", json=sim.complete_payment(checkout_id, success=True))
        c.post("/mpesa/callback", json=sim.complete_payment(checkout_id, success=False))

        conv = rt.store.get_by_checkout(checkout_id)
        assert conv.payment.status.value == "success"
        assert conv.order.status.value == "paid"

    def test_non_callback_body_is_ignored_gracefully(self, client):
        c, _, _ = client
        r = c.post("/mpesa/callback", json={"something": "else"})
        assert r.status_code == 200
        assert r.json()["ResultCode"] == 0
