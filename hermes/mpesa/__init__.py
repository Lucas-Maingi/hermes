"""M-Pesa payment collection.

The agent depends only on the ``MpesaClient`` protocol. Two implementations
sit behind it:

* ``DarajaClient`` -- the real Safaricom Daraja integration (OAuth + STK push +
  status query), works against sandbox or production by config.
* ``MpesaSimulator`` -- mirrors Daraja's request/response/callback shapes so the
  whole payment flow runs and is tested with no account.

``get_mpesa_client()`` returns the real client when Daraja credentials are
configured, else the simulator.
"""

from hermes.mpesa.base import MpesaClient, STKPushResult, normalize_phone
from hermes.mpesa.factory import get_mpesa_client
from hermes.mpesa.simulator import MpesaSimulator

__all__ = [
    "MpesaClient",
    "STKPushResult",
    "normalize_phone",
    "MpesaSimulator",
    "get_mpesa_client",
]
