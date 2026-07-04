from hermes.agent import Agent, build_agent
from hermes.llm.mock import MockLLM
from hermes.models import Conversation, OrderStatus, PaymentStatus
from hermes.mpesa.simulator import MpesaSimulator
from hermes.sample_business import sample_business


def make_agent(mpesa=None):
    return Agent(sample_business(), MockLLM(), mpesa or MpesaSimulator())


class TestAgentConversation:
    def test_greeting_gets_a_reply(self):
        agent = make_agent()
        conv = Conversation(customer_phone="254712345678")
        reply = agent.handle_message(conv, "Hi")
        assert reply
        assert conv.messages[-1].text == reply

    def test_price_question_answered_from_catalog(self):
        agent = make_agent()
        conv = Conversation(customer_phone="254712345678")
        reply = agent.handle_message(conv, "How much is maize flour?")
        assert "180" in reply

    def test_ordering_builds_the_order(self):
        agent = make_agent()
        conv = Conversation(customer_phone="254712345678")
        agent.handle_message(conv, "I want 2 maize flour and 1 cooking oil")
        assert conv.order is not None
        assert conv.order.total == 2 * 180 + 350

    def test_unknown_question_triggers_handoff(self):
        agent = make_agent()
        conv = Conversation(customer_phone="254712345678")
        agent.handle_message(conv, "Can you repair my laptop?")
        assert conv.needs_human is True

    def test_full_order_to_payment_flow(self):
        sim = MpesaSimulator()
        agent = make_agent(mpesa=sim)
        conv = Conversation(customer_phone="254712345678")

        agent.handle_message(conv, "I want 2 maize flour")
        assert conv.order.total == 360

        pay_reply = agent.handle_message(conv, "let me pay with mpesa")
        assert conv.payment is not None
        assert conv.order.status == OrderStatus.AWAITING_PAYMENT
        assert "360" in pay_reply

        # Customer enters PIN (simulated), then agent confirms.
        sim.complete_payment(conv.payment.checkout_request_id, success=True)
        confirm = agent.handle_message(conv, "have you received it?")
        # 'received' comes via check_payment; the mock may answer generally, so
        # assert the authoritative state instead of exact wording.
        assert conv.payment.status == PaymentStatus.SUCCESS or "received" in confirm.lower()

    def test_tools_used_recorded_in_message_metadata(self):
        agent = make_agent()
        conv = Conversation(customer_phone="254712345678")
        agent.handle_message(conv, "How much is sugar?")
        assert "lookup_knowledge" in conv.messages[-1].metadata.get("tools_used", [])

    def test_handle_message_never_raises_on_gibberish(self):
        agent = make_agent()
        conv = Conversation(customer_phone="254712345678")
        reply = agent.handle_message(conv, "asdkjfh qwe zzz")
        assert isinstance(reply, str) and reply


class TestBuildAgent:
    def test_build_agent_uses_simulators_by_default(self, monkeypatch):
        for k in ["GROQ_API_KEY", "OPENAI_API_KEY", "MPESA_CONSUMER_KEY"]:
            monkeypatch.delenv(k, raising=False)
        agent = build_agent(sample_business())
        assert type(agent.llm).__name__ == "MockLLM"
        assert type(agent.mpesa).__name__ == "MpesaSimulator"
