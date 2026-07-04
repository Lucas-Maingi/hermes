# Hermes — WhatsApp AI Commerce & Support Agent for African SMEs

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

In Kenya and much of Africa, **WhatsApp is the storefront and M-Pesa is the checkout.** Small business owners lose hours a day answering the same questions, taking orders by hand, and chasing payments — and lose sales entirely after hours. Hermes is an AI agent that handles all three, in the channels customers already use.

A business connects its WhatsApp number and Hermes:

1. **Answers FAQs** grounded in the business's own catalog and info (so it never invents a price).
2. **Takes orders** conversationally, extracting items, quantities, and delivery details into structured data.
3. **Collects payment** by triggering an **M-Pesa STK push** and confirming when paid.
4. **Hands off to a human** the moment it's unsure or the customer asks — it never fakes competence.
5. Gives the owner a **dashboard**: deflection rate, orders captured, revenue collected, and cost per conversation — value stated in shillings.

> **Status:** in active development, built milestone by milestone. See commit history.

## Design principle: demoable with zero production accounts

Hermes is built so the whole thing runs and can be demoed **without a WhatsApp number, an M-Pesa account, or a paid LLM key**:

- A **web chat simulator** drives the exact same agent core the real WhatsApp webhook uses.
- A **Daraja-faithful M-Pesa simulator** mirrors Safaricom's real STK-push request/response/callback shape — with the real Daraja-sandbox adapter built behind the same interface.
- The LLM layer is **provider-agnostic** with a deterministic mock, so tests and the demo never require an API key; a free-tier provider (Gemini/Groq) powers the live demo.

The real Meta WhatsApp Cloud API and Daraja sandbox adapters are built alongside their simulators and activate with credentials.

## Architecture

```
  WhatsApp Cloud API  ─┐
                       ├─►  FastAPI webhook  ─►  Agent orchestrator  ─►  LLM (provider-agnostic)
  Web chat simulator  ─┘                              │                        │
                                                      │                        ▼
                                                      │                 tool-calling loop
                                                      ▼                        │
                                          conversation / order / payment state │
                                                      │            ┌───────────┴───────────┐
                                                      ▼            ▼           ▼           ▼
                                                 SQLite store  knowledge   capture_    mpesa_stk_push
                                                               lookup      order       (Daraja / simulator)
                                                                                         │
                                          Owner dashboard (deflection, revenue, cost) ◄──┘
```

## License

MIT.
