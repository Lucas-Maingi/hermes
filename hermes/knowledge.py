"""Grounded question answering over a business's catalog and info facts.

The whole point is that the agent answers ONLY from the business's real data --
so it can never quote a price the shop didn't set. A miss returns ``None`` so
the agent can honestly say it doesn't know (and offer a human) rather than
guessing.
"""

from __future__ import annotations

import re

from hermes.business import Business

# Maps an info topic to the words that signal a customer is asking about it.
_INFO_TRIGGERS = {
    "hours": ["hour", "open", "close", "time", "when", "saa", "fungua"],
    "location": ["where", "location", "address", "find you", "uko wapi", "mahali"],
    "delivery": ["deliver", "delivery", "shipping", "bring", "leta", "send"],
    "payment": ["pay", "payment", "mpesa", "m-pesa", "lipa", "cash", "card"],
    "returns": ["return", "refund", "exchange", "warranty", "rudisha"],
    "contact": ["contact", "call", "phone", "number", "reach", "simu"],
}


def answer(query: str, business: Business) -> str | None:
    """Return a grounded answer to ``query`` for ``business``, or None if the
    business's data doesn't cover it."""
    q = query.lower()

    # 1. Product/price questions take priority.
    product = business.find_product(q)
    if product is not None and _looks_like_product_question(q):
        return _describe_product(product, business)

    # 2. Info-fact questions (hours, location, ...). Match trigger words on
    #    word boundaries so, e.g., "airtime" doesn't trip the "time" trigger.
    for topic, triggers in _INFO_TRIGGERS.items():
        if topic in business.info and any(
            re.search(rf"\b{re.escape(t)}\b", q) for t in triggers
        ):
            return business.info[topic]

    # 3. A bare product mention with no explicit question word: still helpful.
    if product is not None:
        return _describe_product(product, business)

    # 4. "What do you sell / show me your products" style.
    if re.search(r"\b(what.*(sell|have|offer)|products?|menu|catalog|price list|bei zote)\b", q):
        return _catalog_summary(business)

    return None


def _looks_like_product_question(q: str) -> bool:
    return bool(
        re.search(r"\b(how much|price|cost|bei|do you have|available|stock|got any)\b", q) or "?" in q
    )


def _describe_product(product, business: Business) -> str:
    label = product.name + (f" ({product.unit})" if product.unit else "")
    if not product.in_stock:
        return f"Sorry, {label} is currently out of stock."
    return f"{label} is {business.currency} {product.price:,.0f}."


def _catalog_summary(business: Business) -> str:
    in_stock = [p for p in business.products if p.in_stock]
    if not in_stock:
        return "We're restocking right now - please check back shortly."
    lines = [
        f"- {p.name}{f' ({p.unit})' if p.unit else ''}: {business.currency} {p.price:,.0f}"
        for p in in_stock
    ]
    return "Here's what we have:\n" + "\n".join(lines)
