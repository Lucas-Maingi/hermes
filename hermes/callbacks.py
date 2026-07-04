"""Parse a Daraja STK callback body into a normalized result.

The same shape is produced by the real M-Pesa and by MpesaSimulator.build_callback,
so this one parser serves both.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.models import PaymentStatus


@dataclass
class CallbackResult:
    checkout_request_id: str
    status: PaymentStatus
    receipt: str | None = None
    amount: float | None = None
    phone: str | None = None


def parse_stk_callback(payload: dict) -> CallbackResult | None:
    """Parse ``Body.stkCallback``. Returns None if the body isn't a callback."""
    stk = payload.get("Body", {}).get("stkCallback")
    if not stk:
        return None

    checkout_id = stk.get("CheckoutRequestID", "")
    result_code = stk.get("ResultCode")

    if result_code == 0:
        meta = {
            item["Name"]: item.get("Value")
            for item in stk.get("CallbackMetadata", {}).get("Item", [])
        }
        return CallbackResult(
            checkout_request_id=checkout_id,
            status=PaymentStatus.SUCCESS,
            receipt=meta.get("MpesaReceiptNumber"),
            amount=meta.get("Amount"),
            phone=str(meta["PhoneNumber"]) if "PhoneNumber" in meta else None,
        )

    # 1037 = timeout; 1032 = cancelled; anything non-zero = not paid.
    status = PaymentStatus.TIMEOUT if result_code == 1037 else PaymentStatus.FAILED
    return CallbackResult(checkout_request_id=checkout_id, status=status)
