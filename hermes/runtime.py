"""Shared application state: the store, the tenant registry, and the LLM /
M-Pesa clients (constructed once so, e.g., the M-Pesa simulator keeps its
in-memory payment state across requests).
"""

from __future__ import annotations

import os

from hermes.agent import Agent
from hermes.business import Business
from hermes.llm.factory import get_llm_client
from hermes.models import Conversation
from hermes.mpesa.factory import get_mpesa_client
from hermes.sample_business import sample_business
from hermes.store import HermesStore


class Runtime:
    def __init__(self, *, db_path: str | None = None, business: Business | None = None, mpesa=None, llm=None):
        self.store = HermesStore(db_path or os.getenv("HERMES_DB", "hermes.db"))
        default = business or sample_business()
        self.businesses: dict[str, Business] = {default.id: default}
        self.default_business = default
        self.llm = llm or get_llm_client()
        self.mpesa = mpesa if mpesa is not None else get_mpesa_client()

    def business_for_phone_number_id(self, phone_number_id: str) -> Business:
        """Route an inbound WhatsApp message to its tenant by phone-number id,
        falling back to the default business (single-tenant demo)."""
        for biz in self.businesses.values():
            if biz.whatsapp_phone_number_id and biz.whatsapp_phone_number_id == phone_number_id:
                return biz
        return self.default_business

    def agent_for(self, business: Business) -> Agent:
        return Agent(business=business, llm=self.llm, mpesa=self.mpesa)

    def continue_or_start(self, phone: str, channel: str, business: Business) -> Conversation:
        conv = self.store.get_latest_by_phone(phone)
        if conv is None:
            conv = Conversation(customer_phone=phone, channel=channel)
        return conv
