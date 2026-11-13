import httpx
import pytest

from hermes.whatsapp import check_connection, parse_inbound, send_template, send_text, verify_signature


class TestWhatsAppSend:
    def test_sends_text_to_configured_graph_version(self, monkeypatch):
        calls = []

        class Response:
            status_code = 200

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return Response()

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setenv("WHATSAPP_TOKEN", "test-token")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456")
        monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v26.0")

        assert send_text("254712345678", "Hello")
        assert calls[0][0] == "https://graph.facebook.com/v26.0/123456/messages"
        assert calls[0][1]["json"]["text"]["body"] == "Hello"

    def test_invalid_graph_version_fails_explicitly(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_TOKEN", "test-token")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456")
        monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "https://example.test")

        with pytest.raises(ValueError, match="must look like"):
            send_text("254712345678", "Hello")

    def test_http_failure_returns_false(self, monkeypatch):
        def fake_post(*args, **kwargs):
            raise httpx.ConnectError("offline")

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setenv("WHATSAPP_TOKEN", "test-token")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456")

        assert send_text("254712345678", "Hello") is False

    def test_sends_approved_template_with_body_parameters(self, monkeypatch):
        calls = []

        class Response:
            status_code = 200

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return Response()

        monkeypatch.setattr(httpx, "post", fake_post)
        assert send_template(
            "254712345678",
            "order_reminder",
            "en",
            ["Mary", "ORD-12"],
            phone_number_id="123456",
            access_token="test-token",
        )
        assert calls[0][1]["headers"]["Authorization"] == "Bearer test-token"
        assert calls[0][1]["json"]["template"] == {
            "name": "order_reminder",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Mary"},
                        {"type": "text", "text": "ORD-12"},
                    ],
                }
            ],
        }

    def test_template_parameter_limits_are_validated(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_TOKEN", "test-token")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456")
        with pytest.raises(ValueError, match="up to 10"):
            send_template("254712345678", "order_reminder", "en", ["value"] * 11)

    def test_read_only_connection_check_does_not_send_message(self, monkeypatch):
        calls = []

        class Response:
            status_code = 200

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return Response()

        monkeypatch.setattr(httpx, "get", fake_get)
        assert check_connection("phone-123", "test-token") is True
        assert calls[0][0] == "https://graph.facebook.com/v26.0/phone-123"
        assert calls[0][1]["params"]["fields"] == "id,display_phone_number,verified_name"
        assert calls[0][1]["headers"]["Authorization"].startswith("Bearer ")


def test_inbound_parser_preserves_click_to_whatsapp_campaign_source():
    messages = parse_inbound(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "phone-123"},
                                "messages": [
                                    {
                                        "id": "message-1",
                                        "from": "254700000000",
                                        "type": "text",
                                        "text": {"body": "Hello"},
                                        "referral": {"source_id": "meta-ad-123", "source_type": "ad"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    )
    assert len(messages) == 1
    assert messages[0].referral_source_id == "meta-ad-123"

def test_signature_requires_secret(monkeypatch):
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    assert verify_signature(b"{}", None) is False


def test_explicit_tenant_secret_never_falls_back_to_global_secret(monkeypatch):
    import hashlib
    import hmac

    monkeypatch.setenv("WHATSAPP_APP_SECRET", "global-secret")
    body = b'{"entry":[]}'
    signature = "sha256=" + hmac.new(b"global-secret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, signature, app_secret="") is False
