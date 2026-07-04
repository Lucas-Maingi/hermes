"""SQLite persistence for conversations, and the metrics the owner dashboard
reports (deflection rate, orders, revenue, escalations).

Conversations serialize as JSON (str-enums make this clean). Kept single-file
and dependency-light for portfolio scale; a multi-tenant production deployment
would move to Postgres -- see the README's Known Limitations.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Union

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
    customer_phone TEXT,
    channel TEXT,
    needs_human INTEGER,
    order_status TEXT,
    order_total REAL,
    created_at REAL,
    updated_at REAL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_created ON conversations(created_at);
"""


def _conversation_to_json(conv: Conversation) -> str:
    return json.dumps(asdict(conv))


def _conversation_from_dict(d: dict) -> Conversation:
    conv = Conversation(
        id=d["id"],
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
    def __init__(self, db_path: Union[str, Path] = "hermes.db"):
        self.db_path = str(db_path)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, conv: Conversation) -> None:
        import time

        order_status = conv.order.status.value if conv.order else ""
        order_total = conv.order.total if conv.order else 0.0
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations
                    (id, customer_phone, channel, needs_human, order_status, order_total,
                     created_at, updated_at, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    customer_phone=excluded.customer_phone,
                    channel=excluded.channel,
                    needs_human=excluded.needs_human,
                    order_status=excluded.order_status,
                    order_total=excluded.order_total,
                    updated_at=excluded.updated_at,
                    data=excluded.data
                """,
                (
                    conv.id,
                    conv.customer_phone,
                    conv.channel,
                    int(conv.needs_human),
                    order_status,
                    order_total,
                    conv.created_at,
                    time.time(),
                    _conversation_to_json(conv),
                ),
            )

    def get(self, conversation_id: str) -> Optional[Conversation]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return _conversation_from_dict(json.loads(row["data"])) if row else None

    def list_conversations(self, limit: int = 500) -> list[Conversation]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_conversation_from_dict(json.loads(r["data"])) for r in rows]

    def metrics(self) -> dict:
        """Aggregate the numbers the owner dashboard reports."""
        convs = self.list_conversations(limit=100000)
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
