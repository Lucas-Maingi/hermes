"""M-Pesa client protocol + shared helpers matching Daraja conventions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from hermes.models import PaymentStatus


@dataclass
class STKPushResult:
    """Mirrors the meaningful fields of a Daraja STK-push response."""

    success: bool                       # ResponseCode == "0"
    checkout_request_id: str = ""       # correlates the async callback
    merchant_request_id: str = ""
    customer_message: str = ""
    error: str = ""


class MpesaClient(Protocol):
    def stk_push(
        self,
        *,
        phone: str,
        amount: float,
        account_reference: str,
        description: str,
    ) -> STKPushResult:
        """Trigger an STK-push (Lipa na M-Pesa Online) to the payer's phone."""
        ...

    def query_status(self, checkout_request_id: str) -> PaymentStatus:
        """Return the current status of a previously initiated STK push."""
        ...


def normalize_phone(phone: str) -> str:
    """Normalize a Kenyan number to Daraja MSISDN form (2547XXXXXXXX / 2541XXXXXXXX).

    Accepts 07..., 01..., +2547..., 2547..., and 7... forms.
    """
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0") and len(digits) == 10:
        return "254" + digits[1:]
    if digits.startswith("254") and len(digits) == 12:
        return digits
    if len(digits) == 9 and digits[0] in "71":
        return "254" + digits
    return digits  # best effort; the real API will reject a bad number
