"""Core domain models for the Hermes commerce/support agent.

Kept provider- and channel-agnostic: nothing here knows whether a message
arrived over WhatsApp or the web simulator, or whether a payment was collected
via real M-Pesa Daraja or the simulator. Those concerns live in adapters.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Role(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    SYSTEM = "system"
    # A message the agent produced that signals a human should take over.
    HANDOFF = "handoff"


class OrderStatus(str, Enum):
    DRAFT = "draft"            # being assembled during the conversation
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    CANCELLED = "cancelled"


class PaymentStatus(str, Enum):
    PENDING = "pending"       # STK push sent, awaiting customer PIN
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Message:
    role: Role
    text: str
    timestamp: float = field(default_factory=time.time)
    # Free-form metadata, e.g. which tools the agent invoked for this turn.
    metadata: dict = field(default_factory=dict)


@dataclass
class OrderItem:
    name: str
    quantity: int
    unit_price: float  # in KES

    @property
    def line_total(self) -> float:
        return self.quantity * self.unit_price


@dataclass
class Order:
    id: str = field(default_factory=lambda: _new_id("ord"))
    items: list[OrderItem] = field(default_factory=list)
    status: OrderStatus = OrderStatus.DRAFT
    delivery_note: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def total(self) -> float:
        return sum(item.line_total for item in self.items)

    def add_item(self, name: str, quantity: int, unit_price: float) -> None:
        # Merge with an existing identically-priced line rather than duplicating.
        for item in self.items:
            if item.name.lower() == name.lower() and item.unit_price == unit_price:
                item.quantity += quantity
                return
        self.items.append(OrderItem(name=name, quantity=quantity, unit_price=unit_price))


@dataclass
class Payment:
    id: str = field(default_factory=lambda: _new_id("pay"))
    order_id: str = ""
    phone: str = ""            # payer MSISDN, e.g. 2547XXXXXXXX
    amount: float = 0.0
    status: PaymentStatus = PaymentStatus.PENDING
    # Daraja's CheckoutRequestID, used to correlate the async callback.
    checkout_request_id: Optional[str] = None
    receipt: Optional[str] = None  # M-Pesa receipt number on success
    created_at: float = field(default_factory=time.time)


@dataclass
class Conversation:
    id: str = field(default_factory=lambda: _new_id("conv"))
    customer_phone: str = ""
    channel: str = "simulator"   # "whatsapp" | "simulator"
    messages: list[Message] = field(default_factory=list)
    order: Optional[Order] = None
    needs_human: bool = False
    created_at: float = field(default_factory=time.time)

    def add_message(self, role: Role, text: str, **metadata) -> Message:
        msg = Message(role=role, text=text, metadata=metadata)
        self.messages.append(msg)
        return msg

    @property
    def handled_by_agent(self) -> bool:
        """True if the conversation never required a human handoff — the
        numerator of the deflection-rate metric the owner dashboard reports."""
        return not self.needs_human
