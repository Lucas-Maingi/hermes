"""Payment tools (mpesa_stk_push, check_payment) and the full tool registry.

Kept separate from tools.py so the catalog/knowledge tools carry no dependency
on the M-Pesa client. These operate on ``ctx.mpesa`` (any MpesaClient) and
manage Payment/Order state on the conversation.
"""

from __future__ import annotations

import time

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
    phone = normalize_phone(conv.customer_phone)
    amount = conv.order.total
    if amount <= 0:
        return "This order total can't be charged. Please let the shop team help."
    platform = ctx.extra.get("platform")
    if platform:
        try:
            platform.reserve_order_inventory(
                ctx.business.id,
                conv.order.id,
                [{"name": item.name, "quantity": item.quantity} for item in conv.order.items],
                expires_at=time.time() + 20 * 60,
            )
        except ValueError as exc:
            return f"I can't start payment because {exc}. Let me connect you with the shop team."

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

    if status == PaymentStatus.SUCCESS:
        conv.needs_human = True
        return (
            "M-Pesa reports that the payment succeeded. I am waiting for the payment "
            "receipt callback to verify the amount and payer before confirming your order; "
            "the shop team has been notified."
        )
    if status == PaymentStatus.PENDING:
        return "I haven't seen the payment yet. Please enter your M-Pesa PIN on the prompt."
    conv.payment.status = status
    if status == PaymentStatus.TIMEOUT:
        if conv.order is not None and ctx.extra.get("platform"):
            ctx.extra["platform"].release_order_inventory(ctx.business.id, conv.order.id)
        return "The M-Pesa prompt timed out. Shall I send it again?"
    if status == PaymentStatus.FAILED and conv.order is not None and ctx.extra.get("platform"):
        ctx.extra["platform"].release_order_inventory(ctx.business.id, conv.order.id)
    return "The payment didn't go through. Would you like me to resend the prompt?"


def build_tool_registry() -> dict[str, Tool]:
    """The complete toolset the orchestrator exposes: catalog tools + payments."""
    return {
        **BASE_TOOLS,
        "mpesa_stk_push": mpesa_stk_push,
        "check_payment": check_payment,
    }
