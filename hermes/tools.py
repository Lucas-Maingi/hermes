"""Tool implementations the agent can call.

Each tool takes ``(arguments, ToolContext)`` and returns a customer-ready
string. Tools mutate conversation/order state on the context. Payment tools
(mpesa_stk_push, check_payment) are registered in the payments module so this
module has no dependency on the M-Pesa client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from hermes import knowledge
from hermes.business import Business
from hermes.models import Conversation, Order, OrderStatus

# A tool: (arguments, context) -> customer-ready reply string.
Tool = Callable[[dict, "ToolContext"], str]


@dataclass
class ToolContext:
    conversation: Conversation
    business: Business
    # Set by the orchestrator once payment tools are wired (M4/M5).
    mpesa: Any = None
    extra: dict = field(default_factory=dict)


def lookup_knowledge(args: dict, ctx: ToolContext) -> str:
    query = str(args.get("query", "")).strip()
    result = knowledge.answer(query, ctx.business)
    if result is None:
        # Honest miss -> flag for handoff; the agent will relay this.
        ctx.conversation.needs_human = True
        return (
            "I'm not sure about that one - let me connect you with a member of "
            f"the {ctx.business.name} team who can help."
        )
    return result


def capture_order(args: dict, ctx: ToolContext) -> str:
    item_text = str(args.get("item", "")).strip()
    try:
        quantity = int(args.get("quantity", 1))
    except (TypeError, ValueError):
        quantity = 1
    quantity = max(1, quantity)

    product = ctx.business.find_product(item_text)
    if product is None:
        return (
            f"Sorry, we don't stock \"{item_text}\". "
            "Would you like to see what we have?"
        )
    if not product.in_stock:
        return f"Sorry, {product.name} is out of stock right now."

    conv = ctx.conversation
    if conv.order is None:
        conv.order = Order()
    conv.order.add_item(product.name, quantity, product.price)

    cur = ctx.business.currency
    line = f"{quantity} x {product.name} @ {cur} {product.price:,.0f}"
    return (
        f"Added {line}.\n"
        f"Your order total is now {cur} {conv.order.total:,.0f}. "
        "Anything else, or shall I send the M-Pesa payment prompt?"
    )


def escalate_to_human(args: dict, ctx: ToolContext) -> str:
    ctx.conversation.needs_human = True
    reason = str(args.get("reason", "")).strip()
    note = f" ({reason})" if reason else ""
    return (
        f"I'm connecting you with the {ctx.business.name} team now{note}. "
        "Someone will reply here shortly."
    )


def confirm_order_ready_for_payment(ctx: ToolContext) -> tuple[bool, str]:
    """Shared precondition check used by the payment tools (registered later)."""
    conv = ctx.conversation
    if conv.order is None or not conv.order.items:
        return False, "You don't have anything in your order yet. What would you like?"
    if conv.order.status == OrderStatus.PAID:
        return False, "This order is already paid. Thank you!"
    return True, ""


# The always-available, payment-free tools. The payments module adds
# mpesa_stk_push and check_payment to a copy of this registry.
BASE_TOOLS: dict[str, Tool] = {
    "lookup_knowledge": lookup_knowledge,
    "capture_order": capture_order,
    "escalate_to_human": escalate_to_human,
}
