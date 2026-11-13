"""Tenant accounts, encrypted provider credentials, and operator workflows."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet

from hermes.business import Business, Product

_PLATFORM_SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    currency TEXT NOT NULL,
    config_json TEXT NOT NULL,
    whatsapp_phone_number_id TEXT UNIQUE,
    whatsapp_token_enc TEXT,
    whatsapp_app_secret_enc TEXT,
    whatsapp_verify_token_enc TEXT,
    mpesa_credentials_enc TEXT,
    llm_credentials_enc TEXT,
    embedding_consent INTEGER NOT NULL DEFAULT 0,
    llm_data_consent INTEGER NOT NULL DEFAULT 0,
    trial_started_at REAL,
    trial_expires_at REAL,
    subscription_status TEXT NOT NULL DEFAULT 'legacy',
    premium_monthly_price_kes INTEGER NOT NULL DEFAULT 2500,
    subscription_expires_at REAL,
    booking_timezone TEXT NOT NULL DEFAULT 'Africa/Nairobi',
    booking_hours_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner', 'staff')),
    email_verified_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS email_verification_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_rate_limits (
    bucket_hash TEXT PRIMARY KEY,
    window_start REAL NOT NULL,
    attempts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_events (
    business_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    status TEXT NOT NULL,
    received_at REAL NOT NULL,
    PRIMARY KEY (business_id, message_id)
);
CREATE TABLE IF NOT EXISTS handoffs (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    assigned_to TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbound_messages (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL,
    to_phone TEXT NOT NULL,
    text TEXT NOT NULL,
    phone_number_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    locked_until REAL,
    claim_token TEXT,
    last_error TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_documents (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    business_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding_json TEXT
);
CREATE TABLE IF NOT EXISTS inventory (
    business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    updated_at REAL NOT NULL,
    PRIMARY KEY (business_id, product_name)
);
CREATE TABLE IF NOT EXISTS inventory_events (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    product_name TEXT NOT NULL,
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_reservations (
    business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    order_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    status TEXT NOT NULL CHECK(status IN ('active', 'committed', 'released')),
    expires_at REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (business_id, order_id, product_name)
);
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    service TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    starts_at REAL NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK(duration_minutes BETWEEN 5 AND 1440),
    status TEXT NOT NULL CHECK(status IN ('confirmed', 'cancelled', 'completed')),
    notes TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS whatsapp_templates (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    language TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('utility', 'marketing', 'authentication')),
    body_parameter_count INTEGER NOT NULL CHECK(body_parameter_count BETWEEN 0 AND 10),
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    UNIQUE (business_id, name, language)
);
CREATE TABLE IF NOT EXISTS whatsapp_consents (
    business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    phone_hash TEXT NOT NULL,
    phone_enc TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('opted_in', 'opted_out')),
    source_enc TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (business_id, phone_hash)
);
CREATE TABLE IF NOT EXISTS scheduled_followups (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    template_id TEXT NOT NULL REFERENCES whatsapp_templates(id) ON DELETE CASCADE,
    phone_hash TEXT NOT NULL,
    phone_enc TEXT NOT NULL,
    parameters_enc TEXT NOT NULL,
    send_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'processing', 'sent', 'failed', 'cancelled', 'suppressed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    locked_until REAL,
    claim_token TEXT,
    last_error TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS billing_payments (
    id TEXT PRIMARY KEY,
    checkout_request_id TEXT UNIQUE,
    business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    amount_kes INTEGER NOT NULL,
    phone_enc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'starting' CHECK(status IN ('starting', 'pending', 'paid', 'failed')),
    receipt TEXT,
    created_at REAL NOT NULL,
    completed_at REAL,
    last_checked_at REAL,
    reconcile_attempts INTEGER NOT NULL DEFAULT 0,
    provider_status TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    channel TEXT NOT NULL,
    external_source_id TEXT,
    created_at REAL NOT NULL,
    UNIQUE (business_id, external_source_id)
);
CREATE TABLE IF NOT EXISTS campaign_attributions (
    business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    provider_source_id TEXT,
    attributed_at REAL NOT NULL,
    converted_at REAL,
    revenue REAL NOT NULL DEFAULT 0,
    order_id TEXT,
    PRIMARY KEY (business_id, conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_handoffs_business_status ON handoffs(business_id, status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_handoffs_one_active
    ON handoffs(business_id, conversation_id) WHERE status != 'resolved';
CREATE INDEX IF NOT EXISTS idx_outbound_ready ON outbound_messages(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_chunks_business ON knowledge_chunks(business_id);
CREATE INDEX IF NOT EXISTS idx_bookings_tenant_time ON bookings(business_id, starts_at, status);
CREATE INDEX IF NOT EXISTS idx_inventory_events_tenant_time ON inventory_events(business_id, created_at);
CREATE INDEX IF NOT EXISTS idx_followups_due ON scheduled_followups(status, send_at, locked_until);
CREATE INDEX IF NOT EXISTS idx_email_verification_expiry ON email_verification_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_inventory_reservations_active
    ON inventory_reservations(business_id, product_name, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_campaigns_tenant ON campaigns(business_id, created_at);
"""

_PASSWORD_ITERATIONS = 310_000
_SESSION_SECONDS = 60 * 60 * 24 * 7
_EMAIL_VERIFICATION_SECONDS = 60 * 60 * 24
_SUBSCRIPTION_GRACE_SECONDS = 7 * 24 * 60 * 60


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${salt.hex()}${derived.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return secrets.compare_digest(derived.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


class PlatformStore:
    """Persistence boundary for tenant accounts and the operational queues."""

    def __init__(self, db_path: str | Path, encryption_key: str | None = None):
        self.db_path = str(db_path)
        key = encryption_key or os.getenv("HERMES_ENCRYPTION_KEY", "")
        self._fernet = Fernet(key.encode()) if key else None
        with self._connect() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(_PLATFORM_SCHEMA)
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(businesses)").fetchall()
            }
            for name, declaration in (
                ("whatsapp_app_secret_enc", "TEXT"),
                ("whatsapp_verify_token_enc", "TEXT"),
                ("embedding_consent", "INTEGER NOT NULL DEFAULT 0"),
                ("llm_data_consent", "INTEGER NOT NULL DEFAULT 0"),
                ("trial_started_at", "REAL"),
                ("trial_expires_at", "REAL"),
                ("subscription_status", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("premium_monthly_price_kes", "INTEGER NOT NULL DEFAULT 2500"),
                ("subscription_expires_at", "REAL"),
                ("booking_timezone", "TEXT NOT NULL DEFAULT 'Africa/Nairobi'"),
                ("booking_hours_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE businesses ADD COLUMN {name} {declaration}")
            user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "email_verified_at" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN email_verified_at REAL")
                conn.execute("UPDATE users SET email_verified_at = created_at")
            billing_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(billing_payments)").fetchall()
            }
            for name, declaration in (
                ("last_checked_at", "REAL"),
                ("reconcile_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("provider_status", "TEXT"),
                ("needs_review", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in billing_columns:
                    conn.execute(f"ALTER TABLE billing_payments ADD COLUMN {name} {declaration}")
            outbound_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(outbound_messages)").fetchall()
            }
            for name, declaration in (("locked_until", "REAL"), ("claim_token", "TEXT")):
                if name not in outbound_columns:
                    conn.execute(f"ALTER TABLE outbound_messages ADD COLUMN {name} {declaration}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _encrypt(self, value: str) -> str | None:
        if not value:
            return None
        if self._fernet is None:
            raise RuntimeError("HERMES_ENCRYPTION_KEY must be configured before saving provider credentials")
        return self._fernet.encrypt(value.encode()).decode()

    def _require_encryption_key(self) -> None:
        if self._fernet is None:
            raise RuntimeError("HERMES_ENCRYPTION_KEY is required to store customer contact data")

    def _decrypt(self, value: str | None) -> str:
        if not value:
            return ""
        if self._fernet is None:
            raise RuntimeError("HERMES_ENCRYPTION_KEY is required to read provider credentials")
        return self._fernet.decrypt(value.encode()).decode()

    def _protect_data(self, value: str) -> str:
        if self._fernet is None:
            return value
        return "enc:" + self._fernet.encrypt(value.encode()).decode()

    def _reveal_data(self, value: str) -> str:
        if not value.startswith("enc:"):
            return value
        if self._fernet is None:
            raise RuntimeError("HERMES_ENCRYPTION_KEY is required to read encrypted merchant data")
        return self._fernet.decrypt(value[4:].encode()).decode()

    def create_business(
        self,
        *,
        name: str,
        email: str,
        password: str,
        currency: str = "KES",
        products: list[dict[str, Any]] | None = None,
        info: dict[str, str] | None = None,
        whatsapp_phone_number_id: str = "",
        whatsapp_token: str = "",
        whatsapp_app_secret: str = "",
        whatsapp_verify_token: str = "",
        mpesa_credentials: dict[str, str] | None = None,
        llm_credentials: dict[str, str] | None = None,
        premium_trial_days: int = 7,
        premium_monthly_price_kes: int = 2500,
        defer_trial_until_verified: bool = False,
    ) -> tuple[str, str]:
        name = name.strip()
        email = email.strip().lower()
        if not name or not email or "@" not in email:
            raise ValueError("A business name and valid email are required")
        if not 1 <= premium_trial_days <= 30:
            raise ValueError("Premium trial must be between 1 and 30 days")
        if premium_monthly_price_kes < 1:
            raise ValueError("Premium monthly price must be positive")
        user_id = uuid.uuid4().hex
        business_id = uuid.uuid4().hex
        now = time.time()
        trial_started_at = None if defer_trial_until_verified else now
        trial_expires_at = None if defer_trial_until_verified else now + premium_trial_days * 24 * 60 * 60
        subscription_status = "pending_verification" if defer_trial_until_verified else "trialing"
        catalog = products or []
        config = {"products": catalog, "info": info or {}, "staff_handoff_number": ""}
        whatsapp_enc = self._encrypt(whatsapp_token)
        whatsapp_app_secret_enc = self._encrypt(whatsapp_app_secret)
        whatsapp_verify_token_enc = self._encrypt(whatsapp_verify_token)
        mpesa_enc = self._encrypt(json.dumps(mpesa_credentials or {})) if mpesa_credentials else None
        llm_enc = self._encrypt(json.dumps(llm_credentials or {})) if llm_credentials else None
        password_hash = _hash_password(password)
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO businesses
                        (id, name, currency, config_json, whatsapp_phone_number_id,
                         whatsapp_token_enc, whatsapp_app_secret_enc, whatsapp_verify_token_enc,
                         mpesa_credentials_enc, llm_credentials_enc, trial_started_at,
                         trial_expires_at, subscription_status, premium_monthly_price_kes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        business_id,
                        name,
                        currency,
                        self._protect_data(json.dumps(config)),
                        whatsapp_phone_number_id or None,
                        whatsapp_enc,
                        whatsapp_app_secret_enc,
                        whatsapp_verify_token_enc,
                        mpesa_enc,
                        llm_enc,
                        trial_started_at,
                        trial_expires_at,
                        subscription_status,
                        premium_monthly_price_kes,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO users (id, business_id, email, password_hash, role, created_at) "
                    "VALUES (?, ?, ?, ?, 'owner', ?)",
                    (user_id, business_id, email, password_hash, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Email or WhatsApp phone-number ID is already registered") from exc
        return user_id, business_id

    def create_email_verification_token(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
                raise KeyError(user_id)
            conn.execute("DELETE FROM email_verification_tokens WHERE user_id = ?", (user_id,))
            conn.execute(
                "INSERT INTO email_verification_tokens (token_hash, user_id, expires_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                (digest, user_id, now + _EMAIL_VERIFICATION_SECONDS, now),
            )
        return token

    def verify_email_token(self, token: str, *, now: float | None = None) -> tuple[str, str] | None:
        if not token or len(token) > 256:
            return None
        current_time = time.time() if now is None else now
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT users.id, users.business_id
                FROM email_verification_tokens
                JOIN users ON users.id = email_verification_tokens.user_id
                WHERE email_verification_tokens.token_hash = ?
                  AND email_verification_tokens.expires_at > ?
                  AND users.email_verified_at IS NULL
                """,
                (digest, current_time),
            ).fetchone()
            if row is None:
                conn.execute("DELETE FROM email_verification_tokens WHERE expires_at <= ?", (current_time,))
                return None
            conn.execute("UPDATE users SET email_verified_at = ? WHERE id = ?", (current_time, row["id"]))
            conn.execute(
                """
                UPDATE businesses
                SET subscription_status = 'trialing', trial_started_at = ?, trial_expires_at = ?
                WHERE id = ? AND subscription_status = 'pending_verification'
                """,
                (current_time, current_time + 7 * 24 * 60 * 60, row["business_id"]),
            )
            conn.execute("DELETE FROM email_verification_tokens WHERE user_id = ?", (row["id"],))
        return row["id"], row["business_id"]

    def create_email_verification_for_email(self, email: str) -> tuple[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email_verified_at FROM users WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        if row is None or row["email_verified_at"] is not None:
            return None
        return row["id"], self.create_email_verification_token(row["id"])

    def subscription(self, business_id: str, *, now: float | None = None) -> dict[str, Any]:
        current_time = time.time() if now is None else now
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT trial_started_at, trial_expires_at, subscription_status,
                       premium_monthly_price_kes, subscription_expires_at
                FROM businesses WHERE id = ?
                """,
                (business_id,),
            ).fetchone()
            if row is None:
                raise KeyError(business_id)
            status = row["subscription_status"]
            trial_expired = status == "trialing" and row["trial_expires_at"] <= current_time
            premium_expired = status == "premium" and (
                row["subscription_expires_at"] is None
                or row["subscription_expires_at"] + _SUBSCRIPTION_GRACE_SECONDS <= current_time
            )
            premium_in_grace = status == "premium" and (
                row["subscription_expires_at"] is not None
                and row["subscription_expires_at"] <= current_time
                and not premium_expired
            )
            if trial_expired or premium_expired:
                status = "free"
                conn.execute(
                    "UPDATE businesses SET subscription_status = 'free' WHERE id = ?",
                    (business_id,),
                )
            elif premium_in_grace:
                status = "grace_period"
        expires_at = row["trial_expires_at"]
        days_remaining = (
            max(0, int((expires_at - current_time + 86_399) // 86_400))
            if status == "trialing" and expires_at is not None
            else 0
        )
        return {
            "status": status,
            "premium_active": status in {"trialing", "premium", "grace_period", "legacy"},
            "grace_expires_at": (
                row["subscription_expires_at"] + _SUBSCRIPTION_GRACE_SECONDS
                if status == "grace_period" and row["subscription_expires_at"] is not None
                else None
            ),
            "trial_started_at": row["trial_started_at"],
            "trial_expires_at": expires_at,
            "trial_days_remaining": days_remaining,
            "premium_monthly_price_kes": row["premium_monthly_price_kes"],
            "subscription_expires_at": row["subscription_expires_at"],
        }

    def create_billing_intent(self, business_id: str, phone: str, amount_kes: int) -> str:
        self._require_encryption_key()
        normalized = self.normalize_whatsapp_phone(phone)
        now = time.time()
        intent_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM businesses WHERE id = ?", (business_id,)).fetchone():
                raise KeyError(business_id)
            pending = conn.execute(
                "SELECT 1 FROM billing_payments WHERE business_id = ? AND status IN ('starting', 'pending')",
                (business_id,),
            ).fetchone()
            if pending:
                raise ValueError("A subscription payment is already pending")
            conn.execute(
                """
                INSERT INTO billing_payments
                    (id, business_id, amount_kes, phone_enc, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (intent_id, business_id, amount_kes, self._protect_data(normalized), now),
            )
        return intent_id

    def attach_billing_checkout(self, intent_id: str, checkout_request_id: str) -> bool:
        if not checkout_request_id:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE billing_payments SET checkout_request_id = ?, status = 'pending' "
                "WHERE id = ? AND status = 'starting'",
                (checkout_request_id, intent_id),
            )
        return cursor.rowcount == 1

    def billing_payment(self, checkout_request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT checkout_request_id, business_id, amount_kes, phone_enc, status, "
                "provider_status, needs_review "
                "FROM billing_payments WHERE checkout_request_id = ?",
                (checkout_request_id,),
            ).fetchone()
        if row is None:
            return None
        payment = dict(row)
        payment["phone"] = self._reveal_data(payment.pop("phone_enc"))
        return payment

    def billing_status(self, business_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT checkout_request_id, amount_kes, status, receipt, created_at,
                       completed_at, provider_status, needs_review
                FROM billing_payments WHERE business_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (business_id,),
            ).fetchone()
        return dict(row) if row else None

    def billing_reconciliation_candidates(
        self,
        *,
        now: float | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        current_time = time.time() if now is None else now
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT checkout_request_id, created_at, reconcile_attempts
                FROM billing_payments
                WHERE status = 'pending' AND checkout_request_id IS NOT NULL AND needs_review = 0
                  AND created_at <= ?
                  AND (last_checked_at IS NULL OR last_checked_at <= ?)
                ORDER BY created_at LIMIT ?
                """,
                (current_time - 60, current_time - 300, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_billing_reconciliation(
        self,
        checkout_request_id: str,
        provider_status: str,
        *,
        now: float | None = None,
    ) -> None:
        current_time = time.time() if now is None else now
        if provider_status not in {"pending", "success", "failed", "timeout"}:
            raise ValueError("Invalid billing provider status")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT reconcile_attempts FROM billing_payments "
                "WHERE checkout_request_id = ? AND status = 'pending'",
                (checkout_request_id,),
            ).fetchone()
            if row is None:
                return
            attempts = row["reconcile_attempts"] + 1
            failed = provider_status in {"failed", "timeout"}
            conn.execute(
                """
                UPDATE billing_payments
                SET last_checked_at = ?, reconcile_attempts = ?, provider_status = ?,
                    needs_review = ?, status = CASE WHEN ? THEN 'failed' ELSE status END,
                    completed_at = CASE WHEN ? THEN ? ELSE completed_at END
                WHERE checkout_request_id = ? AND status = 'pending'
                """,
                (
                    current_time,
                    attempts,
                    provider_status,
                    int(attempts >= 5 and provider_status == "success"),
                    int(failed),
                    int(failed),
                    current_time,
                    checkout_request_id,
                ),
            )

    def complete_billing_payment(self, checkout_request_id: str, *, amount_kes: int, receipt: str | None) -> bool:
        if not receipt or len(receipt) > 40:
            return False
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            payment = conn.execute(
                "SELECT business_id, amount_kes FROM billing_payments "
                "WHERE checkout_request_id = ? AND status = 'pending'",
                (checkout_request_id,),
            ).fetchone()
            if payment is None or payment["amount_kes"] != amount_kes:
                return False
            business_id = payment["business_id"]
            conn.execute(
                """
                UPDATE billing_payments SET status = 'paid', receipt = ?, completed_at = ?
                WHERE checkout_request_id = ? AND status = 'pending'
                """,
                (receipt, now, checkout_request_id),
            )
            row = conn.execute(
                "SELECT subscription_expires_at FROM businesses WHERE id = ?",
                (business_id,),
            ).fetchone()
            current_expiry = row["subscription_expires_at"] if row else None
            expiry = max(now, current_expiry or now) + 30 * 24 * 60 * 60
            conn.execute(
                "UPDATE businesses SET subscription_status = 'premium', subscription_expires_at = ? "
                "WHERE id = ?",
                (expiry, business_id),
            )
        return True

    def fail_billing_payment(self, checkout_request_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE billing_payments SET status = 'failed', completed_at = ? "
                "WHERE (checkout_request_id = ? OR id = ?) AND status IN ('starting', 'pending')",
                (time.time(), checkout_request_id, checkout_request_id),
            )

    def create_campaign(
        self,
        business_id: str,
        *,
        name: str,
        channel: str,
        external_source_id: str,
    ) -> str:
        campaign_name = name.strip()
        source_id = external_source_id.strip()
        if not campaign_name or len(campaign_name) > 120:
            raise ValueError("Campaign name is required and must be at most 120 characters")
        if channel not in {"whatsapp", "facebook", "instagram", "other"}:
            raise ValueError("Invalid campaign channel")
        if not source_id or len(source_id) > 200:
            raise ValueError("A provider source/ad ID is required for conversion attribution")
        campaign_id = uuid.uuid4().hex
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO campaigns (id, business_id, name, channel, external_source_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (campaign_id, business_id, campaign_name, channel, source_id, time.time()),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Campaign source ID already exists or workspace was not found") from exc
        return campaign_id

    def attribute_campaign_conversation(
        self,
        business_id: str,
        conversation_id: str,
        provider_source_id: str,
    ) -> bool:
        source_id = provider_source_id.strip()
        if not source_id:
            return False
        with self._connect() as conn:
            campaign = conn.execute(
                "SELECT id FROM campaigns WHERE business_id = ? AND external_source_id = ?",
                (business_id, source_id),
            ).fetchone()
            if campaign is None:
                return False
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO campaign_attributions
                    (business_id, conversation_id, campaign_id, provider_source_id, attributed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (business_id, conversation_id, campaign["id"], source_id, time.time()),
            )
        return cursor.rowcount == 1

    def record_campaign_conversion(
        self,
        business_id: str,
        conversation_id: str,
        *,
        order_id: str,
        revenue: float,
    ) -> bool:
        if revenue < 0:
            raise ValueError("Campaign conversion revenue cannot be negative")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE campaign_attributions
                SET converted_at = ?, revenue = ?, order_id = ?
                WHERE business_id = ? AND conversation_id = ? AND converted_at IS NULL
                """,
                (time.time(), revenue, order_id, business_id, conversation_id),
            )
        return cursor.rowcount == 1

    def campaigns(self, business_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT campaigns.id, campaigns.name, campaigns.channel,
                       campaigns.external_source_id, campaigns.created_at,
                       COUNT(campaign_attributions.conversation_id) AS attributed_conversations,
                       SUM(CASE WHEN campaign_attributions.converted_at IS NOT NULL THEN 1 ELSE 0 END)
                           AS conversions,
                       COALESCE(SUM(campaign_attributions.revenue), 0) AS revenue
                FROM campaigns
                LEFT JOIN campaign_attributions
                  ON campaign_attributions.business_id = campaigns.business_id
                 AND campaign_attributions.campaign_id = campaigns.id
                WHERE campaigns.business_id = ?
                GROUP BY campaigns.id
                ORDER BY campaigns.created_at DESC
                """,
                (business_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def ensure_business(self, business: Business) -> None:
        config = {
            "products": [
                {"name": p.name, "price": p.price, "aliases": p.aliases, "in_stock": p.in_stock, "unit": p.unit}
                for p in business.products
            ],
            "info": business.info,
            "staff_handoff_number": business.staff_handoff_number,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO businesses
                    (id, name, currency, config_json, whatsapp_phone_number_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    business.id,
                    business.name,
                    business.currency,
                    self._protect_data(json.dumps(config)),
                    business.whatsapp_phone_number_id or None,
                    time.time(),
                ),
            )

    def business(self, business_id: str) -> Business | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
        return self._business_from_row(row) if row else None

    def businesses(self) -> list[Business]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM businesses ORDER BY created_at").fetchall()
        return [self._business_from_row(row) for row in rows]

    def _business_from_row(self, row: sqlite3.Row) -> Business:
        config = json.loads(self._reveal_data(row["config_json"]))
        return Business(
            id=row["id"],
            name=row["name"],
            currency=row["currency"],
            products=[Product(**product) for product in config.get("products", [])],
            info=config.get("info", {}),
            staff_handoff_number=config.get("staff_handoff_number", ""),
            whatsapp_phone_number_id=row["whatsapp_phone_number_id"] or "",
        )

    def business_for_phone_number_id(self, phone_number_id: str) -> Business | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM businesses WHERE whatsapp_phone_number_id = ?",
                (phone_number_id,),
            ).fetchone()
        return self._business_from_row(row) if row else None

    def save_integrations(
        self,
        business_id: str,
        *,
        whatsapp_phone_number_id: str,
        whatsapp_token: str,
        whatsapp_app_secret: str = "",
        whatsapp_verify_token: str = "",
        mpesa_credentials: dict[str, str],
        llm_credentials: dict[str, str],
        embedding_consent: bool = False,
        llm_data_consent: bool = False,
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE businesses
                SET whatsapp_phone_number_id = ?, whatsapp_token_enc = ?,
                    whatsapp_app_secret_enc = ?, whatsapp_verify_token_enc = ?,
                    mpesa_credentials_enc = ?, llm_credentials_enc = ?,
                    embedding_consent = ?, llm_data_consent = ?
                WHERE id = ?
                """,
                (
                    whatsapp_phone_number_id or None,
                    self._encrypt(whatsapp_token),
                    self._encrypt(whatsapp_app_secret),
                    self._encrypt(whatsapp_verify_token),
                    self._encrypt(json.dumps(mpesa_credentials)) if any(mpesa_credentials.values()) else None,
                    self._encrypt(json.dumps(llm_credentials)) if any(llm_credentials.values()) else None,
                    int(embedding_consent),
                    int(llm_data_consent),
                    business_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(business_id)
            if not embedding_consent:
                conn.execute(
                    """
                    UPDATE knowledge_chunks SET embedding_json = NULL
                    WHERE business_id = ? AND embedding_json IS NOT NULL
                    """,
                    (business_id,),
                )

    def save_catalog(
        self,
        business_id: str,
        *,
        products: list[dict[str, Any]],
        info: dict[str, str],
    ) -> None:
        validated = [Product(**product) for product in products]
        if len({product.name.casefold() for product in validated}) != len(validated):
            raise ValueError("Product names must be unique")
        for value in info.values():
            if not isinstance(value, str) or len(value) > 4000:
                raise ValueError("Business facts must be text under 4,000 characters")
        with self._connect() as conn:
            row = conn.execute("SELECT config_json FROM businesses WHERE id = ?", (business_id,)).fetchone()
            if row is None:
                raise KeyError(business_id)
            config = json.loads(self._reveal_data(row["config_json"]))
            config["products"] = products
            config["info"] = info
            conn.execute(
                "UPDATE businesses SET config_json = ? WHERE id = ?",
                (self._protect_data(json.dumps(config)), business_id),
            )

    def set_inventory(self, business_id: str, product_name: str, quantity: int, reason: str) -> dict[str, Any]:
        name = product_name.strip()
        note = reason.strip()
        if not name or quantity < 0:
            raise ValueError("A product name and non-negative quantity are required")
        if not note or len(note) > 500:
            raise ValueError("An inventory adjustment reason is required (max 500 characters)")
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT config_json FROM businesses WHERE id = ?", (business_id,)).fetchone()
            if row is None:
                raise KeyError(business_id)
            config = json.loads(self._reveal_data(row["config_json"]))
            catalog_name = next(
                (product["name"] for product in config.get("products", []) if product["name"].casefold() == name.casefold()),
                None,
            )
            if catalog_name is None:
                raise ValueError("Inventory can only be tracked for a product in this business catalog")
            for product in config.get("products", []):
                if product["name"] == catalog_name:
                    product["stock_quantity"] = quantity
                    product["in_stock"] = quantity > 0
                    break
            conn.execute(
                "UPDATE businesses SET config_json = ? WHERE id = ?",
                (self._protect_data(json.dumps(config)), business_id),
            )
            current = conn.execute(
                "SELECT quantity FROM inventory WHERE business_id = ? AND product_name = ?",
                (business_id, catalog_name),
            ).fetchone()
            previous = current["quantity"] if current else 0
            reserved = conn.execute(
                """
                SELECT COALESCE(SUM(quantity), 0) AS quantity
                FROM inventory_reservations
                WHERE business_id = ? AND product_name = ? AND status = 'active' AND expires_at > ?
                """,
                (business_id, catalog_name, now),
            ).fetchone()["quantity"]
            if quantity < reserved:
                raise ValueError(f"Stock count cannot be below {reserved} currently reserved units")
            conn.execute(
                """
                INSERT INTO inventory (business_id, product_name, quantity, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(business_id, product_name) DO UPDATE SET
                    quantity = excluded.quantity, updated_at = excluded.updated_at
                """,
                (business_id, catalog_name, quantity, now),
            )
            conn.execute(
                "INSERT INTO inventory_events (id, business_id, product_name, delta, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, business_id, catalog_name, quantity - previous, self._protect_data(note), now),
            )
        return {"product_name": catalog_name, "quantity": quantity, "updated_at": now}

    def inventory(self, business_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT inventory.product_name, inventory.quantity, inventory.updated_at,
                       COALESCE(SUM(
                           CASE WHEN inventory_reservations.status = 'active'
                                     AND inventory_reservations.expires_at > ?
                                THEN inventory_reservations.quantity ELSE 0 END
                       ), 0) AS reserved
                FROM inventory
                LEFT JOIN inventory_reservations
                  ON inventory_reservations.business_id = inventory.business_id
                 AND inventory_reservations.product_name = inventory.product_name
                WHERE inventory.business_id = ?
                GROUP BY inventory.business_id, inventory.product_name
                ORDER BY inventory.product_name COLLATE NOCASE
                """,
                (time.time(), business_id),
            ).fetchall()
        inventory = []
        for row in rows:
            item = dict(row)
            item["available"] = item["quantity"] - item["reserved"]
            inventory.append(item)
        return inventory

    def inventory_history(self, business_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, product_name, delta, reason, created_at FROM inventory_events "
                "WHERE business_id = ? ORDER BY created_at DESC LIMIT ?",
                (business_id, limit),
            ).fetchall()
        events = [dict(row) for row in rows]
        for event in events:
            event["reason"] = self._reveal_data(event["reason"])
        return events

    def reserve_order_inventory(
        self,
        business_id: str,
        order_id: str,
        items: list[dict[str, Any]],
        *,
        expires_at: float | None = None,
    ) -> None:
        now = time.time()
        expiry = expires_at or now + 30 * 60
        requested: dict[str, int] = {}
        for item in items:
            name = str(item["name"]).strip()
            quantity = int(item["quantity"])
            if not name or quantity < 1:
                raise ValueError("Inventory reservations require a product and positive quantity")
            requested[name] = requested.get(name, 0) + quantity
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT status FROM inventory_reservations WHERE business_id = ? AND order_id = ?",
                (business_id, order_id),
            ).fetchall()
            if any(row["status"] == "committed" for row in existing):
                raise ValueError("Paid order inventory cannot be changed")
            conn.execute(
                """
                UPDATE inventory_reservations SET status = 'released', updated_at = ?
                WHERE business_id = ? AND status = 'active' AND expires_at <= ?
                """,
                (now, business_id, now),
            )
            for name, quantity in requested.items():
                stock = conn.execute(
                    "SELECT quantity FROM inventory WHERE business_id = ? AND product_name = ? COLLATE NOCASE",
                    (business_id, name),
                ).fetchone()
                if stock is None:
                    continue
                other_reserved = conn.execute(
                    """
                    SELECT COALESCE(SUM(quantity), 0)
                    FROM inventory_reservations
                    WHERE business_id = ? AND product_name = ? COLLATE NOCASE
                      AND order_id != ? AND status = 'active' AND expires_at > ?
                    """,
                    (business_id, name, order_id, now),
                ).fetchone()[0]
                if quantity > stock["quantity"] - other_reserved:
                    raise ValueError(
                        f"Only {max(0, stock['quantity'] - other_reserved)} units of {name} are currently available"
                    )
            for row in conn.execute(
                "SELECT product_name FROM inventory_reservations WHERE business_id = ? AND order_id = ? "
                "AND status = 'active'",
                (business_id, order_id),
            ).fetchall():
                if row["product_name"] not in requested:
                    conn.execute(
                        "UPDATE inventory_reservations SET status = 'released', updated_at = ? "
                        "WHERE business_id = ? AND order_id = ? AND product_name = ? AND status = 'active'",
                        (now, business_id, order_id, row["product_name"]),
                    )
            for name, quantity in requested.items():
                if not conn.execute(
                    "SELECT 1 FROM inventory WHERE business_id = ? AND product_name = ? COLLATE NOCASE",
                    (business_id, name),
                ).fetchone():
                    continue
                conn.execute(
                    """
                    INSERT INTO inventory_reservations
                        (business_id, order_id, product_name, quantity, status, expires_at, updated_at)
                    VALUES (?, ?, ?, ?, 'active', ?, ?)
                    ON CONFLICT(business_id, order_id, product_name) DO UPDATE SET
                        quantity = excluded.quantity,
                        status = 'active',
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (business_id, order_id, name, quantity, expiry, now),
                )

    def release_order_inventory(self, business_id: str, order_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inventory_reservations SET status = 'released', updated_at = ?
                WHERE business_id = ? AND order_id = ? AND status = 'active'
                """,
                (time.time(), business_id, order_id),
            )

    def commit_order_inventory(
        self,
        business_id: str,
        order_id: str,
        items: list[dict[str, Any]],
    ) -> bool:
        now = time.time()
        requested = {str(item["name"]): int(item["quantity"]) for item in items}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            config_row = conn.execute(
                "SELECT config_json FROM businesses WHERE id = ?", (business_id,)
            ).fetchone()
            if config_row is None:
                return False
            committed = conn.execute(
                "SELECT product_name, quantity FROM inventory_reservations "
                "WHERE business_id = ? AND order_id = ? AND status = 'committed'",
                (business_id, order_id),
            ).fetchall()
            committed_by_name = {row["product_name"].casefold(): row["quantity"] for row in committed}
            tracked_requested = {
                name.casefold(): quantity
                for name, quantity in requested.items()
                if conn.execute(
                    "SELECT 1 FROM inventory WHERE business_id = ? AND product_name = ? COLLATE NOCASE",
                    (business_id, name),
                ).fetchone()
            }
            if tracked_requested and all(
                committed_by_name.get(name, 0) >= quantity for name, quantity in tracked_requested.items()
            ):
                return True
            for name, quantity in requested.items():
                stock = conn.execute(
                    "SELECT quantity FROM inventory WHERE business_id = ? AND product_name = ? COLLATE NOCASE",
                    (business_id, name),
                ).fetchone()
                if stock is None:
                    continue
                reservation = conn.execute(
                    """
                    SELECT quantity FROM inventory_reservations
                    WHERE business_id = ? AND order_id = ? AND product_name = ?
                      AND status = 'active' AND expires_at > ?
                    """,
                    (business_id, order_id, name, now),
                ).fetchone()
                if reservation is None or reservation["quantity"] < quantity or stock["quantity"] < quantity:
                    return False
            config = json.loads(self._reveal_data(config_row["config_json"]))
            for name, quantity in requested.items():
                stock = conn.execute(
                    "SELECT quantity FROM inventory WHERE business_id = ? AND product_name = ? COLLATE NOCASE",
                    (business_id, name),
                ).fetchone()
                if stock is None:
                    continue
                conn.execute(
                    "UPDATE inventory SET quantity = quantity - ?, updated_at = ? "
                    "WHERE business_id = ? AND product_name = ? COLLATE NOCASE",
                    (quantity, now, business_id, name),
                )
                conn.execute(
                    "UPDATE inventory_reservations SET status = 'committed', updated_at = ? "
                    "WHERE business_id = ? AND order_id = ? AND product_name = ? AND status = 'active'",
                    (now, business_id, order_id, name),
                )
                conn.execute(
                    "INSERT INTO inventory_events (id, business_id, product_name, delta, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        business_id,
                        name,
                        -quantity,
                        self._protect_data(f"Paid order {order_id}"),
                        now,
                    ),
                )
                for product in config.get("products", []):
                    if product["name"].casefold() == name.casefold():
                        remaining = stock["quantity"] - quantity
                        product["stock_quantity"] = remaining
                        product["in_stock"] = remaining > 0
                        break
            conn.execute(
                "UPDATE businesses SET config_json = ? WHERE id = ?",
                (self._protect_data(json.dumps(config)), business_id),
            )
        return True

    def expire_inventory_reservations(self, *, now: float | None = None) -> int:
        current_time = time.time() if now is None else now
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE inventory_reservations SET status = 'released', updated_at = ?
                WHERE status = 'active' AND expires_at <= ?
                """,
                (current_time, current_time),
            )
        return cursor.rowcount

    def booking_settings(self, business_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT booking_timezone, booking_hours_json FROM businesses WHERE id = ?",
                (business_id,),
            ).fetchone()
        if row is None:
            raise KeyError(business_id)
        return {
            "timezone": row["booking_timezone"],
            "weekly_hours": json.loads(row["booking_hours_json"]),
            "slot_duration_minutes": 30,
            "enabled": bool(json.loads(row["booking_hours_json"])),
        }

    def save_booking_settings(
        self,
        business_id: str,
        *,
        timezone_name: str,
        weekly_hours: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Timezone must be a valid IANA timezone") from exc
        if not isinstance(weekly_hours, dict) or set(weekly_hours) - {str(day) for day in range(7)}:
            raise ValueError("Weekly hours must use weekday keys from 0 (Monday) to 6 (Sunday)")
        normalized: dict[str, dict[str, str]] = {}
        for day, period in weekly_hours.items():
            if not isinstance(period, dict) or set(period) != {"open", "close"}:
                raise ValueError("Each open weekday must have open and close times")
            try:
                opening = datetime_time.fromisoformat(period["open"])
                closing = datetime_time.fromisoformat(period["close"])
            except (TypeError, ValueError) as exc:
                raise ValueError("Opening hours must use HH:MM 24-hour times") from exc
            if opening.second or closing.second or opening >= closing:
                raise ValueError("Opening time must be earlier than closing time")
            normalized[day] = {
                "open": opening.strftime("%H:%M"),
                "close": closing.strftime("%H:%M"),
            }
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE businesses SET booking_timezone = ?, booking_hours_json = ? WHERE id = ?",
                (timezone_name, json.dumps(normalized, sort_keys=True), business_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(business_id)
        return self.booking_settings(business_id)

    def available_booking_slots(self, business_id: str, day: date, *, now: float | None = None) -> list[dict[str, Any]]:
        settings = self.booking_settings(business_id)
        hours = settings["weekly_hours"].get(str(day.weekday()))
        if hours is None:
            return []
        timezone = ZoneInfo(settings["timezone"])
        opening = datetime.combine(day, datetime_time.fromisoformat(hours["open"]), tzinfo=timezone)
        closing = datetime.combine(day, datetime_time.fromisoformat(hours["close"]), tzinfo=timezone)
        slot = timedelta(minutes=settings["slot_duration_minutes"])
        current_time = time.time() if now is None else now
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT starts_at, duration_minutes FROM bookings
                WHERE business_id = ? AND status = 'confirmed'
                  AND starts_at < ? AND starts_at + duration_minutes * 60 > ?
                """,
                (
                    business_id,
                    closing.astimezone(ZoneInfo("UTC")).timestamp(),
                    opening.astimezone(ZoneInfo("UTC")).timestamp(),
                ),
            ).fetchall()
        busy = [
            (row["starts_at"], row["starts_at"] + row["duration_minutes"] * 60)
            for row in existing
        ]
        slots = []
        cursor = opening
        while cursor + slot <= closing:
            start_epoch = cursor.astimezone(ZoneInfo("UTC")).timestamp()
            end_epoch = (cursor + slot).astimezone(ZoneInfo("UTC")).timestamp()
            if start_epoch > current_time and not any(start < end_epoch and end > start_epoch for start, end in busy):
                slots.append(
                    {
                        "starts_at": datetime.fromtimestamp(start_epoch, ZoneInfo("UTC")).isoformat(),
                        "local_time": cursor.strftime("%H:%M"),
                    }
                )
            cursor += slot
        return slots

    def create_booking(
        self,
        business_id: str,
        *,
        service: str,
        customer_name: str,
        customer_phone: str,
        starts_at: float,
        duration_minutes: int,
        notes: str = "",
        require_business_hours: bool = False,
    ) -> dict[str, Any]:
        self._require_encryption_key()
        service = service.strip()
        customer_name = customer_name.strip()
        if not service or len(service) > 120 or not customer_name or len(customer_name) > 120:
            raise ValueError("A service and customer name are required (max 120 characters each)")
        if not customer_phone.strip() or len(customer_phone) > 40:
            raise ValueError("A valid customer phone number is required")
        if not 5 <= duration_minutes <= 1440:
            raise ValueError("Booking duration must be between 5 minutes and 24 hours")
        if len(notes) > 2000:
            raise ValueError("Booking notes cannot exceed 2,000 characters")
        booking_id = uuid.uuid4().hex
        now = time.time()
        ends_at = starts_at + duration_minutes * 60
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM businesses WHERE id = ?", (business_id,)).fetchone():
                raise KeyError(business_id)
            if require_business_hours:
                settings = conn.execute(
                    "SELECT booking_timezone, booking_hours_json FROM businesses WHERE id = ?",
                    (business_id,),
                ).fetchone()
                timezone = ZoneInfo(settings["booking_timezone"])
                local_start = datetime.fromtimestamp(starts_at, timezone)
                hours = json.loads(settings["booking_hours_json"]).get(str(local_start.weekday()))
                if (
                    hours is None
                    or duration_minutes != 30
                    or local_start.minute % 30
                    or local_start.second
                    or local_start.microsecond
                ):
                    raise ValueError("That time is outside the configured booking slots")
                local_open = datetime.combine(
                    local_start.date(), datetime_time.fromisoformat(hours["open"]), tzinfo=timezone
                )
                local_close = datetime.combine(
                    local_start.date(), datetime_time.fromisoformat(hours["close"]), tzinfo=timezone
                )
                if local_start < local_open or local_start + timedelta(minutes=duration_minutes) > local_close:
                    raise ValueError("That time is outside the configured booking hours")
            conflict = conn.execute(
                """
                SELECT 1 FROM bookings
                WHERE business_id = ? AND status = 'confirmed'
                  AND starts_at < ? AND starts_at + duration_minutes * 60 > ?
                LIMIT 1
                """,
                (business_id, ends_at, starts_at),
            ).fetchone()
            if conflict:
                raise ValueError("That time already has another confirmed booking")
            conn.execute(
                """
                INSERT INTO bookings
                    (id, business_id, service, customer_name, customer_phone, starts_at,
                     duration_minutes, status, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)
                """,
                (
                    booking_id,
                    business_id,
                    service,
                    self._protect_data(customer_name),
                    self._protect_data(customer_phone.strip()),
                    starts_at,
                    duration_minutes,
                    self._protect_data(notes.strip()),
                    now,
                ),
            )
        return self.get_booking(business_id, booking_id)

    def list_bookings(self, business_id: str, *, start_at: float | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if start_at is None:
                rows = conn.execute(
                    "SELECT * FROM bookings WHERE business_id = ? ORDER BY starts_at LIMIT ?",
                    (business_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bookings WHERE business_id = ? AND starts_at >= ? ORDER BY starts_at LIMIT ?",
                    (business_id, start_at, limit),
                ).fetchall()
        return [self._booking_from_row(row) for row in rows]

    def get_booking(self, business_id: str, booking_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bookings WHERE business_id = ? AND id = ?",
                (business_id, booking_id),
            ).fetchone()
        return self._booking_from_row(row) if row else None

    def update_booking_status(self, business_id: str, booking_id: str, status: str) -> bool:
        if status not in {"confirmed", "cancelled", "completed"}:
            raise ValueError("Invalid booking status")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            booking = conn.execute(
                "SELECT starts_at, duration_minutes, status FROM bookings WHERE business_id = ? AND id = ?",
                (business_id, booking_id),
            ).fetchone()
            if booking is None:
                return False
            if status == "confirmed" and booking["status"] != "confirmed":
                conflict = conn.execute(
                    """
                    SELECT 1 FROM bookings
                    WHERE business_id = ? AND id != ? AND status = 'confirmed'
                      AND starts_at < ? AND starts_at + duration_minutes * 60 > ?
                    LIMIT 1
                    """,
                    (
                        business_id,
                        booking_id,
                        booking["starts_at"] + booking["duration_minutes"] * 60,
                        booking["starts_at"],
                    ),
                ).fetchone()
                if conflict:
                    raise ValueError("That time has another confirmed booking")
            cursor = conn.execute(
                "UPDATE bookings SET status = ? WHERE business_id = ? AND id = ?",
                (status, business_id, booking_id),
            )
        return cursor.rowcount == 1

    def cancel_customer_booking(self, business_id: str, booking_id: str, phone: str) -> bool:
        supplied = self.normalize_whatsapp_phone(phone)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT customer_phone, status FROM bookings WHERE business_id = ? AND id = ?",
                (business_id, booking_id),
            ).fetchone()
            if row is None or row["status"] != "confirmed":
                return False
            if not secrets.compare_digest(
                self.normalize_whatsapp_phone(self._reveal_data(row["customer_phone"])),
                supplied,
            ):
                return False
            cursor = conn.execute(
                "UPDATE bookings SET status = 'cancelled' "
                "WHERE business_id = ? AND id = ? AND status = 'confirmed'",
                (business_id, booking_id),
            )
        return cursor.rowcount == 1

    def _booking_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        booking = dict(row)
        for key in ("customer_name", "customer_phone", "notes"):
            booking[key] = self._reveal_data(booking[key])
        return booking

    def documents(self, business_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, created_at FROM knowledge_documents WHERE business_id = ? "
                "ORDER BY created_at DESC",
                (business_id,),
            ).fetchall()
        documents = [dict(row) for row in rows]
        for document in documents:
            document["title"] = self._reveal_data(document["title"])
        return documents

    def delete_document(self, business_id: str, document_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM knowledge_documents WHERE id = ? AND business_id = ?",
                (document_id, business_id),
            )
        return cursor.rowcount == 1

    def integrations(self, business_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT whatsapp_phone_number_id, whatsapp_token_enc, whatsapp_app_secret_enc, "
                "whatsapp_verify_token_enc, mpesa_credentials_enc, llm_credentials_enc, "
                "embedding_consent, llm_data_consent "
                "FROM businesses WHERE id = ?",
                (business_id,),
            ).fetchone()
        if row is None:
            raise KeyError(business_id)
        return {
            "whatsapp_phone_number_id": row["whatsapp_phone_number_id"] or "",
            "whatsapp_token": self._decrypt(row["whatsapp_token_enc"]),
            "whatsapp_app_secret": self._decrypt(row["whatsapp_app_secret_enc"]),
            "whatsapp_verify_token": self._decrypt(row["whatsapp_verify_token_enc"]),
            "mpesa": json.loads(self._decrypt(row["mpesa_credentials_enc"]) or "{}"),
            "llm": json.loads(self._decrypt(row["llm_credentials_enc"]) or "{}"),
            "embedding_consent": bool(row["embedding_consent"]),
            "llm_data_consent": bool(row["llm_data_consent"]),
        }

    def integration_status(self, business_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT whatsapp_phone_number_id, whatsapp_token_enc,
                       whatsapp_app_secret_enc, whatsapp_verify_token_enc,
                       mpesa_credentials_enc, llm_credentials_enc,
                       embedding_consent, llm_data_consent
                FROM businesses WHERE id = ?
                """,
                (business_id,),
            ).fetchone()
        if row is None:
            raise KeyError(business_id)
        llm = json.loads(self._decrypt(row["llm_credentials_enc"]) or "{}")
        mpesa = json.loads(self._decrypt(row["mpesa_credentials_enc"]) or "{}")
        return {
            "whatsapp_connected": bool(row["whatsapp_phone_number_id"] and row["whatsapp_token_enc"]),
            "whatsapp_phone_number_id": row["whatsapp_phone_number_id"] or "",
            "whatsapp_webhook_secured": bool(
                row["whatsapp_app_secret_enc"] and row["whatsapp_verify_token_enc"]
            ),
            "mpesa_connected": all(
                mpesa.get(key)
                for key in (
                    "MPESA_CONSUMER_KEY",
                    "MPESA_CONSUMER_SECRET",
                    "MPESA_SHORTCODE",
                    "MPESA_PASSKEY",
                    "MPESA_CALLBACK_URL",
                )
            ),
            "embeddings_configured": bool(llm.get("embedding_api_key") and row["embedding_consent"]),
            "embedding_consent": bool(row["embedding_consent"]),
            "llm_data_consent": bool(row["llm_data_consent"]),
            "mpesa_environment": mpesa.get("MPESA_ENVIRONMENT", "sandbox"),
            "mpesa_shortcode": mpesa.get("MPESA_SHORTCODE", ""),
            "embedding_base_url": llm.get("embedding_base_url", "https://api.openai.com/v1"),
            "embedding_model": llm.get("embedding_model", "text-embedding-3-small"),
            "chat_base_url": llm.get("chat_base_url", "https://api.openai.com/v1"),
            "chat_model": llm.get("chat_model", "gpt-4o-mini"),
        }

    def whatsapp_app_secret(self, business_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT whatsapp_app_secret_enc FROM businesses WHERE id = ?",
                (business_id,),
            ).fetchone()
        return self._decrypt(row["whatsapp_app_secret_enc"]) if row else ""

    def verify_whatsapp_token(self, candidate: str) -> bool:
        if not candidate:
            return False
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT whatsapp_verify_token_enc FROM businesses "
                "WHERE whatsapp_verify_token_enc IS NOT NULL"
            ).fetchall()
        return any(
            secrets.compare_digest(candidate, self._decrypt(row["whatsapp_verify_token_enc"]))
            for row in rows
        )

    def authenticate(self, email: str, password: str) -> tuple[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, business_id, password_hash, email_verified_at FROM users WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        if row and row["email_verified_at"] is not None and _verify_password(password, row["password_hash"]):
            return row["id"], row["business_id"]
        return None

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (digest, user_id, time.time() + _SESSION_SECONDS),
            )
        return token

    def session_user(self, token: str) -> dict[str, str] | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
            row = conn.execute(
                """
                SELECT users.id, users.business_id, users.email, users.role
                FROM sessions JOIN users ON sessions.user_id = users.id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                  AND users.email_verified_at IS NOT NULL
                """,
                (digest, time.time()),
            ).fetchone()
        return dict(row) if row else None

    def consume_auth_attempt(
        self,
        bucket: str,
        *,
        limit: int,
        window_seconds: int,
        now: float | None = None,
    ) -> tuple[bool, int]:
        if limit < 1 or window_seconds < 1:
            raise ValueError("Rate limit and window must be positive")
        current_time = time.time() if now is None else now
        bucket_hash = hashlib.sha256(bucket.encode()).hexdigest()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT window_start, attempts FROM auth_rate_limits WHERE bucket_hash = ?",
                (bucket_hash,),
            ).fetchone()
            if row is None or row["window_start"] + window_seconds <= current_time:
                conn.execute(
                    """
                    INSERT INTO auth_rate_limits (bucket_hash, window_start, attempts)
                    VALUES (?, ?, 1)
                    ON CONFLICT(bucket_hash) DO UPDATE SET
                        window_start = excluded.window_start, attempts = 1
                    """,
                    (bucket_hash, current_time),
                )
                return True, 0
            retry_after = max(1, int(row["window_start"] + window_seconds - current_time))
            if row["attempts"] >= limit:
                return False, retry_after
            conn.execute(
                "UPDATE auth_rate_limits SET attempts = attempts + 1 WHERE bucket_hash = ?",
                (bucket_hash,),
            )
        return True, 0

    def delete_session(self, token: str) -> None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))

    def list_handoffs(self, business_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM handoffs WHERE business_id = ? ORDER BY created_at DESC LIMIT ?",
                (business_id, limit),
            ).fetchall()
        handoffs = [dict(row) for row in rows]
        for handoff in handoffs:
            handoff["reason"] = self._reveal_data(handoff["reason"])
            if handoff["assigned_to"]:
                handoff["assigned_to"] = self._reveal_data(handoff["assigned_to"])
        return handoffs

    def create_handoff(self, business_id: str, conversation_id: str, reason: str) -> str:
        handoff_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO handoffs
                    (id, business_id, conversation_id, reason, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'open', ?, ?)
                """,
                (handoff_id, business_id, conversation_id, self._protect_data(reason), now, now),
            )
        return handoff_id

    def ensure_handoff(self, business_id: str, conversation_id: str, reason: str) -> tuple[str, bool]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM handoffs WHERE business_id = ? AND conversation_id = ? AND status != 'resolved'",
                (business_id, conversation_id),
            ).fetchone()
            if row:
                return row["id"], False
            handoff_id = uuid.uuid4().hex
            now = time.time()
            conn.execute(
                """
                INSERT INTO handoffs
                    (id, business_id, conversation_id, reason, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    handoff_id,
                    business_id,
                    conversation_id,
                    self._protect_data(reason[:500]),
                    now,
                    now,
                ),
            )
        return handoff_id, True

    def update_handoff(
        self, business_id: str, handoff_id: str, status: str, assigned_to: str | None = None
    ) -> bool:
        if status not in {"open", "assigned", "resolved"}:
            raise ValueError("Invalid handoff status")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE handoffs SET status = ?, assigned_to = ?, updated_at = ?
                WHERE id = ? AND business_id = ?
                """,
                (
                    status,
                    self._protect_data(assigned_to) if assigned_to else None,
                    time.time(),
                    handoff_id,
                    business_id,
                ),
            )
        return cursor.rowcount == 1

    def claim_inbound(self, business_id: str, message_id: str) -> bool:
        if not message_id:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO inbound_events (business_id, message_id, status, received_at)
                VALUES (?, ?, 'processing', ?)
                ON CONFLICT(business_id, message_id) DO UPDATE SET
                    status = 'processing', received_at = excluded.received_at
                WHERE inbound_events.status = 'failed'
                   OR (inbound_events.status = 'processing' AND inbound_events.received_at < ?)
                """,
                (business_id, message_id, time.time(), time.time() - 300),
            )
        return cursor.rowcount == 1

    def finish_inbound(self, business_id: str, message_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE inbound_events SET status = ? WHERE business_id = ? AND message_id = ?",
                (status, business_id, message_id),
            )

    def enqueue_outbound(self, business_id: str, phone: str, text: str, phone_number_id: str) -> str:
        message_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO outbound_messages
                    (id, business_id, to_phone, text, phone_number_id, next_attempt_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    business_id,
                    self._protect_data(phone),
                    self._protect_data(text),
                    phone_number_id,
                    time.time(),
                    time.time(),
                ),
            )
        return message_id

    def ready_outbound(self, limit: int = 20, *, lease_seconds: int = 300) -> list[dict[str, Any]]:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM outbound_messages
                WHERE (status = 'pending' AND next_attempt_at <= ?)
                   OR (status = 'processing' AND locked_until <= ?)
                ORDER BY created_at LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                token = secrets.token_urlsafe(24)
                cursor = conn.execute(
                    """
                    UPDATE outbound_messages
                    SET status = 'processing', locked_until = ?, claim_token = ?
                    WHERE id = ? AND (
                        (status = 'pending' AND next_attempt_at <= ?)
                        OR (status = 'processing' AND locked_until <= ?)
                    )
                    """,
                    (now + lease_seconds, token, row["id"], now, now),
                )
                if cursor.rowcount == 1:
                    message = dict(row)
                    message["to_phone"] = self._reveal_data(message["to_phone"])
                    message["text"] = self._reveal_data(message["text"])
                    message["claim_token"] = token
                    claimed.append(message)
        return claimed

    def finish_outbound(
        self,
        message_id: str,
        *,
        claim_token: str | None = None,
        success: bool,
        error: str = "",
    ) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM outbound_messages "
                "WHERE id = ? AND status = 'processing' AND claim_token = ?",
                (message_id, claim_token),
            ).fetchone()
            if row is None:
                return
            attempts = row["attempts"] + 1
            done = success or attempts >= 8
            conn.execute(
                """
                UPDATE outbound_messages
                SET attempts = ?, status = ?, last_error = ?, next_attempt_at = ?,
                    locked_until = NULL, claim_token = NULL
                WHERE id = ? AND status = 'processing' AND claim_token = ?
                """,
                (
                    attempts,
                    "sent" if success else ("failed" if done else "pending"),
                    None if success else error[:500],
                    time.time() + min(3600, 5 * (2 ** min(attempts, 9))),
                    message_id,
                    claim_token,
                ),
            )

    @staticmethod
    def normalize_whatsapp_phone(phone: str) -> str:
        normalized = re.sub(r"\D", "", phone)
        if not 8 <= len(normalized) <= 15:
            raise ValueError("WhatsApp phone number must include 8 to 15 digits")
        return normalized

    def save_whatsapp_template(
        self,
        business_id: str,
        *,
        name: str,
        language: str,
        category: str,
        body_parameter_count: int,
    ) -> str:
        self._require_encryption_key()
        name = name.strip().lower()
        language = language.strip()
        if not re.fullmatch(r"[a-z0-9_]{1,512}", name):
            raise ValueError("Template name must use lowercase letters, digits, or underscores")
        if not re.fullmatch(r"[a-z]{2,3}(?:_[A-Z]{2})?", language):
            raise ValueError("Template language must be a locale such as en or en_US")
        if category not in {"utility", "marketing"}:
            raise ValueError("Invalid WhatsApp template category")
        if not 0 <= body_parameter_count <= 10:
            raise ValueError("Template can contain 0 to 10 body parameters")
        template_id = uuid.uuid4().hex
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO whatsapp_templates
                        (id, business_id, name, language, category, body_parameter_count, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (template_id, business_id, name, language, category, body_parameter_count, time.time()),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Template already exists or merchant was not found") from exc
        return template_id

    def whatsapp_templates(self, business_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, language, category, body_parameter_count, active, created_at "
                "FROM whatsapp_templates WHERE business_id = ? ORDER BY name",
                (business_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_whatsapp_template_active(self, business_id: str, template_id: str, active: bool) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE whatsapp_templates SET active = ? WHERE business_id = ? AND id = ?",
                (int(active), business_id, template_id),
            )
            if cursor.rowcount != 1:
                return False
            if not active:
                conn.execute(
                    "UPDATE scheduled_followups SET status = 'suppressed', last_error = 'Template is no longer approved' "
                    "WHERE business_id = ? AND template_id = ? AND status = 'pending'",
                    (business_id, template_id),
                )
        return True

    def record_whatsapp_consent(self, business_id: str, phone: str, *, opted_in: bool, source: str) -> None:
        self._require_encryption_key()
        normalized = self.normalize_whatsapp_phone(phone)
        source = source.strip()
        if not source or len(source) > 500:
            raise ValueError("Record where and how the customer gave or withdrew consent")
        phone_hash = hashlib.sha256(normalized.encode()).hexdigest()
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM businesses WHERE id = ?", (business_id,)).fetchone():
                raise KeyError(business_id)
            conn.execute(
                """
                INSERT INTO whatsapp_consents
                    (business_id, phone_hash, phone_enc, status, source_enc, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(business_id, phone_hash) DO UPDATE SET
                    phone_enc = excluded.phone_enc, status = excluded.status,
                    source_enc = excluded.source_enc, updated_at = excluded.updated_at
                """,
                (
                    business_id,
                    phone_hash,
                    self._protect_data(normalized),
                    "opted_in" if opted_in else "opted_out",
                    self._protect_data(source),
                    time.time(),
                ),
            )

    def has_whatsapp_consent(self, business_id: str, phone: str) -> bool:
        normalized = self.normalize_whatsapp_phone(phone)
        phone_hash = hashlib.sha256(normalized.encode()).hexdigest()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM whatsapp_consents WHERE business_id = ? AND phone_hash = ?",
                (business_id, phone_hash),
            ).fetchone()
        return row is not None and row["status"] == "opted_in"

    def schedule_followup(
        self,
        business_id: str,
        *,
        template_id: str,
        phone: str,
        parameters: list[str],
        send_at: float,
    ) -> str:
        normalized = self.normalize_whatsapp_phone(phone)
        phone_hash = hashlib.sha256(normalized.encode()).hexdigest()
        followup_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            template = conn.execute(
                "SELECT body_parameter_count FROM whatsapp_templates "
                "WHERE id = ? AND business_id = ? AND active = 1",
                (template_id, business_id),
            ).fetchone()
            if template is None:
                raise ValueError("Approved template not found for this business")
            if template["body_parameter_count"] != len(parameters):
                raise ValueError("Template parameter count does not match the approved template")
            consent = conn.execute(
                "SELECT status FROM whatsapp_consents WHERE business_id = ? AND phone_hash = ?",
                (business_id, phone_hash),
            ).fetchone()
            if consent is None or consent["status"] != "opted_in":
                raise ValueError("Customer has not opted in to WhatsApp follow-ups")
            conn.execute(
                """
                INSERT INTO scheduled_followups
                    (id, business_id, template_id, phone_hash, phone_enc, parameters_enc, send_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    followup_id,
                    business_id,
                    template_id,
                    phone_hash,
                    self._protect_data(normalized),
                    self._protect_data(json.dumps(parameters)),
                    send_at,
                    now,
                ),
            )
        return followup_id

    def list_followups(self, business_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT f.id, f.phone_enc, f.parameters_enc, f.send_at, f.status, f.last_error,
                       t.name AS template_name
                FROM scheduled_followups f
                JOIN whatsapp_templates t ON t.id = f.template_id
                WHERE f.business_id = ? ORDER BY f.send_at DESC LIMIT ?
                """,
                (business_id, limit),
            ).fetchall()
        followups = []
        for row in rows:
            item = dict(row)
            item["phone"] = self._reveal_data(item.pop("phone_enc"))
            item["parameters"] = json.loads(self._reveal_data(item.pop("parameters_enc")))
            followups.append(item)
        return followups

    def cancel_followup(self, business_id: str, followup_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE scheduled_followups SET status = 'cancelled' "
                "WHERE business_id = ? AND id = ? AND status = 'pending'",
                (business_id, followup_id),
            )
        return cursor.rowcount == 1

    def ready_followups(self, limit: int = 20, *, lease_seconds: int = 300) -> list[dict[str, Any]]:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT f.*, t.name AS template_name, t.language, t.body_parameter_count,
                       b.whatsapp_phone_number_id
                FROM scheduled_followups f
                JOIN whatsapp_templates t ON t.id = f.template_id AND t.business_id = f.business_id
                    AND t.active = 1
                JOIN businesses b ON b.id = f.business_id
                WHERE (f.status = 'pending' AND f.send_at <= ?)
                   OR (f.status = 'processing' AND f.locked_until <= ?)
                ORDER BY f.send_at LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                consent = conn.execute(
                    "SELECT status FROM whatsapp_consents WHERE business_id = ? AND phone_hash = ?",
                    (row["business_id"], row["phone_hash"]),
                ).fetchone()
                if consent is None or consent["status"] != "opted_in":
                    conn.execute(
                        "UPDATE scheduled_followups SET status = 'suppressed', last_error = 'Customer consent was withdrawn' "
                        "WHERE id = ?",
                        (row["id"],),
                    )
                    continue
                claim_token = secrets.token_urlsafe(24)
                cursor = conn.execute(
                    """
                    UPDATE scheduled_followups
                    SET status = 'processing', locked_until = ?, claim_token = ?
                    WHERE id = ? AND (
                        (status = 'pending' AND send_at <= ?)
                        OR (status = 'processing' AND locked_until <= ?)
                    )
                    """,
                    (now + lease_seconds, claim_token, row["id"], now, now),
                )
                if cursor.rowcount:
                    item = dict(row)
                    item["phone"] = self._reveal_data(item["phone_enc"])
                    item["parameters"] = json.loads(self._reveal_data(item["parameters_enc"]))
                    item["claim_token"] = claim_token
                    claimed.append(item)
        return claimed

    def finish_followup(
        self,
        followup_id: str,
        *,
        claim_token: str,
        success: bool,
        error: str = "",
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM scheduled_followups "
                "WHERE id = ? AND status = 'processing' AND claim_token = ?",
                (followup_id, claim_token),
            ).fetchone()
            if row is None:
                return
            attempts = row["attempts"] + 1
            status = "sent" if success else ("failed" if attempts >= 8 else "pending")
            conn.execute(
                """
                UPDATE scheduled_followups
                SET attempts = ?, status = ?, last_error = ?, send_at = ?,
                    locked_until = NULL, claim_token = NULL
                WHERE id = ? AND status = 'processing' AND claim_token = ?
                """,
                (
                    attempts,
                    status,
                    None if success else error[:500],
                    now + min(3600, 5 * (2 ** min(attempts, 9))) if not success else now,
                    followup_id,
                    claim_token,
                ),
            )

    def add_document(self, business_id: str, title: str, chunks: list[tuple[str, list[float] | None]]) -> str:
        document_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO knowledge_documents (id, business_id, title, created_at) VALUES (?, ?, ?, ?)",
                (document_id, business_id, self._protect_data(title), now),
            )
            for position, (text, embedding) in enumerate(chunks):
                conn.execute(
                    """
                    INSERT INTO knowledge_chunks
                        (id, document_id, business_id, position, text, embedding_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        document_id,
                        business_id,
                        position,
                        self._protect_data(text),
                        self._protect_data(json.dumps(embedding)) if embedding is not None else None,
                    ),
                )
        return document_id

    def list_chunks(self, business_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT knowledge_chunks.id, knowledge_chunks.document_id,
                       knowledge_documents.title, knowledge_chunks.text,
                       knowledge_chunks.embedding_json
                FROM knowledge_chunks
                JOIN knowledge_documents ON knowledge_documents.id = knowledge_chunks.document_id
                WHERE knowledge_chunks.business_id = ?
                """,
                (business_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["title"] = self._reveal_data(item["title"])
            item["text"] = self._reveal_data(item["text"])
            embedding_json = item.pop("embedding_json")
            item["embedding"] = (
                json.loads(self._reveal_data(embedding_json)) if embedding_json else None
            )
            result.append(item)
        return result

    def delete_business_data(self, business_id: str) -> None:
        with self._connect() as conn:
            conversation_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM conversations WHERE business_id = ?",
                    (business_id,),
                ).fetchall()
            ]
            for conversation_id in conversation_ids:
                conn.execute(
                    "DELETE FROM conversations WHERE id = ? AND business_id = ?",
                    (conversation_id, business_id),
                )
            for table in ("inbound_events", "handoffs", "outbound_messages", "inventory_events", "inventory", "bookings"):
                conn.execute(f"DELETE FROM {table} WHERE business_id = ?", (business_id,))
            conn.execute("DELETE FROM businesses WHERE id = ?", (business_id,))

    def purge_expired_conversations(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, business_id FROM conversations WHERE updated_at < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "DELETE FROM handoffs WHERE business_id = ? AND conversation_id = ?",
                    (row["business_id"], row["id"]),
                )
                conn.execute(
                    "DELETE FROM conversations WHERE id = ? AND business_id = ?",
                    (row["id"], row["business_id"]),
                )
            conn.execute("DELETE FROM inbound_events WHERE received_at < ?", (cutoff,))
            conn.execute(
                "DELETE FROM outbound_messages WHERE created_at < ? AND status IN ('sent', 'failed')",
                (cutoff,),
            )
        return len(rows)
