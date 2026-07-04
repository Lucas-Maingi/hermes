"""Seed demo/hermes_demo.db with realistic conversations so the owner dashboard
has something to show on first load (e.g. the hosted demo Space).

Runs the real agent against the M-Pesa simulator -- so the seeded data is
produced by the actual code path, not hand-faked. Illustrative, not real sales.
"""

from __future__ import annotations

from pathlib import Path

from hermes.agent import Agent
from hermes.llm.mock import MockLLM
from hermes.models import Conversation
from hermes.mpesa.simulator import MpesaSimulator
from hermes.sample_business import sample_business
from hermes.store import HermesStore

DB_PATH = Path(__file__).resolve().parent.parent / "demo" / "hermes_demo.db"

# (phone, [messages], pay?, then success?)
SCRIPTS = [
    ("254712000001", ["Habari", "bei ya unga?", "nataka 2 unga na 1 sugar", "nlipe na mpesa"], True, True),
    ("254712000002", ["Hi", "do you have cooking oil?", "I want 3 cooking oil", "pay"], True, True),
    ("254712000003", ["how much is bread and milk?", "2 bread and 2 milk", "lipa"], True, True),
    ("254712000004", ["what time do you open?", "3 rice", "pay with mpesa"], True, False),   # cancelled on phone
    ("254712000005", ["I need to speak to a person about a complaint"], False, False),        # escalation
    ("254712000006", ["do you sell laptops?"], False, False),                                 # off-catalog -> escalation
    ("254712000007", ["niaje", "4 eggs", "1 tea leaves", "nlipe"], True, True),
]


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    store = HermesStore(DB_PATH)
    sim = MpesaSimulator()
    biz = sample_business()

    for phone, messages, pay, success in SCRIPTS:
        agent = Agent(biz, MockLLM(), sim)
        conv = Conversation(customer_phone=phone, channel="whatsapp")
        for text in messages:
            agent.handle_message(conv, text)
        if pay and conv.payment is not None:
            sim.complete_payment(conv.payment.checkout_request_id, success=success)
            agent.handle_message(conv, "have you received the payment?")
        store.save(conv)

    m = store.metrics()
    print(f"Seeded {DB_PATH}")
    print(
        f"  conversations={m['total_conversations']} deflection={m['deflection_rate']:.0%} "
        f"paid={m['orders_paid']} revenue={biz.currency} {m['revenue_collected']:,.0f}"
    )


if __name__ == "__main__":
    main()
