"""The agent orchestrator: turns a customer message into a reply by running a
tool-calling loop over the LLM + the registered tools.

Channel- and provider-agnostic. The same ``Agent.handle_message`` serves the
WhatsApp webhook and the web simulator, running on the mock LLM or a real one.
"""

from __future__ import annotations

from hermes.business import Business
from hermes.llm.base import LLMClient
from hermes.llm.factory import get_llm_client
from hermes.llm.mock import default_tools
from hermes.models import Conversation, Role
from hermes.mpesa.factory import get_mpesa_client
from hermes.payments import build_tool_registry
from hermes.tools import ToolContext

_SYSTEM_TEMPLATE = """You are the assistant for {business_name}, a shop.
You help customers over chat: answer questions, take orders, and collect payment via M-Pesa.

Rules:
- Be brief, warm, and natural. Match the customer's language (English, Swahili, or Sheng).
- NEVER invent prices, products, hours, or policies. Use the lookup_knowledge tool for any
  factual question, and only state what it returns.
- Use capture_order to add items (prices come from the catalog, not you).
- When the customer is ready to pay, use mpesa_stk_push, then check_payment to confirm.
- If you cannot answer from the shop's information, or the customer is upset or asks for a
  person, use escalate_to_human. Do not pretend to know things.
Currency is {currency}.
"""

MAX_TOOL_STEPS = 6


class Agent:
    def __init__(self, business: Business, llm: LLMClient, mpesa=None):
        self.business = business
        self.llm = llm
        self.mpesa = mpesa
        self.tools = build_tool_registry()
        self.tool_specs = default_tools()
        self.system = _SYSTEM_TEMPLATE.format(
            business_name=business.name, currency=business.currency
        )

    def handle_message(self, conversation: Conversation, text: str) -> str:
        """Process one inbound customer message and return the agent's reply.
        Mutates ``conversation`` (messages, order, payment, needs_human)."""
        conversation.add_message(Role.CUSTOMER, text)
        history = self._build_history(conversation)
        ctx = ToolContext(conversation=conversation, business=self.business, mpesa=self.mpesa)

        tools_used: list[str] = []
        for _ in range(MAX_TOOL_STEPS):
            resp = self.llm.complete(self.system, history, self.tool_specs)

            if not resp.wants_tools:
                reply = resp.text or "Sorry, could you say that again?"
                conversation.add_message(Role.AGENT, reply, tools_used=tools_used)
                return reply

            for call in resp.tool_calls:
                tool = self.tools.get(call.name)
                if tool is None:
                    result = f"(unknown tool {call.name})"
                else:
                    result = tool(call.arguments, ctx)
                    tools_used.append(call.name)
                history.append({"role": "assistant", "content": f"[called {call.name}]"})
                history.append({"role": "tool", "content": result})

        # Safety net: tool loop didn't converge -> return the last tool result.
        last_tool = next((m["content"] for m in reversed(history) if m["role"] == "tool"), "")
        reply = last_tool or "Let me get someone from the team to help you."
        if not last_tool:
            conversation.needs_human = True
        conversation.add_message(Role.AGENT, reply, tools_used=tools_used)
        return reply

    @staticmethod
    def _build_history(conversation: Conversation) -> list[dict[str, str]]:
        """Map stored conversation messages to the LLM's role/content format.
        Only customer/agent turns carry into history; tool results are per-turn."""
        role_map = {Role.CUSTOMER: "user", Role.AGENT: "assistant", Role.HANDOFF: "assistant"}
        history = []
        for msg in conversation.messages:
            api_role = role_map.get(msg.role)
            if api_role:
                history.append({"role": api_role, "content": msg.text})
        return history


def build_agent(business: Business) -> Agent:
    """Construct an Agent wired to whatever LLM/M-Pesa providers the environment
    configures -- real ones when credentials are present, simulators otherwise."""
    return Agent(business=business, llm=get_llm_client(), mpesa=get_mpesa_client())
