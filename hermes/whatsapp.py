"""Meta WhatsApp Cloud API adapter: parse inbound webhooks, send outbound
messages via the Graph API.

This is the real WhatsApp integration. A business creates a Meta app, points
its webhook at ``/webhook``, and sets WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID
/ WHATSAPP_VERIFY_TOKEN. Parsing is pure and unit-tested; sending is a thin
Graph API call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

GRAPH_BASE = "https://graph.facebook.com/v21.0"


@dataclass
class InboundMessage:
    from_phone: str
    text: str
    message_id: str
    phone_number_id: str


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
                out.append(
                    InboundMessage(
                        from_phone=msg.get("from", ""),
                        text=msg.get("text", {}).get("body", ""),
                        message_id=msg.get("id", ""),
                        phone_number_id=phone_number_id,
                    )
                )
    return out


def verify_webhook(mode: str | None, token: str | None, challenge: str | None) -> str | None:
    """Meta's GET verification handshake. Returns the challenge to echo back if
    the subscribe token matches, else None (caller returns 403)."""
    expected = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    if mode == "subscribe" and token and token == expected:
        return challenge
    return None


def send_text(to_phone: str, text: str, *, phone_number_id: str | None = None) -> bool:
    """Send a WhatsApp text via the Graph API. No-op (returns False) when no
    token is configured, so the app runs credential-free in the simulator."""
    token = os.getenv("WHATSAPP_TOKEN")
    number_id = phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not number_id:
        return False
    try:
        resp = httpx.post(
            f"{GRAPH_BASE}/{number_id}/messages",
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
