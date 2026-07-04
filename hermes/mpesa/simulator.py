"""A Daraja-faithful M-Pesa simulator.

Reproduces the STK-push request/response and the asynchronous callback so the
entire payment flow -- push, customer PIN entry, receipt, confirmation -- can
run and be tested without a Safaricom account. The response and callback
payloads match the real Daraja shapes, so the same callback-handling code path
works against real M-Pesa unchanged.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from hermes.models import PaymentStatus
from hermes.mpesa.base import STKPushResult, normalize_phone


@dataclass
class _PendingPush:
    checkout_request_id: str
    merchant_request_id: str
    phone: str
    amount: float
    status: PaymentStatus = PaymentStatus.PENDING
    receipt: str | None = None


@dataclass
class MpesaSimulator:
    """In-memory STK-push simulator.

    By default a push stays PENDING until ``complete_payment`` is called
    (mirroring the customer entering their PIN) -- so a UI or test drives it
    explicitly. Set ``auto_complete=True`` to have pushes succeed immediately,
    which is convenient for smoke tests.
    """

    auto_complete: bool = False
    _pushes: dict[str, _PendingPush] = field(default_factory=dict)
    _seq: int = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_sim_{int(time.time())}_{self._seq}"

    def stk_push(self, *, phone, amount, account_reference, description) -> STKPushResult:
        phone = normalize_phone(phone)
        if amount <= 0:
            return STKPushResult(success=False, error="Invalid amount")

        checkout_id = self._next_id("ws_CO")
        merchant_id = self._next_id("ws_MO")
        push = _PendingPush(
            checkout_request_id=checkout_id,
            merchant_request_id=merchant_id,
            phone=phone,
            amount=float(amount),
        )
        if self.auto_complete:
            push.status = PaymentStatus.SUCCESS
            push.receipt = self._fake_receipt()
        self._pushes[checkout_id] = push

        return STKPushResult(
            success=True,
            checkout_request_id=checkout_id,
            merchant_request_id=merchant_id,
            customer_message="Success. Request accepted for processing",
        )

    def query_status(self, checkout_request_id: str) -> PaymentStatus:
        push = self._pushes.get(checkout_request_id)
        return push.status if push else PaymentStatus.FAILED

    # --- Simulation controls (stand in for the customer's phone) -------------

    def complete_payment(self, checkout_request_id: str, *, success: bool = True) -> dict:
        """Mark a pending push as paid/failed and return the Daraja-shaped
        callback payload that Safaricom would POST to the CallBackURL."""
        push = self._pushes.get(checkout_request_id)
        if push is None:
            raise KeyError(checkout_request_id)
        if success:
            push.status = PaymentStatus.SUCCESS
            push.receipt = self._fake_receipt()
        else:
            push.status = PaymentStatus.FAILED
        return self.build_callback(checkout_request_id)

    def build_callback(self, checkout_request_id: str) -> dict:
        """Construct the exact JSON body Daraja POSTs to the callback URL."""
        push = self._pushes[checkout_request_id]
        if push.status == PaymentStatus.SUCCESS:
            return {
                "Body": {
                    "stkCallback": {
                        "MerchantRequestID": push.merchant_request_id,
                        "CheckoutRequestID": push.checkout_request_id,
                        "ResultCode": 0,
                        "ResultDesc": "The service request is processed successfully.",
                        "CallbackMetadata": {
                            "Item": [
                                {"Name": "Amount", "Value": push.amount},
                                {"Name": "MpesaReceiptNumber", "Value": push.receipt},
                                {"Name": "PhoneNumber", "Value": int(push.phone)},
                                {"Name": "TransactionDate", "Value": int(time.strftime("%Y%m%d%H%M%S"))},
                            ]
                        },
                    }
                }
            }
        return {
            "Body": {
                "stkCallback": {
                    "MerchantRequestID": push.merchant_request_id,
                    "CheckoutRequestID": push.checkout_request_id,
                    "ResultCode": 1032,
                    "ResultDesc": "Request cancelled by user",
                }
            }
        }

    @staticmethod
    def _fake_receipt() -> str:
        letters = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(3))
        digits = "".join(random.choice("0123456789") for _ in range(7))
        return f"{letters}{digits}"
