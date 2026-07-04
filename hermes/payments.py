"""Payment tools (mpesa_stk_push, check_payment) and the full tool registry.

Kept separate from tools.py so the catalog/knowledge tools carry no dependency
on the M-Pesa client. These operate on ``ctx.mpesa`` (any MpesaClient) and
manage Payment/Order state on the conversation.
"""

from __future__ import annotations

from hermes.models import OrderStatus, Payment, PaymentStatus
from hermes.mpesa.base import normalize_phone
from hermes.tools import BASE_TOOLS, Tool, ToolContext, confirm_order_ready_for_payment


def mpesa_stk_push(args: dict, ctx: ToolContext) -> str:
    ok, msg = confirm_order_ready_for_payment(ctx)
    if not ok:
        return msg
    if ctx.mpesa is None:
        return "Payment isn't set up for this shop yet - let me get someone to help."

    conv = ctx.conversation
    phone = normalize_phone(str(args.get("phone") or conv.customer_phone))
    amount = conv.order.total

    result = ctx.mpesa.stk_push(
        phone=phone,
        amount=amount,
        account_reference=conv.order.id,
        description=f"Pay {ctx.business.name}",
    )
    if not result.success:
        return "Sorry, I couldn't start the M-Pesa payment just now. Shall I try again?"

    conv.payment = Payment(
        order_id=conv.order.id,
        phone=phone,
        amount=amount,
        checkout_request_id=result.checkout_request_id,
        status=PaymentStatus.PENDING,
    )
    conv.order.status = OrderStatus.AWAITING_PAYMENT
    cur = ctx.business.currency
    return (
        f"I've sent an M-Pesa request for {cur} {amount:,.0f} to {phone}. "
        "Please enter your M-Pesa PIN on the prompt to complete payment."
    )


def check_payment(args: dict, ctx: ToolContext) -> str:
    conv = ctx.conversation
    if conv.payment is None:
        return "There's no pending payment right now."
    if ctx.mpesa is None:
        return "Payment isn't set up for this shop yet."

    status = ctx.mpesa.query_status(conv.payment.checkout_request_id)
    conv.payment.status = status

    if status == PaymentStatus.SUCCESS:
        if conv.order is not None:
            conv.order.status = OrderStatus.PAID
        return (
            f"Payment received - thank you! Your order {conv.order.id if conv.order else ''} "
            "is confirmed and we'll get it ready."
        )
    if status == PaymentStatus.PENDING:
        return "I haven't seen the payment yet. Please enter your M-Pesa PIN on the prompt."
    if status == PaymentStatus.TIMEOUT:
        return "The M-Pesa prompt timed out. Shall I send it again?"
    return "The payment didn't go through. Would you like me to resend the prompt?"


def build_tool_registry() -> dict[str, Tool]:
    """The complete toolset the orchestrator exposes: catalog tools + payments."""
    return {
        **BASE_TOOLS,
        "mpesa_stk_push": mpesa_stk_push,
        "check_payment": check_payment,
    }
