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
    if result is None and ctx.extra.get("retriever"):
        passages = ctx.extra["retriever"].search(ctx.business.id, query, limit=3)
        if passages:
            result = "\n".join(
                f"From {passage['title']}: {passage['text']}" for passage in passages
            )
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
    order = conv.order or Order()
    existing_quantity = sum(
        line.quantity for line in order.items if line.name.casefold() == product.name.casefold()
    )
    if product.stock_quantity is not None and existing_quantity + quantity > product.stock_quantity:
        return (
            f"Sorry, we only have {max(0, product.stock_quantity - existing_quantity)} "
            f"{product.name} available right now. "
            "Would you like to adjust the quantity?"
        )

    planned_items = [
        {"name": line.name, "quantity": line.quantity}
        for line in order.items
    ]
    planned_items.append({"name": product.name, "quantity": quantity})
    platform = ctx.extra.get("platform")
    if platform:
        try:
            platform.reserve_order_inventory(ctx.business.id, order.id, planned_items)
        except ValueError as exc:
            return f"Sorry, {exc}. Would you like to adjust the quantity?"
    if conv.order is None:
        conv.order = order
    conv.order.add_item(product.name, quantity, product.price)

    cur = ctx.business.currency
    line = f"{quantity} x {product.name} @ {cur} {product.price:,.0f}"
    return (
        f"Added {line}.\n"
        f"Your order total is now {cur} {conv.order.total:,.0f}. "
        "Anything else, or shall I send the M-Pesa payment prompt?"
    )


def escalate_to_human(args: dict, ctx: ToolContext) -> str:
    del args
    ctx.conversation.needs_human = True
    return (
        f"I've flagged your request for the {ctx.business.name} team. "
        "They can review it in the staff inbox; response times depend on their availability."
    )


def set_delivery_note(args: dict, ctx: ToolContext) -> str:
    if ctx.conversation.order is None or not ctx.conversation.order.items:
        return "There isn't an order yet. Tell me what you would like to order first."
    note = str(args.get("note", "")).strip()
    if not note or len(note) > 500:
        return "Please provide a delivery note of 1 to 500 characters."
    ctx.conversation.order.delivery_note = note
    return "I've added that delivery instruction to your order."


def remove_order_item(args: dict, ctx: ToolContext) -> str:
    order = ctx.conversation.order
    if order is None or not order.items:
        return "There isn't an order to update."
    if order.status in {OrderStatus.AWAITING_PAYMENT, OrderStatus.PAID}:
        return "This order can't be edited while payment is pending or after payment. Let me connect you with the shop team."
    product = ctx.business.find_product(str(args.get("item", "")))
    if product is None:
        return "I couldn't match that item in your order. Please tell me its product name."
    item = next((line for line in order.items if line.name.lower() == product.name.lower()), None)
    if item is None:
        return f"{product.name} isn't in this order."
    try:
        quantity = int(args.get("quantity", item.quantity))
    except (TypeError, ValueError):
        return "Please provide a valid quantity."
    if quantity <= 0 or quantity > item.quantity:
        return f"Please choose a quantity from 1 to {item.quantity}."
    item.quantity -= quantity
    if item.quantity == 0:
        order.items.remove(item)
    if ctx.extra.get("platform"):
        ctx.extra["platform"].reserve_order_inventory(
            ctx.business.id,
            order.id,
            [{"name": line.name, "quantity": line.quantity} for line in order.items],
        )
    if not order.items:
        ctx.conversation.order = None
        return f"Removed {product.name}. Your order is now empty."
    return f"Updated your order. The new total is {ctx.business.currency} {order.total:,.0f}."


def cancel_order(args: dict, ctx: ToolContext) -> str:
    del args
    order = ctx.conversation.order
    if order is None:
        return "There isn't an order to cancel."
    if order.status == OrderStatus.PAID:
        ctx.conversation.needs_human = True
        return "The order is already paid; I'll ask the shop team to help with cancellation or refund."
    if order.status == OrderStatus.AWAITING_PAYMENT:
        ctx.conversation.needs_human = True
        return "A payment request may still complete. I'll ask the shop team to verify it before cancellation."
    order.status = OrderStatus.CANCELLED
    if ctx.extra.get("platform"):
        ctx.extra["platform"].release_order_inventory(ctx.business.id, order.id)
    ctx.conversation.order = None
    ctx.conversation.payment = None
    return "I've cancelled the unpaid order."


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
    "set_delivery_note": set_delivery_note,
    "remove_order_item": remove_order_item,
    "cancel_order": cancel_order,
}
