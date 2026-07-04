from hermes.models import (
    Conversation,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Role,
)


class TestOrder:
    def test_total_sums_line_items(self):
        order = Order()
        order.add_item("Maize flour 2kg", 2, 180.0)
        order.add_item("Cooking oil 1L", 1, 350.0)
        assert order.total == 2 * 180.0 + 350.0

    def test_add_item_merges_same_named_same_price_line(self):
        order = Order()
        order.add_item("Maize flour 2kg", 2, 180.0)
        order.add_item("maize flour 2kg", 3, 180.0)  # case-insensitive merge
        assert len(order.items) == 1
        assert order.items[0].quantity == 5

    def test_add_item_keeps_separate_lines_when_price_differs(self):
        order = Order()
        order.add_item("Delivery", 1, 100.0)
        order.add_item("Delivery", 1, 150.0)
        assert len(order.items) == 2

    def test_new_order_is_draft(self):
        assert Order().status == OrderStatus.DRAFT

    def test_ids_are_unique(self):
        assert Order().id != Order().id


class TestPayment:
    def test_defaults_to_pending(self):
        assert Payment().status == PaymentStatus.PENDING

    def test_carries_order_link_and_amount(self):
        pay = Payment(order_id="ord_1", phone="254712345678", amount=710.0)
        assert pay.order_id == "ord_1"
        assert pay.amount == 710.0


class TestConversation:
    def test_add_message_appends_in_order(self):
        conv = Conversation(customer_phone="254712345678")
        conv.add_message(Role.CUSTOMER, "Do you have maize flour?")
        conv.add_message(Role.AGENT, "Yes, KES 180 for 2kg.")
        assert [m.role for m in conv.messages] == [Role.CUSTOMER, Role.AGENT]
        assert conv.messages[0].text == "Do you have maize flour?"

    def test_handled_by_agent_true_until_handoff(self):
        conv = Conversation()
        assert conv.handled_by_agent is True
        conv.needs_human = True
        assert conv.handled_by_agent is False

    def test_message_metadata_is_captured(self):
        conv = Conversation()
        msg = conv.add_message(Role.AGENT, "Order placed", tools_used=["capture_order"])
        assert msg.metadata["tools_used"] == ["capture_order"]
