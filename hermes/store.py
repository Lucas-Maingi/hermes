"""SQLite persistence for conversations, and the metrics the owner dashboard
reports (deflection rate, orders, revenue, escalations).

Conversations serialize as JSON (str-enums make this clean). Kept single-file
and dependency-light for portfolio scale; a multi-tenant production deployment
would move to Postgres -- see the README's Known Limitations.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Union

from cryptography.fernet import Fernet

from hermes.models import (
    Conversation,
    Message,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentStatus,
    Role,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL,
    customer_phone TEXT,
    channel TEXT,
    needs_human INTEGER,
    order_status TEXT,
    order_total REAL,
    mpesa_checkout_id TEXT,
    created_at REAL,
    updated_at REAL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_business_created ON conversations(business_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conv_checkout ON conversations(mpesa_checkout_id);
CREATE INDEX IF NOT EXISTS idx_conv_tenant_phone ON conversations(business_id, customer_phone, updated_at);
"""


def _conversation_to_json(conv: Conversation, fernet: Fernet | None = None) -> str:
    data = json.dumps(asdict(conv))
    return f"enc:{fernet.encrypt(data.encode()).decode()}" if fernet else data


def _conversation_from_dict(d: dict) -> Conversation:
    conv = Conversation(
        id=d["id"],
        business_id=d.get("business_id", "duka_demo"),
        customer_phone=d.get("customer_phone", ""),
        channel=d.get("channel", "simulator"),
        needs_human=d.get("needs_human", False),
        created_at=d.get("created_at", 0.0),
    )
    conv.messages = [
        Message(role=Role(m["role"]), text=m["text"], timestamp=m["timestamp"], metadata=m.get("metadata", {}))
        for m in d.get("messages", [])
    ]
    order_d = d.get("order")
    if order_d:
        order = Order(
            id=order_d["id"],
            status=OrderStatus(order_d["status"]),
            delivery_note=order_d.get("delivery_note", ""),
            created_at=order_d.get("created_at", 0.0),
        )
        order.items = [OrderItem(**item) for item in order_d.get("items", [])]
        conv.order = order
    pay_d = d.get("payment")
    if pay_d:
        conv.payment = Payment(
            id=pay_d["id"],
            order_id=pay_d.get("order_id", ""),
            phone=pay_d.get("phone", ""),
            amount=pay_d.get("amount", 0.0),
            status=PaymentStatus(pay_d["status"]),
            checkout_request_id=pay_d.get("checkout_request_id"),
            receipt=pay_d.get("receipt"),
            created_at=pay_d.get("created_at", 0.0),
        )
    return conv


class HermesStore:
    def __init__(
        self,
        db_path: Union[str, Path] = "hermes.db",
        *,
        legacy_business_id: str = "duka_demo",
        encryption_key: str | None = None,
    ):
        self.db_path = str(db_path)
        key = encryption_key or os.getenv("HERMES_ENCRYPTION_KEY", "")
        self._fernet = Fernet(key.encode()) if key else None
        self._phone_lookup_key = hashlib.sha256(key.encode()).digest() if key else None
        if os.getenv("HERMES_ENVIRONMENT", "").lower() == "production" and self._fernet is None:
            raise RuntimeError("HERMES_ENCRYPTION_KEY is required in production")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'conversations'"
            ).fetchone()
            if existing:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)")}
                if "business_id" not in columns:
                    conn.execute(
                        "ALTER TABLE conversations ADD COLUMN business_id TEXT NOT NULL DEFAULT 'legacy'"
                    )
                    conn.execute(
                        "UPDATE conversations SET business_id = ? WHERE business_id = 'legacy'",
                        (legacy_business_id,),
                    )
                    for row in conn.execute("SELECT id, data FROM conversations").fetchall():
                        data = json.loads(row["data"])
                        data["business_id"] = legacy_business_id
                        conn.execute(
                            "UPDATE conversations SET data = ? WHERE id = ?",
                            (json.dumps(data), row["id"]),
                        )
            conn.executescript(_SCHEMA)
            if self._fernet:
                for row in conn.execute(
                    "SELECT id, customer_phone, data FROM conversations"
                ).fetchall():
                    if row["data"].startswith("enc:"):
                        continue
                    payload = json.loads(row["data"])
                    phone = payload.get("customer_phone", row["customer_phone"] or "")
                    conn.execute(
                        "UPDATE conversations SET customer_phone = ?, data = ? WHERE id = ?",
                        (
                            self._phone_index(phone),
                            f"enc:{self._fernet.encrypt(row['data'].encode()).decode()}",
                            row["id"],
                        ),
                    )

    def _phone_index(self, phone: str) -> str:
        if self._fernet is None:
            return phone
        assert self._phone_lookup_key is not None
        return hmac.new(self._phone_lookup_key, phone.encode(), hashlib.sha256).hexdigest()

    def _decode_data(self, value: str) -> dict:
        if value.startswith("enc:"):
            if self._fernet is None:
                raise RuntimeError("HERMES_ENCRYPTION_KEY is required to read encrypted conversations")
            value = self._fernet.decrypt(value[4:].encode()).decode()
        return json.loads(value)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, conv: Conversation) -> None:
        import time

        order_status = conv.order.status.value if conv.order else ""
        order_total = conv.order.total if conv.order else 0.0
        checkout_id = conv.payment.checkout_request_id if conv.payment else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations
                    (id, business_id, customer_phone, channel, needs_human, order_status, order_total,
                     mpesa_checkout_id, created_at, updated_at, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    business_id=excluded.business_id,
                    customer_phone=excluded.customer_phone,
                    channel=excluded.channel,
                    needs_human=excluded.needs_human,
                    order_status=excluded.order_status,
                    order_total=excluded.order_total,
                    mpesa_checkout_id=excluded.mpesa_checkout_id,
                    updated_at=excluded.updated_at,
                    data=excluded.data
                WHERE conversations.business_id = excluded.business_id
                """,
                (
                    conv.id,
                    conv.business_id,
                    self._phone_index(conv.customer_phone),
                    conv.channel,
                    int(conv.needs_human),
                    order_status,
                    order_total,
                    checkout_id,
                    conv.created_at,
                    time.time(),
                    _conversation_to_json(conv, self._fernet),
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] == 0:
                raise ValueError("Conversation ID already belongs to another business")

    def get(
        self,
        conversation_id: str,
        *,
        business_id: str,
        customer_phone: str | None = None,
    ) -> Optional[Conversation]:
        conditions = ["id = ?", "business_id = ?"]
        params: list[str] = [conversation_id, business_id]
        if customer_phone is not None:
            conditions.append("customer_phone = ?")
            params.append(self._phone_index(customer_phone))
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT data FROM conversations WHERE {' AND '.join(conditions)}",
                params,
            ).fetchone()
        return _conversation_from_dict(self._decode_data(row["data"])) if row else None

    def get_by_checkout(self, checkout_request_id: str) -> Optional[Conversation]:
        """Find the conversation whose pending payment matches a Daraja
        CheckoutRequestID -- used by the M-Pesa callback handler."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM conversations WHERE mpesa_checkout_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (checkout_request_id,),
            ).fetchone()
        return _conversation_from_dict(self._decode_data(row["data"])) if row else None

    def get_latest_by_phone(self, phone: str, *, business_id: str) -> Optional[Conversation]:
        """Most recent conversation for a phone number -- used to continue a
        WhatsApp thread across messages within one business."""
        query = (
            "SELECT data FROM conversations WHERE customer_phone = ? AND business_id = ? "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        params = (self._phone_index(phone), business_id)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return _conversation_from_dict(self._decode_data(row["data"])) if row else None

    def list_conversations(self, *, business_id: str, limit: int = 500) -> list[Conversation]:
        query = (
            "SELECT data FROM conversations WHERE business_id = ? "
            "ORDER BY updated_at DESC LIMIT ?"
        )
        params = (business_id, limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_conversation_from_dict(self._decode_data(r["data"])) for r in rows]

    def metrics(self, *, business_id: str) -> dict:
        """Aggregate the numbers the owner dashboard reports."""
        convs = self.list_conversations(limit=100000, business_id=business_id)
        total = len(convs)
        escalations = sum(1 for c in convs if c.needs_human)
        deflected = total - escalations
        with_orders = [c for c in convs if c.order and c.order.items]
        paid = [c for c in with_orders if c.order.status == OrderStatus.PAID]
        revenue = sum(c.order.total for c in paid)
        return {
            "total_conversations": total,
            "deflected": deflected,
            "escalations": escalations,
            "deflection_rate": (deflected / total) if total else 0.0,
            "orders_captured": len(with_orders),
            "orders_paid": len(paid),
            "revenue_collected": revenue,
        }
