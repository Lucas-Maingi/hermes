"""Meta WhatsApp Cloud API adapter: parse inbound webhooks, send outbound
messages via the Graph API.

This is the real WhatsApp integration. A business creates a Meta app, points
its webhook at ``/webhook``, and sets WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID
/ WHATSAPP_VERIFY_TOKEN. Parsing is pure and unit-tested; sending is a thin
Graph API call.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass

import httpx

_GRAPH_ORIGIN = "https://graph.facebook.com"


@dataclass
class InboundMessage:
    from_phone: str
    text: str
    message_id: str
    phone_number_id: str
    referral_source_id: str | None = None


def parse_inbound(payload: dict) -> list[InboundMessage]:
    """Extract text messages from a Meta webhook POST body.

    Meta batches messages under entry[].changes[].value.messages[]. Non-text
    messages and status/delivery events are ignored (return nothing for them).
    """
    out: list[InboundMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            for msg in value.get("messages", []):
                if msg.get("type") != "text":
                    continue
                referral = msg.get("referral", {})
                referral_source_id = (
                    str(referral.get("source_id", ""))
                    if isinstance(referral, dict) and referral.get("source_id") is not None
                    else None
                )
                out.append(
                    InboundMessage(
                        from_phone=msg.get("from", ""),
                        text=msg.get("text", {}).get("body", ""),
                        message_id=msg.get("id", ""),
                        phone_number_id=phone_number_id,
                        referral_source_id=referral_source_id,
                    )
                )
    return out


def verify_webhook(
    mode: str | None,
    token: str | None,
    challenge: str | None,
    *,
    expected_token: str | None = None,
) -> str | None:
    """Meta's GET verification handshake. Returns the challenge to echo back if
    the subscribe token matches, else None (caller returns 403)."""
    expected = expected_token if expected_token is not None else os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    if mode == "subscribe" and token and expected and hmac.compare_digest(token, expected):
        return challenge
    return None


def verify_signature(
    body: bytes,
    signature_header: str | None,
    *,
    app_secret: str | None = None,
) -> bool:
    """Verify Meta's ``X-Hub-Signature-256`` header: ``sha256=`` followed by
    the HMAC-SHA256 of the raw request body keyed with the app secret.

    An absent app secret or signature rejects the payload. The chat simulator
    does not use this webhook, so real webhook traffic should always be signed.
    """
    secret = app_secret if app_secret is not None else os.getenv("WHATSAPP_APP_SECRET", "")
    if not secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def check_connection(phone_number_id: str, access_token: str) -> bool:
    if not phone_number_id or not access_token:
        raise ValueError("WhatsApp phone-number ID and access token are required")
    version = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v26.0")
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise ValueError("WHATSAPP_GRAPH_API_VERSION must look like v26.0")
    response = httpx.get(
        f"{_GRAPH_ORIGIN}/{version}/{phone_number_id}",
        headers={"Authorization": "Bearer " + access_token},
        params={"fields": "id,display_phone_number,verified_name"},
        timeout=15.0,
    )
    return response.status_code < 300


def send_text(
    to_phone: str,
    text: str,
    *,
    phone_number_id: str | None = None,
    access_token: str | None = None,
) -> bool:
    """Send a WhatsApp text via the Graph API. No-op (returns False) when no
    token is configured, so the app runs credential-free in the simulator."""
    token = access_token or os.getenv("WHATSAPP_TOKEN")
    number_id = phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not number_id:
        return False
    version = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v26.0")
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise ValueError("WHATSAPP_GRAPH_API_VERSION must look like v26.0")
    try:
        resp = httpx.post(
            f"{_GRAPH_ORIGIN}/{version}/{number_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to_phone,
                "type": "text",
                "text": {"body": text},
            },
            timeout=15.0,
        )
        return resp.status_code < 300
    except httpx.HTTPError:
        return False


def send_template(
    to_phone: str,
    name: str,
    language: str,
    body_parameters: list[str],
    *,
    phone_number_id: str | None = None,
    access_token: str | None = None,
) -> bool:
    """Send an already-approved WhatsApp template using body text parameters."""
    token = access_token or os.getenv("WHATSAPP_TOKEN")
    number_id = phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not number_id:
        return False
    if not re.fullmatch(r"[a-z0-9_]{1,512}", name):
        raise ValueError("WhatsApp template name is invalid")
    if not re.fullmatch(r"[a-z]{2,3}(?:_[A-Z]{2})?", language):
        raise ValueError("WhatsApp template language is invalid")
    if len(body_parameters) > 10 or any(not isinstance(value, str) or len(value) > 1024 for value in body_parameters):
        raise ValueError("WhatsApp template supports up to 10 body parameters of at most 1,024 characters")
    version = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v26.0")
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise ValueError("WHATSAPP_GRAPH_API_VERSION must look like v26.0")
    template: dict = {"name": name, "language": {"code": language}}
    if body_parameters:
        template["components"] = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": value} for value in body_parameters],
            }
        ]
    try:
        response = httpx.post(
            f"{_GRAPH_ORIGIN}/{version}/{number_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to_phone,
                "type": "template",
                "template": template,
            },
            timeout=15.0,
        )
        return response.status_code < 300
    except httpx.HTTPError:
        return False
