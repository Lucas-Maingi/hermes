# Hermes — WhatsApp AI Commerce & Support Agent for African SMEs

[![CI](https://github.com/Lucas-Maingi/hermes/actions/workflows/ci.yml/badge.svg)](https://github.com/Lucas-Maingi/hermes/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

In Kenya and much of Africa, **WhatsApp is the storefront and M-Pesa is the checkout.** Small business owners lose hours a day answering the same questions, taking orders by hand, and chasing payments — and lose sales entirely after hours. Hermes is an AI agent that handles all three, in the channels customers already use.

A business connects its WhatsApp number and Hermes:

1. **Answers FAQs** grounded in the business's own catalog and info (so it never invents a price).
2. **Takes orders** conversationally, extracting items, quantities, and delivery details into structured data.
3. **Collects payment** by triggering an **M-Pesa STK push** and confirming when paid.
4. **Queues conversations for human follow-up** when it's unsure or the customer asks. Staff can assign and resolve requests in the dashboard; external staff notifications are not yet sent.
5. Gives the owner a **dashboard** for conversations, paid orders, revenue, staff handoffs, integrations, and tenant data management.

## Live demo

**Try it:** https://huggingface.co/spaces/lucas-maingi/hermes

Chat with the agent (English or Swahili/Sheng — try `nataka 2 unga na 1 cooking oil`), press the "enter M-Pesa PIN" button to run a payment end-to-end, and watch the owner dashboard update. The demo runs on the built-in simulators; the same code runs on real WhatsApp + M-Pesa when credentials are set (below).

## What's real vs. what you plug in

This is a real, connectable product, not a mock. To be precise about the line:

**Implemented, with live credentials still to be validated:**
- Meta WhatsApp Cloud API webhook verification, tenant-specific signature checks, inbound routing and outbound text sending.
- M-Pesa Daraja OAuth, STK push, status query and callback reconciliation.
- A provider-agnostic LLM adapter, tenant-configurable credentials, and explicit consent before customer data is sent to an external LLM or embedding service.
- Email-verified merchant signup with a seven-day Premium trial (invite-only mode remains available), tenant-scoped sessions/data, encrypted provider credentials and conversations, an assignable staff handoff queue, idempotent inbound events, retryable outbound messages, document BM25/vector hybrid retrieval, and the browser dashboard.
- Tenant-scoped stock counts with adjustment history, expiring order reservations and payment-confirmed deductions; customer self-booking with merchant-configured weekly hours and 30-minute slots.
- Approved WhatsApp template records, opt-in/opt-out tracking (including STOP keywords), and a durable scheduled follow-up worker. Merchants must first get templates approved in Meta and attest approval in the dashboard.
- Hermes Premium subscription checkout via a separately configured Daraja STK push, with renewal reconciliation, a seven-day grace period, and callback status/amount/phone/receipt verification before activation. A status query alone does not finalize an order.
- Dashboard campaign attribution for Meta click-to-WhatsApp referral source IDs, plus read-only Meta account and Daraja OAuth checks.

These are implemented adapters, not proof of a live production deployment. The
offline suite exercises simulators, request construction, tenant routing and
privacy gates. Connecting a Meta test number and Safaricom sandbox account is
still required to validate credentials, permissions, callback reachability and
live delivery. No live Meta/Daraja transaction has been performed by this repo.

**What only a business can supply to go live** (real-world setup, not code):
- **M-Pesa production credentials** — a registered business + paybill/till + Safaricom Daraja **Go-Live** approval. (Sandbox proves the flow for free; collecting real money needs this.)
- **A WhatsApp number** on a Meta Business app (test number works immediately; a public brand number needs Meta business verification).
- **Always-on hosting** with a public HTTPS URL for the webhook and M-Pesa callback (Render / Railway / a VPS), and ideally Postgres instead of SQLite.

The simulators exist so the product is fully demonstrable and testable **without** any of the above — set the credentials and the same code path serves real customers.

## Design principle: demoable with zero production accounts

Hermes is built so the whole thing runs and can be demoed **without a WhatsApp number, an M-Pesa account, or a paid LLM key**:

- A **web chat simulator** drives the exact same agent core the real WhatsApp webhook uses.
- A **Daraja-faithful M-Pesa simulator** mirrors Safaricom's real STK-push request/response/callback shape — with the real Daraja-sandbox adapter built behind the same interface.
- The LLM layer is **provider-agnostic** with a deterministic mock, so tests and local exploration never require an API key; real providers require credentials and explicit tenant consent.

The real Meta WhatsApp Cloud API and Daraja sandbox adapters are built alongside their simulators and activate with credentials.

## Architecture

```
  WhatsApp Cloud API  ─┐
                       ├─►  FastAPI webhook  ─►  Agent orchestrator  ─►  LLM (provider-agnostic)
  Browser dashboard / simulator ─┐                   │
                                  ├─► FastAPI ─► tenant-scoped Agent ─► LLM (consent gated)
  WhatsApp Cloud API ─────────────┘                       │
                                                         ▼
                                          hybrid retrieval + commerce tools
                                            │              │             │
                                            ▼              ▼             ▼
                                        BM25/vector    SQLite tenant    M-Pesa Daraja
                                        knowledge       data/queues      or simulator
                                            ▲              │
                                            └──── worker ──┴─► retries + retention
```

For the detailed request/order flow and a first-pilot readiness checklist,
see [docs/PILOT_READINESS.md](docs/PILOT_READINESS.md).

## Run it locally

Install and launch the dedicated dashboard/API from PowerShell:

```powershell
python -m pip install -e ".[dev]"
# Enable self-serve trial registration locally. Setting HERMES_INVITE_TOKEN
# instead restricts registration to people who have the invitation code.
$env:HERMES_PUBLIC_SIGNUP_ENABLED = "true"
# Generate once, store securely, and reuse this same key on every restart:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
$env:HERMES_ENCRYPTION_KEY = "paste-the-same-generated-key-here"
$env:HERMES_DB = ".\hermes.db"
uvicorn hermes.app:app --reload --port 8000
```

Open `http://127.0.0.1:8000` to register. Email verification is required before
the account session and seven-day Premium trial begin. In local development,
configure SMTP (`HERMES_SMTP_HOST` and `HERMES_EMAIL_FROM`, with optional
credentials/TLS settings below) or use the test mail sender. Self-serve
registration is disabled by default; enable it with
`HERMES_PUBLIC_SIGNUP_ENABLED=true`. To restrict registration to invited
pilots, set `HERMES_INVITE_TOKEN` instead. The trial is followed by a free
workspace tier. Premium costs KES 2,500 per month, with a seven-day grace
period and customer-authorized M-Pesa STK renewal; feature access is gated by
the current plan. For durable message retries, billing reconciliation,
reservation expiry, and daily retention, start a second PowerShell window with
the same environment and run
`python scripts/run_worker.py`. The API also attempts immediate queued delivery;
the worker recovers retries and sends after API restarts.

Configure WhatsApp, M-Pesa and optional tenant LLM/embedding credentials in the
Integrations page. Credentials are write-only and encrypted with
`HERMES_ENCRYPTION_KEY`. Customer data stays on the built-in mock agent unless
the merchant explicitly consents to an external LLM; hybrid vector retrieval
similarly requires embedding consent. Booking hours, inventory, and campaign
source IDs are managed in the dashboard. The `/chat` API is a separate
simulator surface and requires `HERMES_CHAT_API_TOKEN`.

The Knowledge base accepts text-based PDF, Word (`.docx`/`.docm`), Excel
(`.xlsx`/`.xlsm`), PowerPoint (`.pptx`/`.pptm`), TXT, Markdown, CSV, JSON, HTML,
XML, YAML, RTF, and EML files up to 15 MB each. Files are parsed locally and
only extracted text is indexed; originals are not retained. Scanned PDFs and
images need OCR before upload. Legacy binary Office files (`.doc`, `.xls`,
`.ppt`) and arbitrary archives are not supported.

Create a consistent SQLite snapshot:

```powershell
python scripts/backup_db.py --db .\hermes.db --directory .\backups
```

For restore, stop the API and worker, then run
`python scripts/restore_db.py --backup <backup-file> --db .\hermes.db --confirm-replace`;
the current database is first preserved as a rollback copy.

## Going live (environment variables)

Set these and the same code switches from simulator to the real integration automatically:

| Variable | Purpose |
| :--- | :--- |
| `HERMES_INVITE_TOKEN` | Optional. When set, registration requires this invitation code, regardless of the self-serve flag. |
| `HERMES_PUBLIC_SIGNUP_ENABLED` | Set to `true` to enable public trial signup when no invite token is configured. Email verification and basic IP/email rate limits are enforced; use invite-only signup for a controlled pilot. |
| `HERMES_SMTP_HOST`, `HERMES_EMAIL_FROM` | Required to deliver verification emails. Optional: `HERMES_SMTP_PORT` (587), `HERMES_SMTP_USERNAME`, `HERMES_SMTP_PASSWORD`, `HERMES_SMTP_USE_SSL`, and `HERMES_SMTP_STARTTLS`. Use SSL or STARTTLS. Set `HERMES_PUBLIC_BASE_URL` to the public HTTPS origin outside localhost. |
| `HERMES_BILLING_MPESA_CONSUMER_KEY`, `HERMES_BILLING_MPESA_CONSUMER_SECRET`, `HERMES_BILLING_MPESA_SHORTCODE`, `HERMES_BILLING_MPESA_PASSKEY`, `HERMES_BILLING_MPESA_CALLBACK_URL` | Hermes's own Daraja account for collecting the KES 2,500 Premium subscription, separate from merchant M-Pesa credentials. Callback URL must be public HTTPS; set `HERMES_BILLING_MPESA_ENVIRONMENT` to `sandbox` or `production`. Renewal uses an explicit customer-authorized STK prompt, not an automatic recurring mandate. |
| `HERMES_ENCRYPTION_KEY` | Required to persist/read encrypted merchant credentials, catalog/knowledge content, and customer conversations. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and keep it backed up separately from the database. |
| `HERMES_DB` | SQLite database path; use one API instance and one worker against a persistent disk. |
| `HERMES_RETENTION_DAYS` | Inactive conversation retention period used by the worker (default 365 days). |
| `HERMES_LLM_PROVIDER`, `GROQ_API_KEY`, `OPENAI_API_KEY` | Optional shared LLM provider for tenants that explicitly consent; merchants can instead configure per-tenant LLM credentials in the dashboard. |
| WhatsApp phone ID, access token, app secret, verify token | Set per merchant in Integrations. Configure Meta's webhook at `GET/POST /webhook`; signed inbound traffic is required. |
| M-Pesa consumer key/secret, shortcode, passkey, callback URL | Set per merchant in Integrations. The callback must be reachable over public HTTPS. |
| `WHATSAPP_GRAPH_API_VERSION` | Meta Graph API version for sends (defaults to `v26.0`); keep it aligned with Meta's supported-version schedule. |
| `HERMES_CHAT_API_TOKEN` | Required Bearer token for the optional `/chat` API; endpoint returns 503 when unset. |

## Testing

```bash
pytest            # no network or API keys required
ruff check .
```

CI runs lint + tests and builds/boots the API image on every push.

## Known Limitations

- **Single-instance SQLite.** Tenant accounts and data are isolated at the application layer, but SQLite is not a horizontally scaled SaaS datastore. Run one API instance and one worker; consider Postgres and a managed queue before scaling.
- **Account safeguards and billing need live validation.** Email verification, login/signup rate limits, Premium entitlements, customer-authorized monthly STK checkout, grace period, and billing reconciliation are implemented. Configure SMTP and Hermes's own Daraja credentials, then test renewal, callback delivery, failed/late payments, and reconciliation in the sandbox before accepting paid customers. There is no automatic recurring payment mandate.
- **Human handoff is an in-dashboard queue.** Staff can assign/resolve requests, but outbound staff notification and response-time automation are not implemented.
- **Operational backups are operator-run.** The repository supplies consistent backup/restore commands and scheduled conversation retention; production still needs off-host encrypted backup storage, monitoring, restore drills, and key-rotation procedures.
- **Campaign attribution is source-ID based.** Dashboard campaigns match Meta click-to-WhatsApp referral `source_id` values and report verified paid-order conversions. This is not generalized UTM attribution or ad-platform spend reporting. WhatsApp number coexistence and off-platform payment screenshot verification are not implemented.
- **WhatsApp template approval is not integrated with Meta.** Merchants must create/submit templates in WhatsApp Manager, wait for approval, and accurately confirm the approved name/language/category/variables in Hermes. Scheduled sends require the worker, live credentials, and recorded opt-in. Template UI supports body text parameters only.
- **Bookings support customer self-service.** Merchants configure weekly hours and a timezone; customers can select 30-minute slots and cancel using the booking reference and original phone. Deposits, calendar sync, and automated booking reminders are not implemented.
- **Inventory reservations are local to the order/payment flow.** Stock is reserved when an order is submitted, expired reservations are released by the worker, and confirmed payments deduct stock. A late successful payment that cannot commit reserved stock is escalated for staff review.
- **The bundled mock LLM is rule-based**, not a real model — it exists for keyless CI/demo. Real conversational quality comes from setting a provider key.
- **Demo hosting sleeps.** The free HF Space is fine to try; a business's live line needs always-on hosting.

## License

MIT.
