# Contributing to Hermes

The architecture is a set of narrow interfaces with simulators behind each one. Most contributions are one of: a new agent tool, a new LLM provider, or a new payment provider.

## Ground rules

- **Simulator-first.** Every external dependency (WhatsApp, M-Pesa, the LLM) has a keyless simulator/mock behind the same interface as the real adapter. A feature that only works with real credentials can't be demoed, tested in CI, or trusted — build the simulator side in the same PR.
- **The agent never invents business facts.** Prices, stock, and policies come from the tenant's catalog via `lookup_knowledge`; the LLM formats, it does not decide. Any change that lets model output reach a customer as a *fact* without passing through the catalog is a bug, not a feature.
- **Escalation is sacred.** `escalate_to_human` exists so the agent fails safe. Don't add retry-harder logic in front of it.
- **Tests run offline.** 87 tests, zero network. Keep it that way.

## Adding an agent tool

Tools live in [hermes/tools.py](hermes/tools.py): a function taking `(args: dict, ctx: ToolContext)` and returning a string the LLM can use.

1. Keep the return value *informative on failure* — the LLM reads it and decides what to tell the customer, so "item 'breda' not found; closest match 'bread' (70 KES)" beats an exception.
2. Register it wherever the agent's tool schema is declared so the model can call it, and cap side effects: anything that moves money goes through the payment flow's confirm step (`confirm_order_ready_for_payment`), never directly from a tool call.
3. Test it twice: once as a unit (call the function), once through the agent loop with `MockLLM` scripted to invoke it.

Remember the loop is bounded (`MAX_TOOL_STEPS = 6` in [hermes/agent.py](hermes/agent.py)) — a tool that needs multi-step back-and-forth should return richer output instead of forcing more turns.

## Adding an LLM or payment provider

Both follow the same pattern: implement the base interface ([hermes/llm/base.py](hermes/llm/base.py) or [hermes/mpesa/base.py](hermes/mpesa/base.py)), register it in the corresponding `factory.py`, and select it by environment variable. Look at `openai_compatible.py` / `daraja.py` for the shape.

Rules for payment adapters specifically:

- Parse callbacks defensively — Daraja's callback shape has surprises, and `parse_stk_callback` returning `None` (ignore) must stay the failure mode, never an exception that 500s the callback endpoint (Safaricom retries on non-200, and you do not want duplicate payment processing).
- The simulator must mirror the real adapter's request/response shape faithfully, including the failure cases (wrong PIN, timeout, insufficient funds).

## Webhook security

Inbound WhatsApp POSTs are verified against Meta's `X-Hub-Signature-256` when `WHATSAPP_APP_SECRET` is set (see `verify_signature` in [hermes/whatsapp.py](hermes/whatsapp.py)). If you add a new inbound surface, it needs the equivalent: authenticate the caller or document explicitly why it's safe not to.

## Running checks

```bash
pip install -e ".[dashboard,dev]"
ruff check .
pytest
```

CI runs lint + tests and builds/boots the Docker image on every push.

## Commit style

Small commits, present tense, explain *why* when the diff can't.
