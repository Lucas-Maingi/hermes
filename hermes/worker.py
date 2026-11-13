"""Background processing for durable outbound messaging and retention."""

from __future__ import annotations

import logging

import httpx

from hermes import whatsapp
from hermes.models import PaymentStatus
from hermes.runtime import Runtime

logger = logging.getLogger(__name__)


def process_outbound_queue(rt: Runtime, *, limit: int = 20) -> int:
    processed = 0
    for message in rt.platform.ready_outbound(limit):
        claim_token = message["claim_token"]
        try:
            business = rt.businesses.get(message["business_id"]) or rt.platform.business(message["business_id"])
            if business is None:
                rt.platform.finish_outbound(
                    message["id"],
                    claim_token=claim_token,
                    success=False,
                    error="Business no longer exists",
                )
                continue
            token = rt.whatsapp_token_for(business)
            sent = whatsapp.send_text(
                message["to_phone"],
                message["text"],
                phone_number_id=message["phone_number_id"],
                access_token=token,
            )
            rt.platform.finish_outbound(
                message["id"],
                claim_token=claim_token,
                success=sent,
                error="WhatsApp API send failed" if not sent else "",
            )
            if not sent:
                logger.error("Queued WhatsApp send failed for business_id=%s", business.id)
            processed += 1
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            rt.platform.finish_outbound(
                message["id"],
                claim_token=claim_token,
                success=False,
                error=str(exc),
            )
            logger.exception("Could not send queued WhatsApp message")
            processed += 1
    return processed


def process_scheduled_followups(rt: Runtime, *, limit: int = 20) -> int:
    processed = 0
    for followup in rt.platform.ready_followups(limit):
        claim_token = followup["claim_token"]
        try:
            business = rt.businesses.get(followup["business_id"]) or rt.platform.business(followup["business_id"])
            if business is None or not followup["whatsapp_phone_number_id"]:
                sent = False
                error = "WhatsApp business connection is unavailable"
            else:
                sent = whatsapp.send_template(
                    followup["phone"],
                    followup["template_name"],
                    followup["language"],
                    followup["parameters"],
                    phone_number_id=followup["whatsapp_phone_number_id"],
                    access_token=rt.whatsapp_token_for(business),
                )
                error = "" if sent else "WhatsApp template send failed"
            rt.platform.finish_followup(
                followup["id"],
                claim_token=claim_token,
                success=sent,
                error=error,
            )
            if not sent:
                logger.error("Scheduled WhatsApp follow-up failed for business_id=%s", followup["business_id"])
            processed += 1
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            rt.platform.finish_followup(
                followup["id"],
                claim_token=claim_token,
                success=False,
                error=str(exc),
            )
            logger.exception("Could not send scheduled WhatsApp follow-up")
            processed += 1
    return processed


def purge_expired_data(rt: Runtime, retention_days: int) -> int:
    if retention_days < 1:
        raise ValueError("Retention days must be at least one")
    return rt.platform.purge_expired_conversations(retention_days)


def process_billing_reconciliation(rt: Runtime, *, limit: int = 20) -> int:
    try:
        provider = rt.billing_mpesa()
    except ValueError:
        logger.exception("Hermes subscription M-Pesa reconciliation is misconfigured")
        return 0
    if provider is None:
        return 0
    processed = 0
    for payment in rt.platform.billing_reconciliation_candidates(limit=limit):
        checkout_id = payment["checkout_request_id"]
        try:
            status = provider.query_status(checkout_id)
        except (httpx.HTTPError, RuntimeError, ValueError):
            logger.exception("Could not reconcile Hermes billing checkout %s", checkout_id)
            continue
        rt.platform.record_billing_reconciliation(checkout_id, status.value)
        if status == PaymentStatus.SUCCESS:
            logger.warning(
                "Billing checkout %s is provider-successful but has no verified callback; manual reconciliation required",
                checkout_id,
            )
        processed += 1
    return processed


def expire_inventory_reservations(rt: Runtime) -> int:
    return rt.platform.expire_inventory_reservations()
