"""Select the real Daraja client when fully configured, else the simulator.

Environment variables (all required for the real client):
  MPESA_CONSUMER_KEY
  MPESA_CONSUMER_SECRET
  MPESA_SHORTCODE
  MPESA_PASSKEY
  MPESA_CALLBACK_URL          public https URL of the /mpesa/callback endpoint
  MPESA_ENVIRONMENT           sandbox | production   (default: sandbox)
  MPESA_TRANSACTION_TYPE      CustomerPayBillOnline | CustomerBuyGoodsOnline

Partial credentials and production mode without complete credentials fail
explicitly instead of silently selecting the simulator.
"""

from __future__ import annotations

import os

from hermes.mpesa.base import MpesaClient
from hermes.mpesa.simulator import MpesaSimulator

_REQUIRED = ["MPESA_CONSUMER_KEY", "MPESA_CONSUMER_SECRET", "MPESA_SHORTCODE", "MPESA_PASSKEY", "MPESA_CALLBACK_URL"]


def get_mpesa_client(credentials: dict[str, str] | None = None) -> MpesaClient:
    values = (
        credentials
        if credentials is not None
        else {key: os.getenv(key, "") for key in _REQUIRED}
    )
    configured = [key for key in _REQUIRED if values.get(key)]
    if len(configured) == len(_REQUIRED):
        from hermes.mpesa.daraja import DarajaClient

        return DarajaClient(
            consumer_key=values["MPESA_CONSUMER_KEY"],
            consumer_secret=values["MPESA_CONSUMER_SECRET"],
            shortcode=values["MPESA_SHORTCODE"],
            passkey=values["MPESA_PASSKEY"],
            callback_url=values["MPESA_CALLBACK_URL"],
            environment=values.get("MPESA_ENVIRONMENT", os.getenv("MPESA_ENVIRONMENT", "sandbox")),
            transaction_type=values.get(
                "MPESA_TRANSACTION_TYPE",
                os.getenv("MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline"),
            ),
        )
    if configured or values.get("MPESA_ENVIRONMENT", os.getenv("MPESA_ENVIRONMENT", "")).lower() == "production":
        missing = [key for key in _REQUIRED if not values.get(key)]
        raise ValueError(
            "Incomplete M-Pesa configuration; missing " + ", ".join(missing)
        )
    return MpesaSimulator()
