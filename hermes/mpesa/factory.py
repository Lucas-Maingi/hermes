"""Select the real Daraja client when configured, else the simulator.

Environment variables (all required for the real client):
  MPESA_CONSUMER_KEY
  MPESA_CONSUMER_SECRET
  MPESA_SHORTCODE
  MPESA_PASSKEY
  MPESA_CALLBACK_URL          public https URL of the /mpesa/callback endpoint
  MPESA_ENVIRONMENT           sandbox | production   (default: sandbox)
  MPESA_TRANSACTION_TYPE      CustomerPayBillOnline | CustomerBuyGoodsOnline
"""

from __future__ import annotations

import os

from hermes.mpesa.base import MpesaClient
from hermes.mpesa.simulator import MpesaSimulator

_REQUIRED = ["MPESA_CONSUMER_KEY", "MPESA_CONSUMER_SECRET", "MPESA_SHORTCODE", "MPESA_PASSKEY", "MPESA_CALLBACK_URL"]


def get_mpesa_client() -> MpesaClient:
    if all(os.getenv(k) for k in _REQUIRED):
        from hermes.mpesa.daraja import DarajaClient

        return DarajaClient(
            consumer_key=os.environ["MPESA_CONSUMER_KEY"],
            consumer_secret=os.environ["MPESA_CONSUMER_SECRET"],
            shortcode=os.environ["MPESA_SHORTCODE"],
            passkey=os.environ["MPESA_PASSKEY"],
            callback_url=os.environ["MPESA_CALLBACK_URL"],
            environment=os.getenv("MPESA_ENVIRONMENT", "sandbox"),
            transaction_type=os.getenv("MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline"),
        )
    return MpesaSimulator()
