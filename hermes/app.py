"""FastAPI application: the real connectable surface.

Endpoints:
  GET  /health              liveness + which providers are live (real vs simulated)
  POST /chat                drive the agent from the web simulator or any client
  GET  /webhook             Meta WhatsApp Cloud API verification handshake
  POST /webhook             Meta inbound messages -> agent -> WhatsApp reply
  POST /mpesa/callback      Daraja STK callback -> mark payment paid + receipt
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response, status
from pydantic import BaseModel, Field

from hermes import whatsapp
from hermes.callbacks import parse_stk_callback
from hermes.models import OrderStatus, PaymentStatus
from hermes.runtime import Runtime


class ChatRequest(BaseModel):
    text: str = Field(..., description="The customer's message")
    phone: str = Field("254700000000", description="Customer phone (MSISDN)")
    conversation_id: str | None = Field(None, description="Continue an existing conversation")


def create_app(runtime: Runtime | None = None) -> FastAPI:
    rt = runtime or Runtime()
    app = FastAPI(title="Hermes", description="WhatsApp AI commerce & support agent for SMEs.", version="0.1.0")
    app.state.rt = rt

    @app.get("/health")
    def health():
        return {
            "status": "healthy",
            "llm_provider": type(rt.llm).__name__,
            "mpesa_provider": type(rt.mpesa).__name__,
            "businesses": list(rt.businesses),
        }

    @app.post("/chat")
    def chat(req: ChatRequest):
        business = rt.default_business
        if req.conversation_id:
            conv = rt.store.get(req.conversation_id) or rt.continue_or_start(req.phone, "simulator", business)
        else:
            conv = rt.continue_or_start(req.phone, "simulator", business)

        agent = rt.agent_for(business)
        reply = agent.handle_message(conv, req.text)
        rt.store.save(conv)

        return {
            "conversation_id": conv.id,
            "reply": reply,
            "needs_human": conv.needs_human,
            "order_total": conv.order.total if conv.order else 0.0,
            "order_status": conv.order.status.value if conv.order else None,
            "payment_status": conv.payment.status.value if conv.payment else None,
            "checkout_request_id": conv.payment.checkout_request_id if conv.payment else None,
        }

    @app.get("/webhook")
    def verify(request: Request):
        params = request.query_params
        challenge = whatsapp.verify_webhook(
            params.get("hub.mode"), params.get("hub.verify_token"), params.get("hub.challenge")
        )
        if challenge is None:
            return Response(status_code=status.HTTP_403_FORBIDDEN)
        return Response(content=challenge, media_type="text/plain")

    @app.post("/webhook")
    async def inbound(request: Request):
        body = await request.body()
        if not whatsapp.verify_signature(body, request.headers.get("X-Hub-Signature-256")):
            return Response(status_code=status.HTTP_403_FORBIDDEN)
        payload = await request.json()
        for msg in whatsapp.parse_inbound(payload):
            business = rt.business_for_phone_number_id(msg.phone_number_id)
            conv = rt.continue_or_start(msg.from_phone, "whatsapp", business)
            conv.channel = "whatsapp"
            reply = rt.agent_for(business).handle_message(conv, msg.text)
            rt.store.save(conv)
            whatsapp.send_text(msg.from_phone, reply, phone_number_id=msg.phone_number_id)
        # Meta expects a 200 quickly regardless.
        return {"status": "received"}

    @app.post("/mpesa/callback")
    async def mpesa_callback(request: Request):
        payload = await request.json()
        result = parse_stk_callback(payload)
        if result is None:
            return {"ResultCode": 0, "ResultDesc": "Ignored"}

        conv = rt.store.get_by_checkout(result.checkout_request_id)
        if conv and conv.payment:
            conv.payment.status = result.status
            if result.receipt:
                conv.payment.receipt = result.receipt
            if result.status == PaymentStatus.SUCCESS and conv.order:
                conv.order.status = OrderStatus.PAID
            rt.store.save(conv)
        # Daraja expects this acknowledgement shape.
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    return app


# Module-level app for `uvicorn hermes.app:app`.
app = create_app()
