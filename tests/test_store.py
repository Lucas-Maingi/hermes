from hermes.agent import Agent
from hermes.llm.mock import MockLLM
from hermes.models import Conversation
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

        loaded = s.get(conv.id)
        assert loaded is not None
        assert loaded.customer_phone == "254712345678"
        assert loaded.order is not None
        assert loaded.order.total == 360
        assert len(loaded.messages) == len(conv.messages)

    def test_save_is_idempotent_upsert(self, tmp_path):
        s = store(tmp_path)
        conv = Conversation(customer_phone="254700000000")
        s.save(conv)
        conv.needs_human = True
        s.save(conv)
        assert len(s.list_conversations()) == 1
        assert s.get(conv.id).needs_human is True

    def test_payment_state_round_trips(self, tmp_path):
        s = store(tmp_path)
        sim = MpesaSimulator()
        agent = Agent(sample_business(), MockLLM(), sim)
        conv = Conversation(customer_phone="254712345678")
        agent.handle_message(conv, "2 bread")
        agent.handle_message(conv, "pay")
        s.save(conv)
        loaded = s.get(conv.id)
        assert loaded.payment is not None
        assert loaded.payment.checkout_request_id == conv.payment.checkout_request_id


class TestMetrics:
    def _paid_conversation(self, sim):
        agent = Agent(sample_business(), MockLLM(), sim)
        conv = Conversation(customer_phone="254712345678")
        agent.handle_message(conv, "2 maize flour")
        agent.handle_message(conv, "pay")
        sim.complete_payment(conv.payment.checkout_request_id, success=True)
        agent.handle_message(conv, "have you received the payment?")
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

        m = s.metrics()
        assert m["total_conversations"] == 2
        assert m["escalations"] == 1
        assert m["deflected"] == 1
        assert m["deflection_rate"] == 0.5
        assert m["orders_paid"] == 1
        assert m["revenue_collected"] == 360

    def test_metrics_empty_store(self, tmp_path):
        m = store(tmp_path).metrics()
        assert m["total_conversations"] == 0
        assert m["deflection_rate"] == 0.0
