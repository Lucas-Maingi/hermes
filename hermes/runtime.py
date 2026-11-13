"""Shared application state: the store, the tenant registry, and the LLM /
M-Pesa clients (constructed once so, e.g., the M-Pesa simulator keeps its
in-memory payment state across requests).
"""

from __future__ import annotations

import os

from hermes.agent import Agent
from hermes.business import Business
from hermes.llm.factory import get_llm_client
from hermes.llm.mock import MockLLM
from hermes.models import Conversation
from hermes.mpesa.factory import get_mpesa_client
from hermes.platform import PlatformStore
from hermes.rag import build_retriever
from hermes.store import HermesStore


class Runtime:
    def __init__(
        self,
        *,
        db_path: str | None = None,
        business: Business | None = None,
        businesses: list[Business] | None = None,
        mpesa=None,
        llm=None,
    ):
        if business is not None and businesses is not None:
            raise ValueError("Pass either business or businesses, not both")
        if businesses is not None and not businesses:
            raise ValueError("At least one business is required")
        default = business or (
            businesses[0] if businesses else Business(id="hermes_workspace", name="Hermes workspace")
        )
        configured = businesses or [default]
        if default not in configured:
            configured = [default, *configured]
        configured_by_id = {item.id: item for item in configured}
        if len(configured_by_id) != len(configured):
            raise ValueError("Business IDs must be unique")
        phone_number_ids = [
            item.whatsapp_phone_number_id
            for item in configured
            if item.whatsapp_phone_number_id
        ]
        if len(phone_number_ids) != len(set(phone_number_ids)):
            raise ValueError("WhatsApp phone-number IDs must be unique")
        database_path = db_path or os.getenv("HERMES_DB", "hermes.db")
        self.platform = PlatformStore(database_path)
        self.store = HermesStore(database_path, legacy_business_id=default.id)
        for item in configured:
            self.platform.ensure_business(item)
        self.businesses = {item.id: item for item in self.platform.businesses()}
        self.default_business = self.businesses[default.id]
        self.llm = llm or get_llm_client()
        self.mpesa = mpesa if mpesa is not None else get_mpesa_client()
        self._mpesa_clients = {default.id: self.mpesa}
        self._billing_mpesa_client = None

    def business_for_phone_number_id(self, phone_number_id: str) -> Business | None:
        """Resolve a WhatsApp tenant from its registered Cloud API phone-number ID."""
        registered = self.platform.business_for_phone_number_id(phone_number_id)
        if registered is not None:
            return self.businesses.get(registered.id, registered)
        if len(self.businesses) == 1 and not self.default_business.whatsapp_phone_number_id:
            return self.default_business
        return None

    def agent_for(self, business: Business) -> Agent:
        integrations = self.platform.integrations(business.id)
        settings = integrations["llm"]
        if not integrations["llm_data_consent"]:
            llm = self.llm if isinstance(self.llm, MockLLM) else MockLLM()
        elif settings.get("chat_api_key"):
            from hermes.llm.openai_compatible import OpenAICompatibleLLM

            llm = OpenAICompatibleLLM(
                api_key=settings["chat_api_key"],
                model=settings.get("chat_model") or "gpt-4o-mini",
                base_url=settings.get("chat_base_url") or "https://api.openai.com/v1",
            )
        else:
            llm = self.llm
        return Agent(
            business=business,
            llm=llm,
            mpesa=self.mpesa_for(business),
            retriever=build_retriever(self.platform, business.id),
            platform=self.platform,
        )

    def mpesa_for(self, business: Business):
        if business.id not in self._mpesa_clients:
            integrations = self.platform.integrations(business.id)
            self._mpesa_clients[business.id] = get_mpesa_client(integrations["mpesa"])
        return self._mpesa_clients[business.id]

    def billing_mpesa(self):
        if self._billing_mpesa_client is not None:
            return self._billing_mpesa_client
        environment = os.getenv("HERMES_BILLING_MPESA_ENVIRONMENT", "sandbox")
        credential_names = {
            "MPESA_CONSUMER_KEY": "HERMES_BILLING_MPESA_CONSUMER_KEY",
            "MPESA_CONSUMER_SECRET": "HERMES_BILLING_MPESA_CONSUMER_SECRET",
            "MPESA_SHORTCODE": "HERMES_BILLING_MPESA_SHORTCODE",
            "MPESA_PASSKEY": "HERMES_BILLING_MPESA_PASSKEY",
            "MPESA_CALLBACK_URL": "HERMES_BILLING_MPESA_CALLBACK_URL",
        }
        credentials = {key: os.getenv(env_name, "") for key, env_name in credential_names.items()}
        credentials["MPESA_ENVIRONMENT"] = environment
        configured = any(credentials[key] for key in credential_names)
        if not configured:
            return None
        self._billing_mpesa_client = get_mpesa_client(credentials)
        return self._billing_mpesa_client

    def whatsapp_token_for(self, business: Business) -> str:
        return self.platform.integrations(business.id)["whatsapp_token"]

    def continue_or_start(self, phone: str, channel: str, business: Business) -> Conversation:
        conv = self.store.get_latest_by_phone(phone, business_id=business.id)
        if conv is None:
            conv = Conversation(business_id=business.id, customer_phone=phone, channel=channel)
        return conv
