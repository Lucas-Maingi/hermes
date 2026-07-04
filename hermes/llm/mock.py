"""A deterministic, rule-based stand-in for a real LLM.

This is NOT the product's brain -- a real provider (see factory.py) is. But it
lets the entire agent, payment flow, and dashboard run and be tested with zero
API keys, and it makes CI deterministic. It recognises the core intents
(greeting, question, order, pay, human handoff) and emits the same tool calls a
real model would, so the orchestrator behaves identically either way.

It understands a little Swahili/Sheng (nataka, bei, uko wapi, nlipe, mtu),
because the target market does.
"""

from __future__ import annotations

import re

from hermes.llm.base import LLMResponse, ToolCall, ToolSpec

_GREETING_RE = re.compile(r"\b(hi|hello|hey|habari|niaje|sasa|mambo|vipi)\b", re.I)
_QUESTION_RE = re.compile(
    r"\b(do you have|how much|price|cost|bei|where|location|uko wapi|open|hours|"
    r"delivery|deliver|available|stock|what|which)\b|\?",
    re.I,
)
_PAY_RE = re.compile(r"\b(pay|payment|checkout|mpesa|m-pesa|nlipe|nilipe|lipa|nataka kulipa)\b", re.I)
_HUMAN_RE = re.compile(
    r"\b(human|person|agent|someone|manager|talk to|speak to|mtu|real person|customer care)\b", re.I
)
_ORDER_INTENT_RE = re.compile(r"\b(order|buy|want|need|nataka|nipe|niletee|get me|add)\b", re.I)

# quantity + item, e.g. "2 maize flour", "3 sacks of sugar", "nataka 5 unga"
_ITEM_RE = re.compile(
    r"(\d+)\s*(?:x|pcs?|pieces?|sacks?|packets?|kg|litres?|l)?\s*(?:of\s+)?([a-zA-Z][a-zA-Z0-9 '\-]{1,40}?)"
    r"(?=(?:\s*,|\s+and\s+|\s+na\s+|\s*$|\s+\d))",
    re.I,
)

_CONFIRM_RE = re.compile(r"\b(yes|yeah|yep|sawa|ndio|ndiyo|confirm|correct|go ahead|proceed)\b", re.I)


class MockLLM:
    """Rule-based LLMClient implementation. Deterministic given the same input."""

    def complete(self, system, messages, tools=None):  # noqa: D401 - protocol impl
        tool_names = {t.name for t in (tools or [])}
        last = messages[-1] if messages else {"role": "user", "content": ""}

        # If we're being called right after a tool executed, narrate the result
        # to the customer instead of calling another tool.
        if last["role"] == "tool":
            return LLMResponse(text=self._narrate_tool_result(last["content"]))

        text = last.get("content", "")

        # 1. Explicit human handoff request -> escalate.
        if _HUMAN_RE.search(text) and "escalate_to_human" in tool_names:
            return LLMResponse(
                tool_calls=[ToolCall(name="escalate_to_human", arguments={"reason": "customer_requested"})]
            )

        # 2. Payment intent -> STK push (only meaningful once an order exists;
        #    the tool itself validates and returns an error the agent relays).
        if _PAY_RE.search(text) and "mpesa_stk_push" in tool_names:
            return LLMResponse(tool_calls=[ToolCall(name="mpesa_stk_push", arguments={})])

        # 3. Order intent with parseable items -> capture each line.
        items = self._parse_items(text)
        if items and (_ORDER_INTENT_RE.search(text) or len(items) >= 1) and "capture_order" in tool_names:
            calls = [
                ToolCall(name="capture_order", arguments={"item": name, "quantity": qty})
                for qty, name in items
            ]
            return LLMResponse(tool_calls=calls)

        # 4. A question -> ground the answer in the business knowledge base.
        if _QUESTION_RE.search(text) and "lookup_knowledge" in tool_names:
            return LLMResponse(tool_calls=[ToolCall(name="lookup_knowledge", arguments={"query": text})])

        # 5. Greeting or anything else -> a friendly opener.
        if _GREETING_RE.search(text) or not text.strip():
            return LLMResponse(
                text="Hi! Welcome. I can share our products and prices, take your order, "
                "and send you an M-Pesa prompt to pay. What can I get you?"
            )

        # 6. Fallback: try the knowledge base, else a gentle re-prompt.
        if "lookup_knowledge" in tool_names:
            return LLMResponse(tool_calls=[ToolCall(name="lookup_knowledge", arguments={"query": text})])
        return LLMResponse(text="Sorry, I didn't quite get that. Could you rephrase?")

    @staticmethod
    def _parse_items(text: str) -> list[tuple[int, str]]:
        items: list[tuple[int, str]] = []
        for qty_str, name in _ITEM_RE.findall(text):
            name = name.strip().rstrip(".").strip()
            # Drop trailing filler words the regex may capture.
            name = re.sub(r"\b(please|tafadhali|now|today)\b", "", name, flags=re.I).strip()
            if name and not _PAY_RE.search(name):
                items.append((int(qty_str), name))
        return items

    @staticmethod
    def _narrate_tool_result(tool_output: str) -> str:
        # The tool already returns a customer-ready string; the mock passes it
        # through. A real LLM would rephrase it in the brand's voice.
        return tool_output


def default_tools() -> list[ToolSpec]:
    """The tool specs the agent exposes. Defined here so both the mock and any
    real provider advertise an identical toolset."""
    return [
        ToolSpec(
            name="lookup_knowledge",
            description="Answer a customer question using ONLY the business's catalog and info. "
            "Use for prices, product availability, hours, location, delivery, and policies.",
            parameters={"query": {"type": "string", "description": "The customer's question"}},
            required=["query"],
        ),
        ToolSpec(
            name="capture_order",
            description="Add an item to the customer's order. Price is resolved from the catalog, "
            "never invented.",
            parameters={
                "item": {"type": "string", "description": "Product name as the customer referred to it"},
                "quantity": {"type": "integer", "description": "How many units"},
            },
            required=["item", "quantity"],
        ),
        ToolSpec(
            name="mpesa_stk_push",
            description="Send an M-Pesa STK-push payment prompt to the customer's phone for the "
            "current order total. Only call once the order is confirmed.",
            parameters={},
        ),
        ToolSpec(
            name="check_payment",
            description="Check whether the pending M-Pesa payment has completed.",
            parameters={},
        ),
        ToolSpec(
            name="escalate_to_human",
            description="Hand the conversation to a human staff member. Call when the customer "
            "asks for a person, is upset, or asks something you cannot answer from the catalog.",
            parameters={"reason": {"type": "string", "description": "Short reason for handoff"}},
            required=["reason"],
        ),
    ]
