# Hermes — WhatsApp AI Commerce & Support Agent for African SMEs

[![CI](https://github.com/Lucas-Maingi/hermes/actions/workflows/ci.yml/badge.svg)](https://github.com/Lucas-Maingi/hermes/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

In Kenya and much of Africa, **WhatsApp is the storefront and M-Pesa is the checkout.** Small business owners lose hours a day answering the same questions, taking orders by hand, and chasing payments — and lose sales entirely after hours. Hermes is an AI agent that handles all three, in the channels customers already use.

A business connects its WhatsApp number and Hermes:

1. **Answers FAQs** grounded in the business's own catalog and info (so it never invents a price).
2. **Takes orders** conversationally, extracting items, quantities, and delivery details into structured data.
3. **Collects payment** by triggering an **M-Pesa STK push** and confirming when paid.
4. **Hands off to a human** the moment it's unsure or the customer asks — it never fakes competence.
5. Gives the owner a **dashboard**: deflection rate, orders captured, revenue collected, and cost per conversation — value stated in shillings.

## Live demo

**Try it:** https://huggingface.co/spaces/lucas-maingi/hermes

Chat with the agent (English or Swahili/Sheng — try `nataka 2 unga na 1 cooking oil`), press the "enter M-Pesa PIN" button to run a payment end-to-end, and watch the owner dashboard update. The demo runs on the built-in simulators; the same code runs on real WhatsApp + M-Pesa when credentials are set (below).

## What's real vs. what you plug in

This is a real, connectable product, not a mock. To be precise about the line:

**Real and working in the code (here now):**
- The actual **Meta WhatsApp Cloud API** integration — webhook verification handshake, inbound-message parsing, and outbound sending via the Graph API.
- The actual **M-Pesa Daraja** flow — OAuth token, STK-push (`processrequest`), status query, and callback parsing, against sandbox or production.
- A provider-agnostic **LLM** layer that runs on Groq's free tier or OpenAI by config.
- Multi-tenant catalog/knowledge, grounded pricing, order capture, human handoff, persistence, and the owner dashboard.

**What only a business can supply to go live** (real-world setup, not code):
- **M-Pesa production credentials** — a registered business + paybill/till + Safaricom Daraja **Go-Live** approval. (Sandbox proves the flow for free; collecting real money needs this.)
- **A WhatsApp number** on a Meta Business app (test number works immediately; a public brand number needs Meta business verification).
- **Always-on hosting** with a public HTTPS URL for the webhook and M-Pesa callback (Render / Railway / a VPS), and ideally Postgres instead of SQLite.

The simulators exist so the product is fully demonstrable and testable **without** any of the above — set the credentials and the same code path serves real customers.

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

## Run it locally

```bash
pip install -e ".[dashboard,dev]"

# The connectable API (WhatsApp webhook, M-Pesa callback, /chat):
uvicorn hermes.app:app --reload --port 8000        # docs at /docs

# The demo dashboard + chat simulator:
python scripts/seed_demo_data.py
HERMES_DB=demo/hermes_demo.db streamlit run hermes/dashboard.py
```

Everything runs on the built-in simulators out of the box — no keys needed.

## Going live (environment variables)

Set these and the same code switches from simulator to the real integration automatically:

| Variable | Purpose |
| :--- | :--- |
| `HERMES_LLM_PROVIDER`, `GROQ_API_KEY` | Run the agent on a real model (Groq free tier / OpenAI). |
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN` | Meta Cloud API. Point your app's webhook at `POST /webhook` (verify at `GET /webhook`). |
| `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_SHORTCODE`, `MPESA_PASSKEY`, `MPESA_CALLBACK_URL`, `MPESA_ENVIRONMENT` | Daraja. Set `MPESA_CALLBACK_URL` to your public `POST /mpesa/callback`. |

## Testing

```bash
pytest            # 83 tests, no network or API keys required
ruff check .
```

CI runs lint + tests and builds/boots the API image on every push.

## Known Limitations

- **Single-file SQLite store.** Fine for one shop and the demo; a multi-tenant production deployment would move to Postgres and a proper job/queue for outbound sends.
- **Onboarding is code-level.** Businesses are configured as `Business` objects; a self-serve signup flow (connect WhatsApp, upload catalog, connect M-Pesa) is the next product step, not yet built.
- **The bundled mock LLM is rule-based**, not a real model — it exists for keyless CI/demo. Real conversational quality comes from setting a provider key.
- **Demo hosting sleeps.** The free HF Space is fine to try; a business's live line needs always-on hosting.

## License

MIT.
