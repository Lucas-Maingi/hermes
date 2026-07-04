from hermes import knowledge
from hermes.models import Conversation, OrderStatus
from hermes.sample_business import sample_business
from hermes.tools import ToolContext, capture_order, escalate_to_human, lookup_knowledge


def make_ctx():
    biz = sample_business()
    conv = Conversation(customer_phone="254712345678")
    return ToolContext(conversation=conv, business=biz), biz, conv


class TestKnowledge:
    def test_price_question_grounded_in_catalog(self):
        biz = sample_business()
        assert "180" in knowledge.answer("how much is maize flour?", biz)

    def test_swahili_alias_resolves(self):
        biz = sample_business()
        assert "180" in knowledge.answer("bei ya unga?", biz)

    def test_out_of_stock_is_reported(self):
        biz = sample_business()
        ans = knowledge.answer("do you have salt?", biz)
        assert "out of stock" in ans.lower()

    def test_hours_question(self):
        biz = sample_business()
        assert "7am" in knowledge.answer("what time do you open?", biz).lower() or \
               "7am" in knowledge.answer("what time do you open?", biz)

    def test_location_question(self):
        biz = sample_business()
        assert "kasarani" in knowledge.answer("where are you located?", biz).lower()

    def test_unknown_topic_returns_none(self):
        biz = sample_business()
        assert knowledge.answer("do you sell airtime for satellites?", biz) is None

    def test_catalog_summary_lists_products(self):
        biz = sample_business()
        summary = knowledge.answer("what do you sell?", biz)
        assert "Maize flour" in summary
        assert "Salt" not in summary  # out of stock excluded


class TestLookupKnowledgeTool:
    def test_grounded_hit_does_not_escalate(self):
        ctx, _, conv = make_ctx()
        out = lookup_knowledge({"query": "price of sugar"}, ctx)
        assert "160" in out
        assert conv.needs_human is False

    def test_miss_escalates_and_says_so(self):
        ctx, _, conv = make_ctx()
        out = lookup_knowledge({"query": "can you fix my car?"}, ctx)
        assert conv.needs_human is True
        assert "team" in out.lower()


class TestCaptureOrderTool:
    def test_adds_catalog_item_with_real_price(self):
        ctx, _, conv = make_ctx()
        out = capture_order({"item": "maize flour", "quantity": 2}, ctx)
        assert conv.order is not None
        assert conv.order.total == 360  # 2 x 180, price from catalog
        assert "360" in out

    def test_resolves_swahili_alias(self):
        ctx, _, conv = make_ctx()
        capture_order({"item": "sukari", "quantity": 1}, ctx)
        assert conv.order.items[0].name == "Sugar"

    def test_unknown_item_is_refused_not_invented(self):
        ctx, _, conv = make_ctx()
        out = capture_order({"item": "playstation 5", "quantity": 1}, ctx)
        assert conv.order is None
        assert "don't stock" in out.lower()

    def test_out_of_stock_item_refused(self):
        ctx, _, conv = make_ctx()
        out = capture_order({"item": "salt", "quantity": 1}, ctx)
        assert "out of stock" in out.lower()

    def test_accumulates_multiple_items(self):
        ctx, _, conv = make_ctx()
        capture_order({"item": "bread", "quantity": 2}, ctx)
        capture_order({"item": "milk", "quantity": 3}, ctx)
        assert conv.order.total == 2 * 70 + 3 * 60

    def test_defaults_quantity_to_one_on_bad_value(self):
        ctx, _, conv = make_ctx()
        capture_order({"item": "bread", "quantity": "lots"}, ctx)
        assert conv.order.items[0].quantity == 1


class TestEscalateTool:
    def test_sets_needs_human_and_confirms(self):
        ctx, _, conv = make_ctx()
        out = escalate_to_human({"reason": "angry customer"}, ctx)
        assert conv.needs_human is True
        assert "connecting you" in out.lower()
        assert conv.order is None or conv.order.status != OrderStatus.PAID
