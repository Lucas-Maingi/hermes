"""FastAPI application: the real connectable surface.

Endpoints:
  GET  /health              liveness + which providers are live (real vs simulated)
  POST /chat                drive the agent from the web simulator or any client
  GET  /webhook             Meta WhatsApp Cloud API verification handshake
  POST /webhook             Meta inbound messages -> agent -> WhatsApp reply
  POST /mpesa/callback      Daraja STK callback -> mark payment paid + receipt
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import re
import smtplib
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse, HTMLResponse

from hermes import whatsapp
from hermes.callbacks import parse_stk_callback
from hermes.document_loader import extract_document_text
from hermes.mailer import send_verification_email, smtp_is_configured
from hermes.models import OrderStatus, PaymentStatus, Role
from hermes.mpesa.base import normalize_phone
from hermes.rag import build_retriever
from hermes.runtime import Runtime
from hermes.worker import process_outbound_queue

logger = logging.getLogger(__name__)


def _validate_provider_url(value: str, label: str) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label} is not a valid URL") from exc
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=400,
            detail=f"{label} must be an HTTPS URL with a public hostname",
        )
    if (
        hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal"))
        or hostname == "metadata.google.internal"
    ):
        raise HTTPException(status_code=400, detail=f"{label} cannot target a local host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise HTTPException(status_code=400, detail=f"{label} cannot target a private IP address")


class ChatRequest(BaseModel):
    text: str = Field(..., description="The customer's message")
    phone: str = Field("254700000000", description="Customer phone (MSISDN)")
    conversation_id: str | None = Field(None, description="Continue an existing conversation")


class RegisterRequest(BaseModel):
    invite_code: str = ""
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    email: str
    password: str


class EmailVerificationRequest(BaseModel):
    token: str = Field(min_length=20, max_length=256)


class ResendVerificationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class CatalogRequest(BaseModel):
    products: list[dict] = Field(max_length=1000)
    info: dict[str, str] = Field(default_factory=dict)

class InventoryAdjustmentRequest(BaseModel):
    product_name: str = Field(min_length=1, max_length=120)
    quantity: int = Field(ge=0, le=10_000_000)
    reason: str = Field(min_length=1, max_length=500)


class BookingRequest(BaseModel):
    service: str = Field(min_length=1, max_length=120)
    customer_name: str = Field(min_length=1, max_length=120)
    customer_phone: str = Field(min_length=1, max_length=40)
    starts_at: datetime
    duration_minutes: int = Field(ge=5, le=1440)
    notes: str = Field(default="", max_length=2000)


class BookingSettingsRequest(BaseModel):
    timezone: str = Field(min_length=1, max_length=100)
    weekly_hours: dict[str, dict[str, str]]


class PublicBookingRequest(BaseModel):
    service: str = Field(min_length=1, max_length=120)
    customer_name: str = Field(min_length=1, max_length=120)
    customer_phone: str = Field(min_length=8, max_length=40)
    starts_at: datetime
    notes: str = Field(default="", max_length=2000)


class PublicBookingCancellationRequest(BaseModel):
    phone: str = Field(min_length=8, max_length=40)


class BookingStatusRequest(BaseModel):
    status: str


class WhatsAppTemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    language: str = Field(default="en", max_length=8)
    category: str
    body_parameter_count: int = Field(ge=0, le=10)
    meta_approval_confirmed: bool


class WhatsAppTemplateStatusRequest(BaseModel):
    meta_approval_confirmed: bool


class WhatsAppConsentRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=40)
    opted_in: bool
    source: str = Field(min_length=1, max_length=500)


class WhatsAppFollowupRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=40)
    template_id: str
    parameters: list[str] = Field(default_factory=list, max_length=10)
    send_at: datetime


class SubscriptionCheckoutRequest(BaseModel):
    phone: str = Field(min_length=8, max_length=40)


class CampaignRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    channel: str = Field(default="whatsapp")
    external_source_id: str = Field(min_length=1, max_length=200)


class IntegrationRequest(BaseModel):
    whatsapp_phone_number_id: str = ""
    whatsapp_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = ""
    mpesa_credentials: dict[str, str] = Field(default_factory=dict)
    llm_credentials: dict[str, str] = Field(default_factory=dict)
    embedding_consent: bool = False
    llm_data_consent: bool = False


class KnowledgeDocumentRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100_000)
    consent_external_embedding: bool = False


MAX_KNOWLEDGE_UPLOAD_BYTES = 15 * 1024 * 1024


class HandoffUpdateRequest(BaseModel):
    status: str
    assigned_to: str | None = None


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


def _session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        "hermes_session",
        token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        max_age=60 * 60 * 24 * 7,
        path="/",
    )


def _current_user(request: Request, rt: Runtime) -> dict[str, str]:
    token = request.cookies.get("hermes_session")
    user = rt.platform.session_user(token) if token else None
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Please sign in")
    origin = request.headers.get("origin")
    if origin and request.url.hostname:
        from urllib.parse import urlparse

        parsed = urlparse(origin)
        if parsed.hostname != request.url.hostname:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request origin")
    return user


def _require_entitlement(request: Request, rt: Runtime, feature: str) -> dict[str, str]:
    user = _current_user(request, rt)
    if not rt.platform.subscription(user["business_id"])["premium_active"]:
        raise HTTPException(
            status_code=402,
            detail=f"{feature} requires Premium. Renew your subscription to continue.",
        )
    return user


def _enforce_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    try:
        origin_host = urlsplit(origin).hostname
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid request origin") from exc
    if not origin_host or origin_host.casefold() != (request.url.hostname or "").casefold():
        raise HTTPException(status_code=403, detail="Invalid request origin")


def _enforce_public_rate_limit(
    request: Request,
    rt: Runtime,
    bucket_prefix: str,
    *,
    limit: int,
    window_seconds: int,
) -> None:
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = rt.platform.consume_auth_attempt(
        f"{bucket_prefix}:{client_ip}",
        limit=limit,
        window_seconds=window_seconds,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests; try again later.",
            headers={"Retry-After": str(retry_after)},
        )


def _enforce_auth_rate_limit(request: Request, rt: Runtime, action: str, identifier: str) -> None:
    client_ip = request.client.host if request.client else "unknown"
    limits = (
        ((f"{action}:ip:{client_ip}", 25, 3600), (f"{action}:email:{identifier.strip().lower()}", 3, 3600))
        if action == "register"
        else ((f"{action}:ip:{client_ip}", 50, 900), (f"{action}:email:{identifier.strip().lower()}", 10, 900))
    )
    for bucket, limit, window_seconds in limits:
        allowed, retry_after = rt.platform.consume_auth_attempt(
            bucket,
            limit=limit,
            window_seconds=window_seconds,
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please wait before trying again.",
                headers={"Retry-After": str(retry_after)},
            )


def _conversation_summary(conversation) -> dict:
    last_message = conversation.messages[-1] if conversation.messages else None
    return {
        "id": conversation.id,
        "customer_phone": conversation.customer_phone,
        "channel": conversation.channel,
        "needs_human": conversation.needs_human,
        "last_message": (
            {"role": last_message.role.value, "text": last_message.text, "timestamp": last_message.timestamp}
            if last_message
            else None
        ),
        "order": (
            {
                "items": [
                    {"name": item.name, "quantity": item.quantity, "unit_price": item.unit_price}
                    for item in conversation.order.items
                ],
                "total": conversation.order.total,
                "status": conversation.order.status.value,
                "delivery_note": conversation.order.delivery_note,
            }
            if conversation.order
            else None
        ),
        "payment": (
            {"status": conversation.payment.status.value, "amount": conversation.payment.amount}
            if conversation.payment
            else None
        ),
        "created_at": conversation.created_at,
    }


def _record_paid_campaign_conversion(rt: Runtime, conversation) -> None:
    if conversation.order is None or conversation.order.status != OrderStatus.PAID:
        return
    rt.platform.record_campaign_conversion(
        conversation.business_id,
        conversation.id,
        order_id=conversation.order.id,
        revenue=conversation.order.total,
    )


def create_app(
    runtime: Runtime | None = None,
    *,
    verification_email_sender: Callable[[str, str], None] | None = None,
) -> FastAPI:
    rt = runtime or Runtime()
    email_sender = verification_email_sender or send_verification_email
    app = FastAPI(title="Hermes", description="WhatsApp AI commerce & support agent for SMEs.", version="0.1.0")
    app.state.rt = rt

    def verification_base_url(request: Request) -> str:
        base_url = os.getenv("HERMES_PUBLIC_BASE_URL", "").rstrip("/")
        if not base_url:
            if os.getenv("HERMES_ENVIRONMENT", "").lower() == "production":
                raise HTTPException(status_code=503, detail="Public HTTPS application URL is not configured")
            base_url = str(request.base_url).rstrip("/")
        try:
            parsed_base_url = urlsplit(base_url)
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail="Verification requires a valid public HTTPS application URL",
            ) from exc
        local_verification = parsed_base_url.hostname in {"localhost", "127.0.0.1"} or (
            verification_email_sender is not None and parsed_base_url.hostname == "testserver"
        )
        if (
            not parsed_base_url.hostname
            or parsed_base_url.username
            or parsed_base_url.password
            or parsed_base_url.query
            or parsed_base_url.fragment
            or (parsed_base_url.scheme != "https" and not local_verification)
        ):
            raise HTTPException(status_code=503, detail="Verification requires a valid public HTTPS application URL")
        return base_url

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(Path(__file__).with_name("dashboard.html"))

    @app.get("/book/{business_id}", response_class=HTMLResponse, include_in_schema=False)
    def public_booking_page(business_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", business_id):
            raise HTTPException(status_code=404, detail="Booking page is not available")
        try:
            subscription = rt.platform.subscription(business_id)
            settings = rt.platform.booking_settings(business_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Booking page is not available") from exc
        if not subscription["premium_active"] or not settings["enabled"]:
            raise HTTPException(status_code=404, detail="Booking page is not available")
        template = Path(__file__).with_name("booking.html").read_text(encoding="utf-8")
        return HTMLResponse(template.replace("__BUSINESS_ID__", business_id))

    @app.get("/public/config")
    def public_config():
        invite_required = bool(os.getenv("HERMES_INVITE_TOKEN", ""))
        mail_ready = verification_email_sender is not None or smtp_is_configured()
        return {
            "invite_required": invite_required,
            "signup_enabled": mail_ready
            and (invite_required or os.getenv("HERMES_PUBLIC_SIGNUP_ENABLED", "").lower() == "true"),
            "email_verification_required": True,
            "premium_trial_days": 7,
            "premium_monthly_price_kes": 2500,
        }

    @app.post("/auth/register", status_code=202)
    def register(req: RegisterRequest, request: Request):
        _enforce_auth_rate_limit(request, rt, "register", req.email)
        if verification_email_sender is None and not smtp_is_configured():
            raise HTTPException(status_code=503, detail="Email verification delivery is not configured")
        base_url = verification_base_url(request)
        invite = os.getenv("HERMES_INVITE_TOKEN", "")
        if invite:
            if not hmac.compare_digest(req.invite_code, invite):
                raise HTTPException(status_code=403, detail="Invalid invitation code")
        elif os.getenv("HERMES_PUBLIC_SIGNUP_ENABLED", "").lower() != "true":
            raise HTTPException(status_code=503, detail="Self-serve registration is not enabled")
        try:
            user_id, business_id = rt.platform.create_business(
                name=req.name,
                email=req.email,
                defer_trial_until_verified=True,
                password=req.password,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        token = rt.platform.create_email_verification_token(user_id)
        try:
            email_sender(req.email.strip().lower(), f"{base_url}/?verify={token}")
        except (OSError, RuntimeError, ValueError, smtplib.SMTPException) as exc:
            logger.exception("Could not send account verification email")
            raise HTTPException(status_code=503, detail="Verification email could not be sent; try again shortly") from exc
        return {"status": "verification_required"}

    @app.post("/auth/verify-email")
    def verify_email(req: EmailVerificationRequest, request: Request, response: Response):
        verified = rt.platform.verify_email_token(req.token)
        if verified is None:
            raise HTTPException(status_code=400, detail="Verification link is invalid or expired")
        user_id, business_id = verified
        business = rt.platform.business(business_id)
        if business is None:
            raise HTTPException(status_code=500, detail="Verified workspace could not be loaded")
        rt.businesses[business_id] = business
        if rt.default_business.id == business_id:
            rt.default_business = business
        token = rt.platform.create_session(user_id)
        _session_cookie(response, request, token)
        return {"business_id": business_id, "status": "verified"}

    @app.post("/auth/verification/resend", status_code=202)
    def resend_verification(req: ResendVerificationRequest, request: Request):
        _enforce_auth_rate_limit(request, rt, "verification", req.email)
        if verification_email_sender is None and not smtp_is_configured():
            raise HTTPException(status_code=503, detail="Email verification delivery is not configured")
        base_url = verification_base_url(request)
        pending = rt.platform.create_email_verification_for_email(req.email)
        if pending is not None:
            _, token = pending
            try:
                email_sender(req.email.strip().lower(), f"{base_url}/?verify={token}")
            except (OSError, RuntimeError, ValueError, smtplib.SMTPException) as exc:
                logger.exception("Could not resend account verification email")
                raise HTTPException(status_code=503, detail="Verification email could not be sent; try again shortly") from exc
        return {"status": "if_unverified_email_sent"}

    @app.post("/auth/login")
    def login(req: LoginRequest, request: Request, response: Response):
        _enforce_auth_rate_limit(request, rt, "login", req.email)
        authenticated = rt.platform.authenticate(req.email, req.password)
        if authenticated is None:
            raise HTTPException(status_code=401, detail="Email or password is incorrect")
        user_id, business_id = authenticated
        token = rt.platform.create_session(user_id)
        _session_cookie(response, request, token)
        return {"business_id": business_id}

    @app.post("/auth/logout", status_code=204)
    def logout(request: Request, response: Response):
        token = request.cookies.get("hermes_session")
        if token:
            rt.platform.delete_session(token)
        response.delete_cookie("hermes_session", path="/", httponly=True, samesite="strict")

    @app.get("/api/dashboard")
    def dashboard_data(request: Request):
        user = _current_user(request, rt)
        business = rt.platform.business(user["business_id"])
        if business is None:
            raise HTTPException(status_code=404, detail="Merchant not found")
        conversations = rt.store.list_conversations(business_id=business.id, limit=100)
        return {
            "user": {"email": user["email"], "role": user["role"]},
            "subscription": rt.platform.subscription(business.id),
            "billing_status": rt.platform.billing_status(business.id),
            "business": {
                "id": business.id,
                "name": business.name,
                "currency": business.currency,
                "products": [
                    {
                        "name": product.name,
                        "price": product.price,
                        "unit": product.unit,
                        "aliases": product.aliases,
                        "in_stock": product.in_stock,
                        "stock_quantity": product.stock_quantity,
                    }
                    for product in business.products
                ],
                "info": business.info,
            },
            "metrics": rt.store.metrics(business_id=business.id),
            "conversations": [_conversation_summary(conversation) for conversation in conversations],
            "handoffs": rt.platform.list_handoffs(business.id),
            "inventory": rt.platform.inventory(business.id),
            "inventory_history": rt.platform.inventory_history(business.id, limit=30),
            "bookings": rt.platform.list_bookings(business.id, limit=100),
            "booking_settings": rt.platform.booking_settings(business.id),
            "campaigns": rt.platform.campaigns(business.id),
            "whatsapp_templates": rt.platform.whatsapp_templates(business.id),
            "whatsapp_followups": rt.platform.list_followups(business.id, limit=100),
            "documents": rt.platform.documents(business.id),
            "integrations": rt.platform.integration_status(business.id),
        }

    @app.post("/api/simulator/chat")
    def simulator_chat(req: ChatRequest, request: Request):
        user = _require_entitlement(request, rt, "AI assistant")
        business = rt.platform.business(user["business_id"])
        if business is None:
            raise HTTPException(status_code=404, detail="Merchant not found")
        conversation = (
            rt.store.get(
                req.conversation_id,
                business_id=business.id,
                customer_phone=req.phone,
            )
            if req.conversation_id
            else None
        )
        conversation = conversation or rt.continue_or_start(req.phone, "simulator", business)
        reply = rt.agent_for(business).handle_message(conversation, req.text)
        _record_paid_campaign_conversion(rt, conversation)
        rt.store.save(conversation)
        if conversation.needs_human:
            rt.platform.ensure_handoff(business.id, conversation.id, "Agent requested human review")
        return {
            "conversation_id": conversation.id,
            "reply": reply,
            "needs_human": conversation.needs_human,
            "order_total": conversation.order.total if conversation.order else 0.0,
        }

    @app.put("/api/catalog")
    def update_catalog(req: CatalogRequest, request: Request):
        user = _current_user(request, rt)
        try:
            rt.platform.save_catalog(user["business_id"], products=req.products, info=req.info)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        business = rt.platform.business(user["business_id"])
        if business is not None:
            rt.businesses[business.id] = business
            if rt.default_business.id == business.id:
                rt.default_business = business
        return {"status": "saved"}

    @app.put("/api/inventory")
    def update_inventory(req: InventoryAdjustmentRequest, request: Request):
        user = _require_entitlement(request, rt, "Inventory management")
        try:
            result = rt.platform.set_inventory(
                user["business_id"],
                req.product_name,
                req.quantity,
                req.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Merchant not found") from exc
        business = rt.platform.business(user["business_id"])
        if business:
            rt.businesses[business.id] = business
            if rt.default_business.id == business.id:
                rt.default_business = business
        return result

    @app.put("/api/bookings/settings")
    def update_booking_settings(req: BookingSettingsRequest, request: Request):
        user = _require_entitlement(request, rt, "Bookings")
        try:
            return rt.platform.save_booking_settings(
                user["business_id"],
                timezone_name=req.timezone,
                weekly_hours=req.weekly_hours,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/public/bookings/{business_id}/availability")
    def public_booking_availability(business_id: str, day: date, request: Request):
        _enforce_public_rate_limit(
            request,
            rt,
            f"public-booking-availability:{business_id}",
            limit=60,
            window_seconds=3600,
        )
        try:
            subscription = rt.platform.subscription(business_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Booking page is not available") from exc
        if not subscription["premium_active"]:
            raise HTTPException(status_code=404, detail="Booking page is not available")
        try:
            settings = rt.platform.booking_settings(business_id)
            if not settings["enabled"]:
                raise HTTPException(status_code=404, detail="Booking page is not configured")
            return {
                "timezone": settings["timezone"],
                "slot_duration_minutes": settings["slot_duration_minutes"],
                "slots": rt.platform.available_booking_slots(business_id, day),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Booking page is not available") from exc

    @app.post("/public/bookings/{business_id}", status_code=201)
    def public_create_booking(business_id: str, req: PublicBookingRequest, request: Request):
        _enforce_same_origin(request)
        _enforce_public_rate_limit(
            request,
            rt,
            f"public-booking:{business_id}",
            limit=10,
            window_seconds=3600,
        )
        try:
            subscription = rt.platform.subscription(business_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Booking page is not available") from exc
        if not subscription["premium_active"]:
            raise HTTPException(status_code=404, detail="Booking page is not available")
        if req.starts_at.tzinfo is None or req.starts_at.utcoffset() is None:
            raise HTTPException(status_code=400, detail="Booking time must include a timezone")
        starts_at = req.starts_at.astimezone(timezone.utc).timestamp()
        if starts_at <= datetime.now(timezone.utc).timestamp():
            raise HTTPException(status_code=400, detail="Booking must be in the future")
        try:
            customer_phone = rt.platform.normalize_whatsapp_phone(req.customer_phone)
            booking = rt.platform.create_booking(
                business_id,
                service=req.service,
                customer_name=req.customer_name,
                customer_phone=customer_phone,
                starts_at=starts_at,
                duration_minutes=30,
                notes=req.notes,
                require_business_hours=True,
            )
        except ValueError as exc:
            conflict = "booking" in str(exc).casefold() and "another" in str(exc).casefold()
            raise HTTPException(
                status_code=409 if conflict else 400,
                detail=str(exc),
            ) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Booking page is not available") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="Customer data encryption is not configured") from exc
        return {"id": booking["id"], "service": booking["service"], "starts_at": booking["starts_at"]}

    @app.post("/public/bookings/{business_id}/{booking_id}/cancel")
    def public_cancel_booking(
        business_id: str,
        booking_id: str,
        req: PublicBookingCancellationRequest,
        request: Request,
    ):
        _enforce_same_origin(request)
        _enforce_public_rate_limit(
            request,
            rt,
            f"public-booking-cancel:{business_id}",
            limit=5,
            window_seconds=3600,
        )
        try:
            cancelled = rt.platform.cancel_customer_booking(business_id, booking_id, req.phone)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Enter the phone number used for this booking") from exc
        if not cancelled:
            raise HTTPException(status_code=404, detail="Booking not found or no longer cancellable")
        return {"status": "cancelled"}

    @app.post("/api/bookings", status_code=201)
    def create_booking(req: BookingRequest, request: Request):
        user = _require_entitlement(request, rt, "Bookings")
        if req.starts_at.tzinfo is None or req.starts_at.utcoffset() is None:
            raise HTTPException(status_code=400, detail="Booking time must include a timezone")
        starts_at = req.starts_at.astimezone(timezone.utc).timestamp()
        if starts_at <= datetime.now(timezone.utc).timestamp():
            raise HTTPException(status_code=400, detail="Booking must be in the future")
        try:
            booking = rt.platform.create_booking(
                user["business_id"],
                service=req.service,
                customer_name=req.customer_name,
                customer_phone=req.customer_phone,
                starts_at=starts_at,
                duration_minutes=req.duration_minutes,
                notes=req.notes,
            )
        except ValueError as exc:
            conflict = "booking" in str(exc).casefold() and "another" in str(exc).casefold()
            raise HTTPException(status_code=409 if conflict else 400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Merchant not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="Customer data encryption is not configured") from exc
        return booking

    @app.patch("/api/bookings/{booking_id}")
    def update_booking(booking_id: str, req: BookingStatusRequest, request: Request):
        user = _require_entitlement(request, rt, "Bookings")
        try:
            updated = rt.platform.update_booking_status(user["business_id"], booking_id, req.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not updated:
            raise HTTPException(status_code=404, detail="Booking not found")
        return {"status": "updated"}

    @app.post("/api/campaigns", status_code=201)
    def create_campaign(req: CampaignRequest, request: Request):
        user = _require_entitlement(request, rt, "Campaign attribution")
        try:
            campaign_id = rt.platform.create_campaign(
                user["business_id"],
                name=req.name,
                channel=req.channel,
                external_source_id=req.external_source_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": campaign_id}

    @app.post("/api/whatsapp/templates", status_code=201)
    def create_whatsapp_template(req: WhatsAppTemplateRequest, request: Request):
        user = _require_entitlement(request, rt, "WhatsApp automation")
        if not req.meta_approval_confirmed:
            raise HTTPException(status_code=400, detail="Only add templates that Meta has approved")
        try:
            template_id = rt.platform.save_whatsapp_template(
                user["business_id"],
                name=req.name,
                language=req.language,
                category=req.category,
                body_parameter_count=req.body_parameter_count,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": template_id, "status": "approved"}

    @app.patch("/api/whatsapp/templates/{template_id}")
    def update_whatsapp_template(template_id: str, req: WhatsAppTemplateStatusRequest, request: Request):
        user = _require_entitlement(request, rt, "WhatsApp automation")
        if not rt.platform.set_whatsapp_template_active(
            user["business_id"],
            template_id,
            req.meta_approval_confirmed,
        ):
            raise HTTPException(status_code=404, detail="WhatsApp template not found")
        return {"status": "approved" if req.meta_approval_confirmed else "disabled"}

    @app.post("/api/whatsapp/consent", status_code=204)
    def record_whatsapp_consent(req: WhatsAppConsentRequest, request: Request):
        user = _require_entitlement(request, rt, "WhatsApp automation")
        try:
            rt.platform.record_whatsapp_consent(
                user["business_id"],
                req.phone,
                opted_in=req.opted_in,
                source=req.source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Merchant not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="Customer data encryption is not configured") from exc

    @app.post("/api/whatsapp/follow-ups", status_code=201)
    def schedule_whatsapp_followup(req: WhatsAppFollowupRequest, request: Request):
        user = _require_entitlement(request, rt, "WhatsApp automation")
        if req.send_at.tzinfo is None or req.send_at.utcoffset() is None:
            raise HTTPException(status_code=400, detail="Follow-up time must include a timezone")
        send_at = req.send_at.astimezone(timezone.utc).timestamp()
        if send_at <= datetime.now(timezone.utc).timestamp():
            raise HTTPException(status_code=400, detail="Follow-up time must be in the future")
        try:
            followup_id = rt.platform.schedule_followup(
                user["business_id"],
                template_id=req.template_id,
                phone=req.phone,
                parameters=req.parameters,
                send_at=send_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="Customer data encryption is not configured") from exc
        return {"id": followup_id, "status": "pending"}

    @app.post("/api/billing/checkout")
    def subscription_checkout(req: SubscriptionCheckoutRequest, request: Request):
        user = _current_user(request, rt)
        try:
            client = rt.billing_mpesa()
        except ValueError as exc:
            logger.exception("Hermes subscription M-Pesa configuration is invalid")
            raise HTTPException(status_code=503, detail="Hermes subscription M-Pesa billing is misconfigured") from exc
        if client is None:
            raise HTTPException(status_code=503, detail="Hermes subscription M-Pesa billing is not configured")
        try:
            phone = normalize_phone(req.phone)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Enter a valid Kenyan M-Pesa phone number") from exc
        if not re.fullmatch(r"254[17]\d{8}", phone):
            raise HTTPException(status_code=400, detail="Enter a valid Kenyan M-Pesa phone number")
        subscription = rt.platform.subscription(user["business_id"])
        amount = subscription["premium_monthly_price_kes"]
        try:
            intent_id = rt.platform.create_billing_intent(user["business_id"], phone, amount)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="Customer data encryption is not configured") from exc
        try:
            result = client.stk_push(
                phone=phone,
                amount=amount,
                account_reference=f"H{user['business_id'][:10]}",
                description="Hermes Premium monthly subscription",
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            rt.platform.fail_billing_payment(intent_id)
            logger.exception("Hermes subscription STK push errored")
            raise HTTPException(status_code=502, detail="M-Pesa subscription request could not be started") from exc
        if not result.success or not result.checkout_request_id:
            rt.platform.fail_billing_payment(intent_id)
            logger.error("Hermes subscription STK push failed")
            raise HTTPException(status_code=502, detail="M-Pesa subscription request could not be started")
        if not rt.platform.attach_billing_checkout(intent_id, result.checkout_request_id):
            rt.platform.fail_billing_payment(intent_id)
            raise HTTPException(status_code=502, detail="M-Pesa payment could not be linked to this workspace")
        return {
            "status": "pending",
            "amount_kes": amount,
            "checkout_request_id": result.checkout_request_id,
            "message": result.customer_message or "Check your phone and enter your M-Pesa PIN to pay.",
        }

    @app.get("/api/billing/status")
    def billing_status(request: Request):
        user = _current_user(request, rt)
        return rt.platform.billing_status(user["business_id"])

    @app.delete("/api/whatsapp/follow-ups/{followup_id}", status_code=204)
    def cancel_whatsapp_followup(followup_id: str, request: Request):
        user = _require_entitlement(request, rt, "WhatsApp automation")
        if not rt.platform.cancel_followup(user["business_id"], followup_id):
            raise HTTPException(status_code=404, detail="Pending follow-up not found")

    @app.put("/api/integrations")
    def update_integrations(req: IntegrationRequest, request: Request):
        user = _current_user(request, rt)
        business_id = user["business_id"]
        try:
            current = rt.platform.integrations(business_id)
        except RuntimeError as exc:
            if "HERMES_ENCRYPTION_KEY" not in str(exc):
                raise
            current = {
                "whatsapp_token": "",
                "whatsapp_app_secret": "",
                "whatsapp_verify_token": "",
                "mpesa": {},
                "llm": {},
                "embedding_consent": False,
                "llm_data_consent": False,
            }
        whatsapp_token = req.whatsapp_token or current["whatsapp_token"]
        whatsapp_app_secret = req.whatsapp_app_secret or current["whatsapp_app_secret"]
        whatsapp_verify_token = req.whatsapp_verify_token or current["whatsapp_verify_token"]
        mpesa = {
            key: value or current["mpesa"].get(key, "")
            for key, value in req.mpesa_credentials.items()
        }
        for key, value in current["mpesa"].items():
            mpesa.setdefault(key, value)
        llm = {
            key: value or current["llm"].get(key, "")
            for key, value in req.llm_credentials.items()
        }
        for key, value in current["llm"].items():
            llm.setdefault(key, value)
        for key in ("embedding_base_url", "chat_base_url"):
            if llm.get(key):
                _validate_provider_url(llm[key], key.replace("_", " ").capitalize())
        callback_url = mpesa.get("MPESA_CALLBACK_URL", "")
        if callback_url:
            _validate_provider_url(callback_url, "M-Pesa callback URL")
            if not urlsplit(callback_url).path.rstrip("/").endswith("/mpesa/callback"):
                raise HTTPException(status_code=400, detail="M-Pesa callback URL must target /mpesa/callback")
        try:
            rt.platform.save_integrations(
                business_id,
                whatsapp_phone_number_id=req.whatsapp_phone_number_id
                or current.get("whatsapp_phone_number_id", ""),
                whatsapp_token=whatsapp_token,
                whatsapp_app_secret=whatsapp_app_secret,
                whatsapp_verify_token=whatsapp_verify_token,
                mpesa_credentials=mpesa,
                llm_credentials=llm,
                embedding_consent=req.embedding_consent,
                llm_data_consent=req.llm_data_consent,
            )
            rt._mpesa_clients.pop(business_id, None)
            business = rt.platform.business(business_id)
            if business:
                rt.businesses[business_id] = business
        except (RuntimeError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "saved"}

    @app.post("/api/integrations/rehearse")
    def rehearse_integrations(request: Request):
        user = _current_user(request, rt)
        try:
            integrations = rt.platform.integrations(user["business_id"])
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="Integration credentials cannot be decrypted") from exc
        outcome = {"whatsapp": "not_configured", "mpesa": "not_configured"}
        if integrations["whatsapp_phone_number_id"] and integrations["whatsapp_token"]:
            try:
                connected = whatsapp.check_connection(
                    integrations["whatsapp_phone_number_id"],
                    integrations["whatsapp_token"],
                )
                outcome["whatsapp"] = "connected" if connected else "rejected"
            except (httpx.HTTPError, RuntimeError, ValueError):
                logger.exception("WhatsApp read-only credential rehearsal failed")
                outcome["whatsapp"] = "failed"
        credentials = integrations["mpesa"]
        if all(
            credentials.get(key)
            for key in ("MPESA_CONSUMER_KEY", "MPESA_CONSUMER_SECRET")
        ):
            from hermes.mpesa.daraja import DarajaClient

            business = rt.platform.business(user["business_id"])
            if business is None:
                raise HTTPException(status_code=404, detail="Merchant not found")
            provider = rt.mpesa_for(business)
            if isinstance(provider, DarajaClient):
                try:
                    outcome["mpesa"] = "connected" if provider.verify_credentials() else "rejected"
                except (httpx.HTTPError, RuntimeError, ValueError):
                    logger.exception("M-Pesa OAuth credential rehearsal failed")
                    outcome["mpesa"] = "failed"
        return {
            **outcome,
            "side_effects": "Read-only Meta account lookup and Daraja OAuth only; no messages or payment prompts sent.",
        }

    @app.post("/api/knowledge/documents")
    def index_knowledge_document(req: KnowledgeDocumentRequest, request: Request):
        user = _current_user(request, rt)
        try:
            retriever = build_retriever(rt.platform, user["business_id"])
            if retriever.embedding_client and not req.consent_external_embedding:
                raise HTTPException(
                    status_code=400,
                    detail="External embeddings are configured. Consent is required before document text is sent.",
                )
            document_id = retriever.index_document(user["business_id"], req.title, req.text)
        except HTTPException:
            raise
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            logger.exception("Knowledge document indexing failed")
            raise HTTPException(status_code=502, detail="Document indexing failed; check embedding configuration") from exc
        return {"id": document_id}

    @app.post("/api/knowledge/upload", status_code=201)
    async def upload_knowledge_document(request: Request):
        user = _current_user(request, rt)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                body_size = int(content_length)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Content-Length header.") from exc
            if body_size > MAX_KNOWLEDGE_UPLOAD_BYTES + 1024 * 1024:
                raise HTTPException(status_code=413, detail="File exceeds the 15 MB upload limit.")
        async with request.form(max_files=1, max_fields=1, max_part_size=16 * 1024) as form:
            file = form.get("file")
            if not isinstance(file, UploadFile):
                raise HTTPException(status_code=400, detail="Choose one file to upload.")
            if file.size is not None and file.size > MAX_KNOWLEDGE_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="File exceeds the 15 MB upload limit.")
            content = await file.read(MAX_KNOWLEDGE_UPLOAD_BYTES + 1)
            filename = file.filename or ""
            consent_external_embedding = str(form.get("consent_external_embedding", "")).lower() in {
                "true",
                "1",
                "yes",
                "on",
            }
        if len(content) > MAX_KNOWLEDGE_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File exceeds the 15 MB upload limit.")
        try:
            title, text = extract_document_text(filename, content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            retriever = build_retriever(rt.platform, user["business_id"])
            if retriever.embedding_client and not consent_external_embedding:
                raise HTTPException(
                    status_code=400,
                    detail="External embeddings are configured. Consent is required before document text is sent.",
                )
            document_id = retriever.index_document(user["business_id"], title, text)
        except HTTPException:
            raise
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            logger.exception("Knowledge file indexing failed")
            raise HTTPException(status_code=502, detail="File indexing failed; check embedding configuration.") from exc
        return {"id": document_id, "title": title, "characters_indexed": len(text)}

    @app.post("/api/knowledge/search")
    def search_knowledge(req: KnowledgeSearchRequest, request: Request):
        user = _current_user(request, rt)
        try:
            results = build_retriever(rt.platform, user["business_id"]).search(
                user["business_id"],
                req.query,
                limit=5,
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            logger.exception("Knowledge search failed")
            raise HTTPException(status_code=502, detail="Knowledge search failed") from exc
        return {"results": results}

    @app.delete("/api/knowledge/documents/{document_id}", status_code=204)
    def delete_knowledge_document(document_id: str, request: Request):
        user = _current_user(request, rt)
        if not rt.platform.delete_document(user["business_id"], document_id):
            raise HTTPException(status_code=404, detail="Document not found")

    @app.patch("/api/handoffs/{handoff_id}")
    def update_handoff(handoff_id: str, req: HandoffUpdateRequest, request: Request):
        user = _current_user(request, rt)
        try:
            updated = rt.platform.update_handoff(
                user["business_id"],
                handoff_id,
                req.status,
                req.assigned_to or user["email"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not updated:
            raise HTTPException(status_code=404, detail="Handoff not found")
        return {"status": "updated"}

    @app.get("/api/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, request: Request):
        user = _current_user(request, rt)
        conversation = rt.store.get(conversation_id, business_id=user["business_id"])
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        summary = _conversation_summary(conversation)
        summary["messages"] = [
            {"role": message.role.value, "text": message.text, "timestamp": message.timestamp}
            for message in conversation.messages
        ]
        return summary

    @app.delete("/api/account", status_code=204)
    def delete_account(request: Request, response: Response):
        user = _current_user(request, rt)
        business_id = user["business_id"]
        rt.platform.delete_business_data(business_id)
        rt.businesses.pop(business_id, None)
        rt._mpesa_clients.pop(business_id, None)
        token = request.cookies.get("hermes_session")
        if token:
            rt.platform.delete_session(token)
        response.delete_cookie("hermes_session", path="/", httponly=True, samesite="strict")

    @app.post("/api/maintenance/purge-expired")
    def purge_expired(request: Request):
        _current_user(request, rt)
        try:
            retention_days = int(os.getenv("HERMES_RETENTION_DAYS", "365"))
        except ValueError as exc:
            raise HTTPException(status_code=500, detail="Invalid retention configuration") from exc
        if retention_days < 1:
            raise HTTPException(status_code=500, detail="Retention must be at least one day")
        deleted = rt.platform.purge_expired_conversations(retention_days)
        return {"deleted_conversations": deleted}

    @app.get("/health")
    def health():
        return {
            "status": "healthy",
            "llm_provider": type(rt.llm).__name__,
            "mpesa_provider": type(rt.mpesa).__name__,
            "business_count": len(rt.businesses),
        }

    @app.post("/chat")
    def chat(req: ChatRequest, authorization: str | None = Header(default=None)):
        configured_token = os.getenv("HERMES_CHAT_API_TOKEN", "")
        if not configured_token:
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        if not authorization or not authorization.startswith("Bearer "):
            return Response(status_code=status.HTTP_401_UNAUTHORIZED)
        supplied_token = authorization.removeprefix("Bearer ")
        if not hmac.compare_digest(supplied_token, configured_token):
            return Response(status_code=status.HTTP_401_UNAUTHORIZED)

        business = rt.default_business
        if req.conversation_id:
            conv = rt.store.get(
                req.conversation_id,
                business_id=business.id,
                customer_phone=req.phone,
            ) or rt.continue_or_start(req.phone, "simulator", business)
        else:
            conv = rt.continue_or_start(req.phone, "simulator", business)

        agent = rt.agent_for(business)
        reply = agent.handle_message(conv, req.text)
        _record_paid_campaign_conversion(rt, conv)
        rt.store.save(conv)
        if conv.needs_human:
            rt.platform.ensure_handoff(business.id, conv.id, "Agent requested human review")

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
        token = params.get("hub.verify_token")
        verified = rt.platform.verify_whatsapp_token(token or "")
        if not verified and len(rt.businesses) == 1:
            verified = (
                whatsapp.verify_webhook(
                    params.get("hub.mode"),
                    token,
                    params.get("hub.challenge"),
                )
                is not None
            )
        challenge = (
            params.get("hub.challenge")
            if params.get("hub.mode") == "subscribe" and verified
            else None
        )
        if challenge is None:
            return Response(status_code=status.HTTP_403_FORBIDDEN)
        return Response(content=challenge, media_type="text/plain")

    @app.post("/webhook")
    async def inbound(request: Request, background_tasks: BackgroundTasks):
        body = await request.body()
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid webhook JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Webhook payload must be a JSON object")
        phone_number_ids = {
            msg.phone_number_id for msg in whatsapp.parse_inbound(payload) if msg.phone_number_id
        }
        if not phone_number_ids:
            phone_number_ids = {
                value.get("metadata", {}).get("phone_number_id", "")
                for entry in payload.get("entry", [])
                for change in entry.get("changes", [])
                if isinstance((value := change.get("value", {})), dict)
            } - {""}
        if len(phone_number_ids) > 1 or (not phone_number_ids and len(rt.businesses) != 1):
            return Response(status_code=status.HTTP_403_FORBIDDEN)
        tenant = (
            rt.business_for_phone_number_id(next(iter(phone_number_ids)))
            if phone_number_ids
            else rt.default_business
        )
        app_secret: str | None = rt.platform.whatsapp_app_secret(tenant.id) if tenant else ""
        if tenant and not app_secret and len(rt.businesses) == 1:
            app_secret = None
        if not whatsapp.verify_signature(
            body,
            request.headers.get("X-Hub-Signature-256"),
            app_secret=app_secret,
        ):
            return Response(status_code=status.HTTP_403_FORBIDDEN)
        for msg in whatsapp.parse_inbound(payload):
            business = rt.business_for_phone_number_id(msg.phone_number_id)
            if business is None:
                logger.warning("Ignoring WhatsApp message for unconfigured phone-number ID")
                continue
            if not rt.platform.claim_inbound(business.id, msg.message_id):
                continue
            if msg.text.strip().casefold() in {"stop", "unsubscribe", "end", "quit"}:
                try:
                    rt.platform.record_whatsapp_consent(
                        business.id,
                        msg.from_phone,
                        opted_in=False,
                        source="Customer WhatsApp opt-out message",
                    )
                except ValueError:
                    logger.warning("Could not normalize opt-out phone for business_id=%s", business.id)
                except RuntimeError as exc:
                    rt.platform.finish_inbound(business.id, msg.message_id, "failed")
                    logger.exception("Could not persist WhatsApp opt-out for business_id=%s", business.id)
                    raise HTTPException(
                        status_code=503,
                        detail="Messaging preferences are temporarily unavailable",
                    ) from exc
                rt.platform.finish_inbound(business.id, msg.message_id, "done")
                continue
            conv = rt.continue_or_start(msg.from_phone, "whatsapp", business)
            conv.channel = "whatsapp"
            if msg.referral_source_id:
                rt.platform.attribute_campaign_conversation(
                    business.id,
                    conv.id,
                    msg.referral_source_id,
                )
            subscription = rt.platform.subscription(business.id)
            if subscription["premium_active"]:
                reply = rt.agent_for(business).handle_message(conv, msg.text)
                _record_paid_campaign_conversion(rt, conv)
            else:
                conv.add_message(Role.CUSTOMER, msg.text)
                conv.needs_human = True
                reply = "Thanks for your message. The shop team will follow up with you."
                conv.add_message(Role.HANDOFF, reply)
            rt.store.save(conv)
            if conv.needs_human:
                rt.platform.ensure_handoff(
                    business.id,
                    conv.id,
                    "Workspace plan requires a staff reply" if not subscription["premium_active"]
                    else "Agent requested human review",
                )
            rt.platform.enqueue_outbound(
                business.id,
                msg.from_phone,
                reply,
                msg.phone_number_id,
            )
            rt.platform.finish_inbound(business.id, msg.message_id, "done")
        background_tasks.add_task(process_outbound_queue, rt)
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
            payment = conv.payment
            business = rt.businesses.get(conv.business_id) or rt.platform.business(conv.business_id)
            if business is None:
                return {"ResultCode": 0, "ResultDesc": "Accepted"}
            provider_status = rt.mpesa_for(business).query_status(result.checkout_request_id)
            valid_callback = result.status == provider_status
            if result.status == PaymentStatus.SUCCESS:
                try:
                    payer_matches = result.phone is not None and normalize_phone(result.phone) == payment.phone
                except ValueError:
                    payer_matches = False
                valid_callback = valid_callback and (
                    result.amount is not None
                    and result.amount == payment.amount
                    and bool(result.receipt)
                    and payer_matches
                )
            if valid_callback and payment.status != PaymentStatus.SUCCESS:
                payment.status = result.status
                if result.receipt:
                    payment.receipt = result.receipt
                if result.status == PaymentStatus.SUCCESS and conv.order:
                    conv.order.status = OrderStatus.PAID
                    inventory_committed = rt.platform.commit_order_inventory(
                        conv.business_id,
                        conv.order.id,
                        [{"name": item.name, "quantity": item.quantity} for item in conv.order.items],
                    )
                    if not inventory_committed:
                        conv.needs_human = True
                        rt.platform.ensure_handoff(
                            conv.business_id,
                            conv.id,
                            "Payment succeeded after the inventory reservation expired or became unavailable",
                        )
                    rt.platform.record_campaign_conversion(
                        conv.business_id,
                        conv.id,
                        order_id=conv.order.id,
                        revenue=conv.order.total,
                    )
                elif result.status in {PaymentStatus.FAILED, PaymentStatus.TIMEOUT} and conv.order:
                    rt.platform.release_order_inventory(conv.business_id, conv.order.id)
                rt.store.save(conv)
        else:
            billing = rt.platform.billing_payment(result.checkout_request_id)
            if billing and billing["status"] == "pending":
                provider_status = rt.billing_mpesa()
                verified_status = provider_status.query_status(result.checkout_request_id) if provider_status else None
                if result.status == PaymentStatus.SUCCESS:
                    try:
                        payer_matches = result.phone is not None and normalize_phone(result.phone) == billing["phone"]
                    except ValueError:
                        payer_matches = False
                    verified = (
                        verified_status == PaymentStatus.SUCCESS
                        and result.amount == billing["amount_kes"]
                        and bool(result.receipt)
                        and payer_matches
                    )
                    if verified:
                        rt.platform.complete_billing_payment(
                            result.checkout_request_id,
                            amount_kes=billing["amount_kes"],
                            receipt=result.receipt,
                        )
                elif result.status in {PaymentStatus.FAILED, PaymentStatus.TIMEOUT} and verified_status == result.status:
                    rt.platform.fail_billing_payment(result.checkout_request_id)
        # Daraja expects this acknowledgement shape.
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    return app


# Module-level app for `uvicorn hermes.app:app`.
app = create_app()
