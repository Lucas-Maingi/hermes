"""The real Safaricom Daraja (M-Pesa Lipa na M-Pesa Online) integration.

Implements the actual OAuth token fetch, STK-push request, and status query
against Daraja's sandbox or production hosts. Behind the same ``MpesaClient``
protocol as the simulator, so nothing upstream changes when real credentials
are supplied. This is what makes payment collection a real capability, not a
mock -- a business plugs in their Daraja app credentials and shortcode and it
collects real money.

Callbacks are received by the FastAPI ``/mpesa/callback`` endpoint (M6), which
parses the same payload shape the simulator produces.
"""

from __future__ import annotations

import base64
import time

import httpx

from hermes.models import PaymentStatus
from hermes.mpesa.base import STKPushResult, normalize_phone

SANDBOX_BASE = "https://sandbox.safaricom.co.ke"
PRODUCTION_BASE = "https://api.safaricom.co.ke"


class DarajaClient:
    def __init__(
        self,
        *,
        consumer_key: str,
        consumer_secret: str,
        shortcode: str,
        passkey: str,
        callback_url: str,
        environment: str = "sandbox",
        transaction_type: str = "CustomerPayBillOnline",
        timeout: float = 30.0,
    ):
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.shortcode = shortcode
        self.passkey = passkey
        self.callback_url = callback_url
        self.base_url = PRODUCTION_BASE if environment == "production" else SANDBOX_BASE
        self.transaction_type = transaction_type
        self.timeout = timeout
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # -- Auth -----------------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 30:
            return self._token
        resp = httpx.get(
            f"{self.base_url}/oauth/v1/generate",
            params={"grant_type": "client_credentials"},
            auth=(self.consumer_key, self.consumer_secret),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3599))
        return self._token

    def _password(self, timestamp: str) -> str:
        raw = f"{self.shortcode}{self.passkey}{timestamp}".encode()
        return base64.b64encode(raw).decode()

    # -- Client protocol ------------------------------------------------------

    def stk_push(self, *, phone, amount, account_reference, description) -> STKPushResult:
        phone = normalize_phone(phone)
        timestamp = time.strftime("%Y%m%d%H%M%S")
        payload = {
            "BusinessShortCode": self.shortcode,
            "Password": self._password(timestamp),
            "Timestamp": timestamp,
            "TransactionType": self.transaction_type,
            "Amount": int(round(amount)),  # Daraja requires a whole number
            "PartyA": phone,
            "PartyB": self.shortcode,
            "PhoneNumber": phone,
            "CallBackURL": self.callback_url,
            "AccountReference": account_reference[:12],
            "TransactionDesc": description[:13] or "Payment",
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/mpesa/stkpush/v1/processrequest",
                headers={"Authorization": f"Bearer {self._access_token()}"},
                json=payload,
                timeout=self.timeout,
            )
            data = resp.json()
        except httpx.HTTPError as exc:
            return STKPushResult(success=False, error=str(exc))

        if str(data.get("ResponseCode")) == "0":
            return STKPushResult(
                success=True,
                checkout_request_id=data.get("CheckoutRequestID", ""),
                merchant_request_id=data.get("MerchantRequestID", ""),
                customer_message=data.get("CustomerMessage", ""),
            )
        return STKPushResult(
            success=False,
            error=data.get("errorMessage") or data.get("ResponseDescription", "STK push failed"),
        )

    def query_status(self, checkout_request_id: str) -> PaymentStatus:
        timestamp = time.strftime("%Y%m%d%H%M%S")
        payload = {
            "BusinessShortCode": self.shortcode,
            "Password": self._password(timestamp),
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/mpesa/stkpushquery/v1/query",
                headers={"Authorization": f"Bearer {self._access_token()}"},
                json=payload,
                timeout=self.timeout,
            )
            data = resp.json()
        except httpx.HTTPError:
            return PaymentStatus.PENDING  # transient; caller can retry

        result_code = str(data.get("ResultCode", ""))
        if result_code == "0":
            return PaymentStatus.SUCCESS
        # 1032 = cancelled by user; 1037 = timeout; others = failed
        if result_code == "1037":
            return PaymentStatus.TIMEOUT
        if result_code in ("", "1"):  # still processing / no final result yet
            return PaymentStatus.PENDING
        return PaymentStatus.FAILED
