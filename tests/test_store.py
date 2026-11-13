import json
import sqlite3

import pytest
from cryptography.fernet import Fernet

from hermes.agent import Agent
from hermes.llm.mock import MockLLM
from hermes.models import Conversation, OrderStatus, PaymentStatus, Role
from hermes.mpesa.simulator import MpesaSimulator
from hermes.sample_business import sample_business
from hermes.store import HermesStore


def store(tmp_path):
    return HermesStore(tmp_path / "hermes.db")


class TestRoundTrip:
    def test_save_and_get_preserves_conversation(self, tmp_path):
        s = store(tmp_path)
        agent = Agent(sample_business(), MockLLM(), MpesaSimulator())
        conv = Conversation(customer_phone="254712345678")
        agent.handle_message(conv, "I want 2 maize flour")
        s.save(conv)

        loaded = s.get(conv.id, business_id=conv.business_id)
        assert loaded is not None
        assert loaded.customer_phone == "254712345678"
        assert loaded.order is not None
        assert loaded.order.total == 360
        assert len(loaded.messages) == len(conv.messages)

    def test_encrypted_phone_lookup_and_conversation_detail(self, tmp_path):
        key = Fernet.generate_key().decode()
        s = HermesStore(tmp_path / "encrypted.db", encryption_key=key)
        conv = Conversation(business_id="shop_a", customer_phone="254712345678")
        conv.add_message(Role.CUSTOMER, "Where is my order?")
        s.save(conv)

        assert s.get(conv.id, business_id="shop_a", customer_phone="254712345678") is not None
        assert s.get(conv.id, business_id="shop_a", customer_phone="254799999999") is None
        with s._connect() as connection:
            row = connection.execute(
                "SELECT customer_phone, data FROM conversations WHERE id=?",
                (conv.id,),
            ).fetchone()
        assert "254712345678" not in row["customer_phone"]
        assert row["data"].startswith("enc:")

    def test_save_is_idempotent_upsert(self, tmp_path):
        s = store(tmp_path)
        conv = Conversation(customer_phone="254700000000")
        s.save(conv)
        conv.needs_human = True
        s.save(conv)
        assert len(s.list_conversations(business_id=conv.business_id)) == 1
        assert s.get(conv.id, business_id=conv.business_id).needs_human is True

    def test_payment_state_round_trips(self, tmp_path):
        s = store(tmp_path)
        sim = MpesaSimulator()
        agent = Agent(sample_business(), MockLLM(), sim)
        conv = Conversation(customer_phone="254712345678")
        agent.handle_message(conv, "2 bread")
        agent.handle_message(conv, "pay")
        s.save(conv)
        loaded = s.get(conv.id, business_id=conv.business_id)
        assert loaded.payment is not None
        assert loaded.payment.checkout_request_id == conv.payment.checkout_request_id

    def test_phone_history_and_metrics_are_scoped_by_business(self, tmp_path):
        s = store(tmp_path)
        conv_a = Conversation(business_id="shop_a", customer_phone="254712345678")
        conv_b = Conversation(business_id="shop_b", customer_phone="254712345678")
        s.save(conv_a)
        s.save(conv_b)

        assert s.get_latest_by_phone("254712345678", business_id="shop_a").id == conv_a.id
        assert s.get_latest_by_phone("254712345678", business_id="shop_b").id == conv_b.id
        assert s.get(conv_a.id, business_id="shop_b") is None
        assert [c.business_id for c in s.list_conversations(business_id="shop_a")] == ["shop_a"]
        assert s.metrics(business_id="shop_a")["total_conversations"] == 1

    def test_cannot_reassign_existing_conversation_to_another_business(self, tmp_path):
        s = store(tmp_path)
        conv = Conversation(business_id="shop_a")
        s.save(conv)
        conv.business_id = "shop_b"

        with pytest.raises(ValueError, match="another business"):
            s.save(conv)
        assert s.get(conv.id, business_id="shop_a") is not None
        assert s.get(conv.id, business_id="shop_b") is None

    def test_existing_database_is_migrated_to_default_business(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        legacy_data = {
            "id": "conv_legacy",
            "customer_phone": "254700000000",
            "channel": "whatsapp",
            "messages": [],
            "order": None,
            "payment": None,
            "needs_human": False,
            "created_at": 1.0,
        }
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY, customer_phone TEXT, channel TEXT,
                    needs_human INTEGER, order_status TEXT, order_total REAL,
                    mpesa_checkout_id TEXT, created_at REAL, updated_at REAL, data TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("conv_legacy", "254700000000", "whatsapp", 0, "", 0, None, 1, 1, json.dumps(legacy_data)),
            )

        migrated = HermesStore(db_path, legacy_business_id="shop_a")
        conv = migrated.get_latest_by_phone("254700000000", business_id="shop_a")
        assert conv is not None
        assert conv.business_id == "shop_a"


class TestMetrics:
    def _paid_conversation(self, sim):
        agent = Agent(sample_business(), MockLLM(), sim)
        conv = Conversation(customer_phone="254712345678")
        agent.handle_message(conv, "2 maize flour")
        agent.handle_message(conv, "pay")
        sim.complete_payment(conv.payment.checkout_request_id, success=True)
        conv.payment.status = PaymentStatus.SUCCESS
        conv.order.status = OrderStatus.PAID
        return conv

    def test_deflection_and_revenue(self, tmp_path):
        s = store(tmp_path)
        sim = MpesaSimulator()

        # A paid, agent-handled conversation.
        s.save(self._paid_conversation(sim))

        # An escalated conversation (needs human).
        agent = Agent(sample_business(), MockLLM(), sim)
        conv2 = Conversation(customer_phone="254700000001")
        agent.handle_message(conv2, "I need to speak to a person")
        s.save(conv2)

        m = s.metrics(business_id="duka_demo")
        assert m["total_conversations"] == 2
        assert m["escalations"] == 1
        assert m["deflected"] == 1
        assert m["deflection_rate"] == 0.5
        assert m["orders_paid"] == 1
        assert m["revenue_collected"] == 360

    def test_metrics_empty_store(self, tmp_path):
        m = store(tmp_path).metrics(business_id="duka_demo")
        assert m["total_conversations"] == 0
        assert m["deflection_rate"] == 0.0
