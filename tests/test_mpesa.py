import pytest

from hermes.models import Conversation, OrderStatus, PaymentStatus
from hermes.mpesa.base import normalize_phone
from hermes.mpesa.factory import get_mpesa_client
from hermes.mpesa.simulator import MpesaSimulator
from hermes.payments import build_tool_registry, check_payment, mpesa_stk_push
from hermes.sample_business import sample_business
from hermes.tools import ToolContext, capture_order


class TestNormalizePhone:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("0712345678", "254712345678"),
            ("+254712345678", "254712345678"),
            ("254712345678", "254712345678"),
            ("712345678", "254712345678"),
            ("0112345678", "254112345678"),
        ],
    )
    def test_normalizes_kenyan_formats(self, raw, expected):
        assert normalize_phone(raw) == expected


class TestSimulator:
    def test_stk_push_returns_accepted_with_checkout_id(self):
        sim = MpesaSimulator()
        res = sim.stk_push(phone="0712345678", amount=710, account_reference="ord_1", description="Pay")
        assert res.success
        assert res.checkout_request_id

    def test_push_is_pending_until_completed(self):
        sim = MpesaSimulator()
        res = sim.stk_push(phone="0712345678", amount=710, account_reference="o", description="d")
        assert sim.query_status(res.checkout_request_id) == PaymentStatus.PENDING
        sim.complete_payment(res.checkout_request_id, success=True)
        assert sim.query_status(res.checkout_request_id) == PaymentStatus.SUCCESS

    def test_auto_complete_succeeds_immediately(self):
        sim = MpesaSimulator(auto_complete=True)
        res = sim.stk_push(phone="0712345678", amount=100, account_reference="o", description="d")
        assert sim.query_status(res.checkout_request_id) == PaymentStatus.SUCCESS

    def test_rejects_non_positive_amount(self):
        sim = MpesaSimulator()
        assert sim.stk_push(phone="0712", amount=0, account_reference="o", description="d").success is False

    def test_unknown_checkout_id_is_failed(self):
        assert MpesaSimulator().query_status("nope") == PaymentStatus.FAILED

    def test_success_callback_matches_daraja_shape(self):
        sim = MpesaSimulator()
        res = sim.stk_push(phone="0712345678", amount=710, account_reference="o", description="d")
        callback = sim.complete_payment(res.checkout_request_id, success=True)
        stk = callback["Body"]["stkCallback"]
        assert stk["ResultCode"] == 0
        assert stk["CheckoutRequestID"] == res.checkout_request_id
        items = {i["Name"]: i["Value"] for i in stk["CallbackMetadata"]["Item"]}
        assert items["Amount"] == 710
        assert "MpesaReceiptNumber" in items

    def test_failed_callback_has_nonzero_result_code(self):
        sim = MpesaSimulator()
        res = sim.stk_push(phone="0712345678", amount=710, account_reference="o", description="d")
        callback = sim.complete_payment(res.checkout_request_id, success=False)
        assert callback["Body"]["stkCallback"]["ResultCode"] != 0


class TestFactory:
    def test_returns_simulator_without_credentials(self, monkeypatch):
        for k in ["MPESA_CONSUMER_KEY", "MPESA_CONSUMER_SECRET", "MPESA_SHORTCODE", "MPESA_PASSKEY", "MPESA_CALLBACK_URL"]:
            monkeypatch.delenv(k, raising=False)
        assert isinstance(get_mpesa_client(), MpesaSimulator)

    def test_returns_daraja_when_fully_configured(self, monkeypatch):
        for k, v in {
            "MPESA_CONSUMER_KEY": "ck",
            "MPESA_CONSUMER_SECRET": "cs",
            "MPESA_SHORTCODE": "174379",
            "MPESA_PASSKEY": "pk",
            "MPESA_CALLBACK_URL": "https://example.test/mpesa/callback",
        }.items():
            monkeypatch.setenv(k, v)
        client = get_mpesa_client()
        assert type(client).__name__ == "DarajaClient"


class TestPaymentTools:
    def make_ctx_with_order(self, mpesa=None):
        biz = sample_business()
        conv = Conversation(customer_phone="254712345678")
        ctx = ToolContext(conversation=conv, business=biz, mpesa=mpesa or MpesaSimulator())
        capture_order({"item": "maize flour", "quantity": 2}, ctx)  # 360
        return ctx, conv

    def test_stk_push_requires_an_order(self):
        biz = sample_business()
        conv = Conversation(customer_phone="254712345678")
        ctx = ToolContext(conversation=conv, business=biz, mpesa=MpesaSimulator())
        out = mpesa_stk_push({}, ctx)
        assert "don't have anything" in out.lower()

    def test_stk_push_creates_pending_payment_and_awaiting_order(self):
        ctx, conv = self.make_ctx_with_order()
        out = mpesa_stk_push({}, ctx)
        assert conv.payment is not None
        assert conv.payment.status == PaymentStatus.PENDING
        assert conv.order.status == OrderStatus.AWAITING_PAYMENT
        assert "360" in out

    def test_check_payment_confirms_once_completed(self):
        sim = MpesaSimulator()
        ctx, conv = self.make_ctx_with_order(mpesa=sim)
        mpesa_stk_push({}, ctx)
        # still pending
        assert "haven't seen" in check_payment({}, ctx).lower()
        # customer pays
        sim.complete_payment(conv.payment.checkout_request_id, success=True)
        out = check_payment({}, ctx)
        assert conv.order.status == OrderStatus.PAID
        assert "received" in out.lower()

    def test_check_payment_without_pending_payment(self):
        biz = sample_business()
        conv = Conversation()
        ctx = ToolContext(conversation=conv, business=biz, mpesa=MpesaSimulator())
        assert "no pending payment" in check_payment({}, ctx).lower()


class TestRegistry:
    def test_full_registry_has_all_five_tools(self):
        assert set(build_tool_registry()) == {
            "lookup_knowledge",
            "capture_order",
            "escalate_to_human",
            "mpesa_stk_push",
            "check_payment",
        }
