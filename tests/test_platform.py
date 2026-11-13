import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from hermes.app import create_app
from hermes.llm.mock import MockLLM
from hermes.maintenance import backup_database, restore_database
from hermes.models import PaymentStatus
from hermes.mpesa.base import STKPushResult
from hermes.mpesa.simulator import MpesaSimulator
from hermes.platform import PlatformStore, _verify_password
from hermes.runtime import Runtime
from hermes.worker import (
    process_billing_reconciliation,
    process_outbound_queue,
    process_scheduled_followups,
)


@pytest.fixture
def platform(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("HERMES_ENCRYPTION_KEY", key)
    return PlatformStore(tmp_path / "platform.db", key)


def create_test_app(runtime):
    verification_tokens = {}

    def send_verification(email, url):
        verification_tokens[email] = parse_qs(urlsplit(url).query)["verify"][0]

    app = create_app(runtime, verification_email_sender=send_verification)
    app.state.verification_tokens = verification_tokens
    return app


def register_verified(client, payload):
    registered = client.post("/auth/register", json=payload)
    if registered.status_code != 202:
        return registered
    email = payload["email"].strip().lower()
    token = client.app.state.verification_tokens[email]
    return client.post("/auth/verify-email", json={"token": token})


def test_invitation_onboarding_and_per_merchant_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_INVITE_TOKEN", "invite-secret")
    rt = Runtime(db_path=str(tmp_path / "onboarding.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(rt))
    assert client.get("/auth/session").json() == {"authenticated": False}

    denied = client.post(
        "/auth/register",
        json={
            "invite_code": "wrong",
            "name": "Shop One",
            "email": "owner@example.test",
            "password": "long-secure-password",
        },
    )
    assert denied.status_code == 403

    registered = register_verified(
        client,
        {
            "invite_code": "invite-secret",
            "name": "Shop One",
            "email": "owner@example.test",
            "password": "long-secure-password",
        },
    )
    assert registered.status_code == 200
    assert client.get("/auth/session").json() == {"authenticated": True}
    cookie = registered.cookies.get("hermes_session")
    assert cookie and "httponly" in registered.headers["set-cookie"].lower()

    dashboard = client.get("/api/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["business"]["name"] == "Shop One"
    assert dashboard.json()["metrics"]["total_conversations"] == 0
    assert dashboard.json()["subscription"]["status"] == "trialing"
    assert dashboard.json()["subscription"]["trial_days_remaining"] == 7

    client.post("/auth/logout")
    assert client.get("/auth/session").json() == {"authenticated": False}
    assert client.get("/api/dashboard").status_code == 401


def test_self_serve_registration_starts_premium_trial_without_invite(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_INVITE_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_PUBLIC_SIGNUP_ENABLED", "true")
    runtime = Runtime(db_path=str(tmp_path / "self-serve.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(runtime))

    assert client.get("/public/config").json() == {
        "invite_required": False,
        "signup_enabled": True,
        "local_email_verification": False,
        "email_verification_required": True,
        "premium_trial_days": 7,
        "premium_monthly_price_kes": 2500,
    }
    registered = register_verified(
        client,
        {
            "name": "Trial shop",
            "email": "trial@example.test",
            "password": "a-strong-password",
        },
    )
    assert registered.status_code == 200
    subscription = client.get("/api/dashboard").json()["subscription"]
    assert subscription["status"] == "trialing"
    assert subscription["premium_active"] is True
    assert subscription["trial_days_remaining"] == 7
    assert subscription["premium_monthly_price_kes"] == 2500


def test_email_verification_is_required_one_time_and_starts_trial_only_after_verify(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_INVITE_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_PUBLIC_SIGNUP_ENABLED", "true")
    runtime = Runtime(db_path=str(tmp_path / "verified-email.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(runtime))
    payload = {"name": "Verified shop", "email": "verified@example.test", "password": "valid-owner-password"}

    pending = client.post("/auth/register", json=payload)
    assert pending.status_code == 202
    assert "hermes_session" not in pending.cookies
    assert client.get("/api/dashboard").status_code == 401
    assert client.post("/auth/login", json={"email": payload["email"], "password": payload["password"]}).status_code == 401

    with runtime.platform._connect() as connection:
        business_id = connection.execute(
            "SELECT business_id FROM users WHERE email = ?",
            (payload["email"],),
        ).fetchone()["business_id"]
    before = runtime.platform.subscription(business_id)
    assert before["status"] == "pending_verification"
    assert before["trial_expires_at"] is None

    token = client.app.state.verification_tokens[payload["email"]]
    verified = client.post("/auth/verify-email", json={"token": token})
    assert verified.status_code == 200
    assert client.get("/api/dashboard").json()["subscription"]["status"] == "trialing"
    assert client.post("/auth/verify-email", json={"token": token}).status_code == 400
    client.post("/auth/logout")
    assert client.post(
        "/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
    ).status_code == 200


def test_resend_verification_rejects_non_https_public_url(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_INVITE_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_PUBLIC_SIGNUP_ENABLED", "true")
    runtime = Runtime(db_path=str(tmp_path / "resend-url.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(runtime))
    payload = {
        "name": "Pending shop",
        "email": "pending@example.test",
        "password": "valid-owner-password",
    }

    assert client.post("/auth/register", json=payload).status_code == 202
    original_token = client.app.state.verification_tokens[payload["email"]]
    monkeypatch.setenv("HERMES_PUBLIC_BASE_URL", "http://example.test")

    response = client.post("/auth/verification/resend", json={"email": payload["email"]})

    assert response.status_code == 503
    assert client.app.state.verification_tokens[payload["email"]] == original_token


def test_email_verification_token_expires(platform):
    user_id, _ = platform.create_business(
        name="Expired token shop",
        email="expired@example.test",
        password="test-password-value",
    )
    token = platform.create_email_verification_token(user_id)
    assert platform.verify_email_token(token, now=time.time() + 24 * 60 * 60 + 1) is None


def test_self_serve_registration_is_closed_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_INVITE_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_PUBLIC_SIGNUP_ENABLED", raising=False)
    runtime = Runtime(db_path=str(tmp_path / "closed-signup.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(runtime))

    assert client.get("/public/config").json()["signup_enabled"] is False
    response = client.post(
        "/auth/register",
        json={
            "name": "Unopened shop",
            "email": "closed@example.test",
            "password": "a-strong-password",
        },
    )
    assert response.status_code == 503


def test_local_signup_verification_is_available_only_from_loopback(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_INVITE_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_PUBLIC_SIGNUP_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_SMTP_HOST", raising=False)
    monkeypatch.delenv("HERMES_EMAIL_FROM", raising=False)
    monkeypatch.delenv("HERMES_ENVIRONMENT", raising=False)
    monkeypatch.setenv("HERMES_ENCRYPTION_KEY", Fernet.generate_key().decode())
    runtime = Runtime(db_path=str(tmp_path / "local-signup.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    assert runtime.default_business.name == "Hermes workspace"
    assert runtime.default_business.products == []
    assert "duka_demo" not in runtime.businesses
    client = TestClient(
        create_app(runtime),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    )

    config = client.get("/public/config").json()
    assert config["signup_enabled"] is True
    assert config["local_email_verification"] is True
    response = client.post(
        "/auth/register",
        json={
            "name": "Local merchant",
            "email": "local@example.test",
            "owner_phone": "+254 712 345 678",
            "password": "a-strong-password",
        },
    )
    assert response.status_code == 202
    result = response.json()
    assert result["delivery"] == "local_development"
    token = parse_qs(urlsplit(result["verification_url"]).query)["verify"][0]
    verified = client.post("/auth/verify-email", json={"token": token})
    assert verified.status_code == 200
    assert client.get("/api/dashboard").json()["business"]["products"] == []

    with runtime.platform._connect() as connection:
        owner_phone = connection.execute(
            "SELECT phone_enc FROM users WHERE email = ?",
            ("local@example.test",),
        ).fetchone()["phone_enc"]
    assert owner_phone.startswith("enc:")
    assert "+254 712 345 678" not in owner_phone

    production = TestClient(create_app(Runtime(
        db_path=str(tmp_path / "production-signup.db"),
        llm=MockLLM(),
        mpesa=MpesaSimulator(),
    )), base_url="https://hermes.example")
    monkeypatch.setenv("HERMES_ENVIRONMENT", "production")
    assert production.get("/public/config").json()["signup_enabled"] is False
    assert production.post(
        "/auth/register",
        json={
            "name": "Blocked merchant",
            "email": "blocked@example.test",
            "password": "a-strong-password",
        },
    ).status_code == 503


def test_catalog_csv_import_merges_and_export_is_spreadsheet_safe(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_INVITE_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_PUBLIC_SIGNUP_ENABLED", "true")
    runtime = Runtime(db_path=str(tmp_path / "catalog-sync.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(runtime))
    owner = register_verified(
        client,
        {
            "name": "Catalog shop",
            "email": "catalog@example.test",
            "password": "catalog-owner-password",
        },
    )
    assert owner.status_code == 200
    business_id = client.get("/api/dashboard").json()["business"]["id"]
    runtime.platform.save_catalog(
        business_id,
        products=[{"name": "Existing item", "price": 25, "unit": "", "aliases": [], "in_stock": True}],
        info={},
    )

    csv_content = (
        "name,price,unit,aliases,in_stock,stock_quantity\n"
        "Coffee,320,500g,java|kahawa,true,9\n"
        "=HYPERLINK(\"https://example.test\"),10,each,,true,\n"
    )
    imported = client.post(
        "/api/catalog/import",
        files={"file": ("catalog.csv", csv_content.encode(), "text/csv")},
    )
    assert imported.status_code == 200
    assert imported.json()["products_imported"] == 2
    assert imported.json()["merge_mode"] == "upsert"
    dashboard = client.get("/api/dashboard").json()
    assert {product["name"] for product in dashboard["business"]["products"]} == {
        "Existing item",
        "Coffee",
        '=HYPERLINK("https://example.test")',
    }
    assert dashboard["inventory"][0]["quantity"] == 9

    exported = client.get("/api/catalog/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK(" in exported.text


def test_catalog_import_holds_price_and_stock_conflicts_for_merchant_review(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_INVITE_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_PUBLIC_SIGNUP_ENABLED", "true")
    runtime = Runtime(db_path=str(tmp_path / "catalog-conflicts.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(runtime))
    owner = register_verified(
        client,
        {
            "name": "Conflict shop",
            "email": "conflict@example.test",
            "password": "conflict-owner-password",
        },
    )
    assert owner.status_code == 200
    business_id = client.get("/api/dashboard").json()["business"]["id"]
    products = [
        {"name": "Coffee", "price": 100, "unit": "", "aliases": [], "in_stock": True},
        {"name": "Tea", "price": 40, "unit": "", "aliases": [], "in_stock": True},
        {"name": "Flour", "price": 30, "unit": "", "aliases": [], "in_stock": True},
    ]
    runtime.platform.save_catalog(business_id, products=products, info={})
    runtime.platform.set_inventory(business_id, "Coffee", 5, "Initial count")
    runtime.platform.set_inventory(business_id, "Tea", 2, "Initial count")
    runtime.platform.set_inventory(business_id, "Flour", 7, "Initial count")

    imported = client.post(
        "/api/catalog/import",
        files={
            "file": (
                "catalog.csv",
                b"name,price,stock_quantity\nCoffee,120,5\nTea,40,1\nFlour,35,7\n",
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200
    assert imported.json()["conflicts_created"] == 3
    dashboard = client.get("/api/dashboard").json()
    current_products = {product["name"]: product for product in dashboard["business"]["products"]}
    assert current_products["Coffee"]["price"] == 100
    assert current_products["Tea"]["price"] == 40
    assert {item["product_name"]: item["quantity"] for item in dashboard["inventory"]} == {
        "Coffee": 5,
        "Tea": 2,
        "Flour": 7,
    }
    conflicts = {conflict["product_name"]: conflict for conflict in dashboard["catalog_conflicts"]}
    assert conflicts["Coffee"]["current"] == {"price": 100}
    assert conflicts["Coffee"]["incoming"] == {"price": 120.0}
    assert conflicts["Tea"]["current"] == {"stock_quantity": 2}
    assert conflicts["Tea"]["incoming"] == {"stock_quantity": 1}

    duplicate = client.post(
        "/api/catalog/import",
        files={"file": ("catalog.csv", b"name,price\nCoffee,125\n", "text/csv")},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["conflicts_created"] == 0
    assert duplicate.json()["conflicts_updated"] == 1
    refreshed = client.get("/api/dashboard").json()["catalog_conflicts"]
    coffee_conflict = next(conflict for conflict in refreshed if conflict["product_name"] == "Coffee")
    assert coffee_conflict["id"] == conflicts["Coffee"]["id"]
    assert coffee_conflict["incoming"] == {"price": 125.0}

    runtime.platform.save_catalog(
        business_id,
        products=[
            {"name": "Coffee", "price": 105, "unit": "", "aliases": [], "in_stock": True, "stock_quantity": 5},
            {"name": "Tea", "price": 40, "unit": "", "aliases": [], "in_stock": True, "stock_quantity": 2},
            {"name": "Flour", "price": 30, "unit": "", "aliases": [], "in_stock": True, "stock_quantity": 7},
        ],
        info={},
    )
    runtime.platform.set_inventory(business_id, "Coffee", 6, "Count corrected during review")
    stale = client.patch(
        f"/api/catalog/conflicts/{coffee_conflict['id']}",
        json={"resolution": "accept_incoming"},
    )
    assert stale.json()["status"] == "stale"
    refreshed_conflict = client.get("/api/dashboard").json()["catalog_conflicts"]
    coffee_conflict = next(conflict for conflict in refreshed_conflict if conflict["product_name"] == "Coffee")
    assert coffee_conflict["current"] == {"price": 105}

    accepted = client.patch(
        f"/api/catalog/conflicts/{coffee_conflict['id']}",
        json={"resolution": "accept_incoming"},
    )
    runtime.platform.reserve_order_inventory(business_id, "tea-order", [{"name": "Tea", "quantity": 2}])
    below_reserved = client.patch(
        f"/api/catalog/conflicts/{conflicts['Tea']['id']}",
        json={"resolution": "accept_incoming"},
    )
    assert below_reserved.status_code == 400
    assert "reserved units" in below_reserved.json()["detail"]
    runtime.platform.release_order_inventory(business_id, "tea-order")
    accepted_stock = client.patch(
        f"/api/catalog/conflicts/{conflicts['Tea']['id']}",
        json={"resolution": "accept_incoming"},
    )
    kept = client.patch(
        f"/api/catalog/conflicts/{conflicts['Flour']['id']}",
        json={"resolution": "keep_current"},
    )
    assert accepted.json()["status"] == "accepted"
    assert accepted_stock.json()["status"] == "accepted"
    assert kept.json()["status"] == "kept"
    dashboard = client.get("/api/dashboard").json()
    current_products = {product["name"]: product for product in dashboard["business"]["products"]}
    assert current_products["Coffee"]["price"] == 125
    assert current_products["Tea"]["price"] == 40
    assert current_products["Flour"]["price"] == 30
    assert {item["product_name"]: item["quantity"] for item in dashboard["inventory"]} == {
        "Coffee": 6,
        "Tea": 1,
        "Flour": 7,
    }
    assert dashboard["catalog_conflicts"] == []


def test_invalid_catalog_csv_does_not_apply_partial_changes_or_conflicts(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_INVITE_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_PUBLIC_SIGNUP_ENABLED", "true")
    runtime = Runtime(db_path=str(tmp_path / "invalid-catalog-import.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(runtime))
    owner = register_verified(
        client,
        {
            "name": "Invalid import shop",
            "email": "invalid-import@example.test",
            "password": "invalid-import-owner-password",
        },
    )
    assert owner.status_code == 200
    business_id = client.get("/api/dashboard").json()["business"]["id"]
    runtime.platform.save_catalog(
        business_id,
        products=[{"name": "Coffee", "price": 100, "unit": "", "aliases": [], "in_stock": True}],
        info={},
    )
    runtime.platform.set_inventory(business_id, "Coffee", 5, "Initial count")

    response = client.post(
        "/api/catalog/import",
        files={
            "file": (
                "invalid.csv",
                b"name,price,stock_quantity\nCoffee,120,4\nBroken,not-a-price,1\n",
                "text/csv",
            )
        },
    )
    assert response.status_code == 400
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["business"]["products"][0]["price"] == 100
    assert dashboard["inventory"][0]["quantity"] == 5
    assert dashboard["catalog_conflicts"] == []


def test_registration_rate_limit_returns_retry_after(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_INVITE_TOKEN", "invite-secret")
    runtime = Runtime(db_path=str(tmp_path / "registration-rate-limit.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(runtime))
    payload = {
        "invite_code": "wrong",
        "name": "Rate limit test",
        "email": "rate-limit@example.test",
        "password": "a-strong-password",
    }

    responses = [client.post("/auth/register", json=payload) for _ in range(3)]
    assert all(response.status_code == 403 for response in responses)

    limited = client.post("/auth/register", json=payload)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


def test_premium_trial_expires_to_free_without_deleting_workspace(platform):
    _, business_id = platform.create_business(
        name="Trial shop",
        email="trial@example.test",
        password="a-strong-password",
    )
    trial = platform.subscription(business_id)

    expired = platform.subscription(business_id, now=trial["trial_expires_at"])

    assert expired["status"] == "free"
    assert expired["premium_active"] is False
    assert expired["trial_days_remaining"] == 0
    assert platform.business(business_id).name == "Trial shop"
    assert platform.subscription(business_id)["status"] == "free"


def test_premium_subscription_has_seven_day_grace_period(platform):
    _, business_id = platform.create_business(
        name="Grace shop",
        email="grace@example.test",
        password="test-password-value",
    )
    expiry = time.time() + 100
    with platform._connect() as connection:
        connection.execute(
            "UPDATE businesses SET subscription_status = 'premium', subscription_expires_at = ? WHERE id = ?",
            (expiry, business_id),
        )

    grace = platform.subscription(business_id, now=expiry + 1)
    assert grace["status"] == "grace_period"
    assert grace["premium_active"] is True
    assert grace["grace_expires_at"] == expiry + 7 * 24 * 60 * 60
    expired = platform.subscription(business_id, now=expiry + 7 * 24 * 60 * 60 + 1)
    assert expired["status"] == "free"
    assert expired["premium_active"] is False


def test_inventory_counts_audit_and_bookings_are_tenant_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PUBLIC_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("HERMES_ENCRYPTION_KEY", Fernet.generate_key().decode())
    runtime = Runtime(db_path=str(tmp_path / "commerce-ops.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    app = create_test_app(runtime)
    client = TestClient(app)
    owner = register_verified(
        client,
        {"name": "First shop", "email": "first-ops@example.test", "password": "first-owner-password"},
    )
    assert owner.status_code == 200
    assert client.put(
        "/api/catalog",
        json={"products": [{"name": "Coffee", "price": 500}], "info": {}},
    ).status_code == 200

    adjustment = client.put(
        "/api/inventory",
        json={"product_name": "coffee", "quantity": 12, "reason": "Opening stock"},
    )
    assert adjustment.status_code == 200
    assert client.get("/api/dashboard").json()["inventory"][0]["quantity"] == 12
    assert client.get("/api/dashboard").json()["inventory_history"][0]["delta"] == 12
    assert client.put(
        "/api/inventory",
        json={"product_name": "Unknown item", "quantity": 2, "reason": "Count"},
    ).status_code == 400

    starts_at = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    booking_payload = {
        "service": "Consultation",
        "customer_name": "Customer One",
        "customer_phone": "254700000001",
        "starts_at": starts_at.isoformat(),
        "duration_minutes": 60,
        "notes": "First visit",
    }
    booking = client.post("/api/bookings", json=booking_payload)
    assert booking.status_code == 201
    assert booking.json()["customer_name"] == "Customer One"
    rescheduled_start = starts_at + timedelta(hours=2)
    edited = client.patch(
        f"/api/bookings/{booking.json()['id']}",
        json={
            "service": "Follow-up consultation",
            "customer_name": "Updated Customer",
            "starts_at": rescheduled_start.isoformat(),
            "duration_minutes": 45,
            "notes": "Bring previous results",
        },
    )
    assert edited.status_code == 200
    assert edited.json()["service"] == "Follow-up consultation"
    assert edited.json()["customer_name"] == "Updated Customer"
    assert edited.json()["duration_minutes"] == 45
    assert edited.json()["notes"] == "Bring previous results"
    assert edited.json()["starts_at"] == rescheduled_start.timestamp()
    overlapping = client.post(
        "/api/bookings",
        json={
            **booking_payload,
            "customer_name": "Customer Two",
            "starts_at": (rescheduled_start + timedelta(minutes=30)).isoformat(),
        },
    )
    assert overlapping.status_code == 409
    self_overlap = client.patch(
        f"/api/bookings/{booking.json()['id']}",
        json={"starts_at": (rescheduled_start + timedelta(minutes=15)).isoformat()},
    )
    assert self_overlap.status_code == 200
    assert self_overlap.json()["starts_at"] == (rescheduled_start + timedelta(minutes=15)).timestamp()
    assert client.patch(f"/api/bookings/{booking.json()['id']}", json={"status": "cancelled"}).status_code == 200

    other_owner = TestClient(app)
    second_account = register_verified(
        other_owner,
        {"name": "Second shop", "email": "second-ops@example.test", "password": "second-owner-password"},
    )
    assert second_account.status_code == 200
    second_dashboard = other_owner.get("/api/dashboard").json()
    assert second_dashboard["inventory"] == []
    assert second_dashboard["bookings"] == []
    assert other_owner.patch(
        f"/api/bookings/{booking.json()['id']}",
        json={"service": "Unauthorized edit"},
    ).status_code == 404


def test_order_inventory_reservation_and_payment_deduction_are_idempotent(platform):
    _, business_id = platform.create_business(
        name="Stock shop",
        email="stock@example.test",
        password="secure-stock-password",
        products=[{"name": "Coffee", "price": 500}],
    )
    platform.set_inventory(business_id, "Coffee", 2, "Opening count")
    platform.reserve_order_inventory(business_id, "order-one", [{"name": "Coffee", "quantity": 1}])
    assert platform.inventory(business_id)[0]["reserved"] == 1
    assert platform.inventory(business_id)[0]["available"] == 1
    with pytest.raises(ValueError, match="currently available"):
        platform.reserve_order_inventory(business_id, "order-two", [{"name": "Coffee", "quantity": 2}])
    with pytest.raises(ValueError, match="reserved units"):
        platform.set_inventory(business_id, "Coffee", 0, "Incorrect count")

    items = [{"name": "Coffee", "quantity": 1}]
    assert platform.commit_order_inventory(business_id, "order-one", items) is True
    assert platform.inventory(business_id)[0]["quantity"] == 1
    assert platform.commit_order_inventory(business_id, "order-one", items) is True
    assert platform.inventory(business_id)[0]["quantity"] == 1
    assert platform.inventory_history(business_id)[0]["delta"] == -1


def test_last_unit_reservation_is_atomic_and_expiration_releases_it(platform):
    _, business_id = platform.create_business(
        name="Single stock shop",
        email="single-stock@example.test",
        password="secure-stock-password",
        products=[{"name": "Tea", "price": 100}],
    )
    platform.set_inventory(business_id, "Tea", 1, "Opening count")

    def reserve(order_id):
        try:
            platform.reserve_order_inventory(business_id, order_id, [{"name": "Tea", "quantity": 1}])
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, ("order-a", "order-b")))
    assert sorted(results) == [False, True]
    with platform._connect() as connection:
        active = connection.execute(
            "SELECT COUNT(*) FROM inventory_reservations WHERE business_id = ? AND status = 'active'",
            (business_id,),
        ).fetchone()[0]
    assert active == 1
    assert platform.expire_inventory_reservations(now=time.time() + 31 * 60) == 1
    assert platform.inventory(business_id)[0]["available"] == 1


def test_customer_self_booking_uses_configured_hours_and_phone_verified_cancellation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PUBLIC_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("HERMES_ENCRYPTION_KEY", Fernet.generate_key().decode())
    runtime = Runtime(db_path=str(tmp_path / "self-booking.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(runtime))
    owner = register_verified(
        client,
        {"name": "Appointment shop", "email": "appointment@example.test", "password": "appointment-owner-password"},
    )
    assert owner.status_code == 200
    business_id = client.get("/api/dashboard").json()["business"]["id"]
    day = datetime.now(timezone.utc).date() + timedelta(days=3)
    assert client.put(
        "/api/bookings/settings",
        json={
            "timezone": "Africa/Nairobi",
            "weekly_hours": {str(day.weekday()): {"open": "09:00", "close": "12:00"}},
        },
    ).status_code == 200
    page = client.get(f"/book/{business_id}")
    assert page.status_code == 200
    assert business_id in page.text

    slots = client.get(
        f"/public/bookings/{business_id}/availability",
        params={"day": day.isoformat()},
    )
    assert slots.status_code == 200
    assert [slot["local_time"] for slot in slots.json()["slots"]] == [
        "09:00", "09:30", "10:00", "10:30", "11:00", "11:30"
    ]
    first_slot = slots.json()["slots"][0]["starts_at"]
    payload = {
        "service": "Consultation",
        "customer_name": "A Customer",
        "customer_phone": "254700000003",
        "starts_at": first_slot,
    }
    created = client.post(f"/public/bookings/{business_id}", json=payload)
    assert created.status_code == 201
    booking_id = created.json()["id"]
    duplicate = client.post(f"/public/bookings/{business_id}", json=payload)
    assert duplicate.status_code == 409
    wrong_phone = client.post(
        f"/public/bookings/{business_id}/{booking_id}/cancel",
        json={"phone": "254700000004"},
    )
    assert wrong_phone.status_code == 404
    cancelled = client.post(
        f"/public/bookings/{business_id}/{booking_id}/cancel",
        json={"phone": "254700000003"},
    )
    assert cancelled.status_code == 200
    assert runtime.platform.get_booking(business_id, booking_id)["status"] == "cancelled"


def test_campaign_attribution_matches_referral_and_records_paid_conversion_once(platform):
    _, business_id = platform.create_business(
        name="Campaign shop",
        email="campaign@example.test",
        password="secure-campaign-password",
    )
    campaign_id = platform.create_campaign(
        business_id,
        name="Meta winter promotion",
        channel="facebook",
        external_source_id="meta-ad-123",
    )
    assert platform.attribute_campaign_conversation(business_id, "conversation-one", "meta-ad-123") is True
    assert platform.attribute_campaign_conversation(business_id, "conversation-one", "another-ad") is False
    assert platform.record_campaign_conversion(
        business_id,
        "conversation-one",
        order_id="order-one",
        revenue=2500,
    ) is True
    assert platform.record_campaign_conversion(
        business_id,
        "conversation-one",
        order_id="order-one",
        revenue=2500,
    ) is False
    campaign = platform.campaigns(business_id)[0]
    assert campaign["id"] == campaign_id
    assert campaign["attributed_conversations"] == 1
    assert campaign["conversions"] == 1
    assert campaign["revenue"] == 2500


def test_whatsapp_template_followup_requires_meta_approval_and_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PUBLIC_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("HERMES_ENCRYPTION_KEY", Fernet.generate_key().decode())
    runtime = Runtime(db_path=str(tmp_path / "followups.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(runtime))
    assert register_verified(
        client,
        {"name": "First shop", "email": "followup@example.test", "password": "first-owner-password"},
    ).status_code == 200
    unapproved = client.post(
        "/api/whatsapp/templates",
        json={
            "name": "order_reminder",
            "language": "en",
            "category": "utility",
            "body_parameter_count": 1,
            "meta_approval_confirmed": False,
        },
    )
    assert unapproved.status_code == 400
    template = client.post(
        "/api/whatsapp/templates",
        json={
            "name": "order_reminder",
            "language": "en",
            "category": "utility",
            "body_parameter_count": 1,
            "meta_approval_confirmed": True,
        },
    )
    assert template.status_code == 201
    send_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    request = {
        "phone": "+254 700 000 001",
        "template_id": template.json()["id"],
        "parameters": ["Customer"],
        "send_at": send_at,
    }
    no_consent = client.post("/api/whatsapp/follow-ups", json=request)
    assert no_consent.status_code == 400
    assert client.post(
        "/api/whatsapp/consent",
        json={"phone": request["phone"], "opted_in": True, "source": "Website signup checkbox"},
    ).status_code == 204
    scheduled = client.post("/api/whatsapp/follow-ups", json=request)
    assert scheduled.status_code == 201
    assert client.get("/api/dashboard").json()["whatsapp_followups"][0]["status"] == "pending"


def test_scheduled_followups_suppress_revoked_consent_and_send_approved_template(platform, monkeypatch):
    _, business_id = platform.create_business(
        name="Shop",
        email="followup-owner@example.test",
        password="a-strong-password",
    )
    platform.save_integrations(
        business_id,
        whatsapp_phone_number_id="phone-id",
        whatsapp_token="provider-token",
        mpesa_credentials={},
        llm_credentials={},
    )
    template_id = platform.save_whatsapp_template(
        business_id,
        name="appointment_reminder",
        language="en",
        category="utility",
        body_parameter_count=1,
    )
    platform.record_whatsapp_consent(
        business_id,
        "254700000001",
        opted_in=True,
        source="Consent form",
    )
    suppressed_id = platform.schedule_followup(
        business_id,
        template_id=template_id,
        phone="254700000001",
        parameters=["Customer"],
        send_at=0,
    )
    platform.record_whatsapp_consent(
        business_id,
        "254700000001",
        opted_in=False,
        source="Customer opt-out",
    )
    assert platform.ready_followups() == []
    assert platform.list_followups(business_id)[0]["status"] == "suppressed"
    assert platform.set_whatsapp_template_active(business_id, template_id, False)
    with pytest.raises(ValueError, match="Approved template"):
        platform.schedule_followup(
            business_id,
            template_id=template_id,
            phone="254700000001",
            parameters=["Customer"],
            send_at=0,
        )
    assert platform.set_whatsapp_template_active(business_id, template_id, True)

    platform.record_whatsapp_consent(
        business_id,
        "254700000001",
        opted_in=True,
        source="New explicit opt-in",
    )
    sent_id = platform.schedule_followup(
        business_id,
        template_id=template_id,
        phone="254700000001",
        parameters=["Customer"],
        send_at=0,
    )
    runtime = Runtime(db_path=platform.db_path, llm=MockLLM(), mpesa=MpesaSimulator())
    sends = []
    monkeypatch.setattr(
        "hermes.worker.whatsapp.send_template",
        lambda *args, **kwargs: sends.append((args, kwargs)) or True,
    )

    assert process_scheduled_followups(runtime) == 1
    assert len(sends) == 1
    assert sends[0][0][1:] == ("appointment_reminder", "en", ["Customer"])
    followups = {item["id"]: item for item in runtime.platform.list_followups(business_id)}
    assert followups[suppressed_id]["status"] == "suppressed"
    assert followups[sent_id]["status"] == "sent"


def test_subscription_checkout_requires_verified_full_amount_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PUBLIC_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("HERMES_ENCRYPTION_KEY", Fernet.generate_key().decode())
    runtime = Runtime(db_path=str(tmp_path / "billing.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(runtime))
    registration = register_verified(
        client,
        {"name": "Billing shop", "email": "billing@example.test", "password": "billing-owner-password"},
    )
    assert registration.status_code == 200

    class BillingProvider:
        def __init__(self):
            self.pushes = []

        def stk_push(self, **kwargs):
            self.pushes.append(kwargs)
            return STKPushResult(success=True, checkout_request_id="premium-checkout", customer_message="Check phone")

        def query_status(self, checkout_request_id):
            assert checkout_request_id == "premium-checkout"
            return PaymentStatus.SUCCESS

    billing_provider = BillingProvider()
    runtime._billing_mpesa_client = billing_provider
    checkout = client.post("/api/billing/checkout", json={"phone": "0712345678"})
    assert checkout.status_code == 200
    assert checkout.json()["amount_kes"] == 2500
    assert billing_provider.pushes[0]["phone"] == "254712345678"
    assert client.post("/api/billing/checkout", json={"phone": "0712345678"}).status_code == 409

    callback = {
        "Body": {
            "stkCallback": {
                "CheckoutRequestID": "premium-checkout",
                "ResultCode": 0,
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": 2500},
                        {"Name": "PhoneNumber", "Value": 254712345678},
                        {"Name": "MpesaReceiptNumber", "Value": "QAB123"},
                    ]
                },
            }
        }
    }
    assert client.post("/mpesa/callback", json=callback).status_code == 200
    subscription = client.get("/api/dashboard").json()["subscription"]
    assert subscription["status"] == "premium"
    expiry = subscription["subscription_expires_at"]
    assert expiry is not None
    client.post("/mpesa/callback", json=callback)
    assert client.get("/api/dashboard").json()["subscription"]["subscription_expires_at"] == expiry


def test_subscription_does_not_activate_when_callback_amount_is_wrong(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PUBLIC_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("HERMES_ENCRYPTION_KEY", Fernet.generate_key().decode())
    runtime = Runtime(db_path=str(tmp_path / "wrong-amount.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(runtime))
    register_verified(
        client,
        {"name": "Billing shop", "email": "wrong-amount@example.test", "password": "billing-owner-password"},
    )

    class BillingProvider:
        def stk_push(self, **kwargs):
            return STKPushResult(success=True, checkout_request_id="wrong-amount-checkout")

        def query_status(self, checkout_request_id):
            return PaymentStatus.SUCCESS

    runtime._billing_mpesa_client = BillingProvider()
    assert client.post("/api/billing/checkout", json={"phone": "254712345678"}).status_code == 200
    callback = {
        "Body": {
            "stkCallback": {
                "CheckoutRequestID": "wrong-amount-checkout",
                "ResultCode": 0,
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": 1},
                        {"Name": "PhoneNumber", "Value": 254712345678},
                    ]
                },
            }
        }
    }
    client.post("/mpesa/callback", json=callback)
    assert client.get("/api/dashboard").json()["subscription"]["status"] == "trialing"


def test_billing_reconciliation_never_activates_without_verified_callback(platform):
    _, business_id = platform.create_business(
        name="Reconciliation shop",
        email="reconcile@example.test",
        password="secure-reconciliation-password",
    )
    intent_id = platform.create_billing_intent(business_id, "254712345678", 2500)
    assert platform.attach_billing_checkout(intent_id, "reconciliation-checkout")
    with platform._connect() as connection:
        connection.execute(
            "UPDATE billing_payments SET created_at = ? WHERE checkout_request_id = ?",
            (time.time() - 3600, "reconciliation-checkout"),
        )

    class Provider:
        def query_status(self, checkout_request_id):
            assert checkout_request_id == "reconciliation-checkout"
            return PaymentStatus.SUCCESS

    runtime = Runtime(db_path=platform.db_path, llm=MockLLM(), mpesa=MpesaSimulator())
    runtime._billing_mpesa_client = Provider()
    for _ in range(5):
        assert process_billing_reconciliation(runtime) == 1
        with platform._connect() as connection:
            connection.execute(
                "UPDATE billing_payments SET last_checked_at = ? WHERE checkout_request_id = ?",
                (time.time() - 301, "reconciliation-checkout"),
            )
    state = platform.billing_status(business_id)
    assert state["status"] == "pending"
    assert state["needs_review"] == 1
    assert platform.subscription(business_id)["status"] == "trialing"


def test_dashboard_chat_simulator_and_conversation_detail_are_tenant_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_INVITE_TOKEN", "invite-secret")
    runtime = Runtime(db_path=str(tmp_path / "simulator.db"), llm=MockLLM(), mpesa=MpesaSimulator())
    app = create_test_app(runtime)
    owner = TestClient(app)
    registered = register_verified(
        owner,
        {
            "invite_code": "invite-secret",
            "name": "First shop",
            "email": "first@example.test",
            "password": "first-owner-password",
        },
    )
    assert registered.status_code == 200
    response = owner.post("/api/simulator/chat", json={"text": "hello", "phone": "254700000001"})
    assert response.status_code == 200
    conversation_id = response.json()["conversation_id"]
    assert response.json()["reply"]
    details = owner.get(f"/api/conversations/{conversation_id}")
    assert details.status_code == 200
    assert details.json()["messages"][-1]["role"] == "agent"
    dashboard = owner.get("/api/dashboard").json()
    assert dashboard["conversations"][0]["last_message"]["role"] == "agent"

    other_owner = TestClient(app)
    other_registration = register_verified(
        other_owner,
        {
            "invite_code": "invite-secret",
            "name": "Second shop",
            "email": "second@example.test",
            "password": "second-owner-password",
        },
    )
    assert other_registration.status_code == 200
    assert other_owner.get(f"/api/conversations/{conversation_id}").status_code == 404


def test_login_is_tenant_scoped_and_password_is_hashed(platform):
    user_id, business_id = platform.create_business(
        name="Shop",
        email="OWNER@EXAMPLE.TEST",
        password="a-strong-password",
    )
    token = platform.create_email_verification_token(user_id)
    assert platform.verify_email_token(token) == (user_id, business_id)
    assert platform.authenticate("owner@example.test", "a-strong-password") == (user_id, business_id)
    assert platform.authenticate("owner@example.test", "incorrect-password") is None
    with platform._connect() as connection:
        password_hash = connection.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()[0]
    assert "a-strong-password" not in password_hash
    assert _verify_password("a-strong-password", password_hash)


def test_auth_rate_limit_uses_hashed_buckets_and_resets_window(platform):
    allowed, retry = platform.consume_auth_attempt("login:email:owner@example.test", limit=2, window_seconds=60, now=100)
    assert allowed and retry == 0
    assert platform.consume_auth_attempt(
        "login:email:owner@example.test", limit=2, window_seconds=60, now=110
    ) == (True, 0)
    assert platform.consume_auth_attempt(
        "login:email:owner@example.test", limit=2, window_seconds=60, now=120
    ) == (False, 40)
    assert platform.consume_auth_attempt(
        "login:email:owner@example.test", limit=2, window_seconds=60, now=160
    ) == (True, 0)
    with platform._connect() as connection:
        stored_bucket = connection.execute("SELECT bucket_hash FROM auth_rate_limits").fetchone()[0]
    assert stored_bucket != "login:email:owner@example.test"


def test_provider_credentials_are_encrypted_and_never_plaintext(platform):
    user_id, business_id = platform.create_business(name="Shop", email="owner@example.test", password="secure-password-1")
    platform.verify_email_token(platform.create_email_verification_token(user_id))
    platform.save_integrations(
        business_id,
        whatsapp_phone_number_id="phone-1",
        whatsapp_token="wa-secret",
        whatsapp_app_secret="wa-app-secret",
        whatsapp_verify_token="wa-verify-token",
        mpesa_credentials={"MPESA_CONSUMER_SECRET": "mpesa-secret"},
        llm_credentials={"embedding_api_key": "embedding-secret"},
        embedding_consent=True,
        llm_data_consent=True,
    )
    with platform._connect() as connection:
        row = connection.execute(
            "SELECT whatsapp_token_enc, whatsapp_app_secret_enc, whatsapp_verify_token_enc, "
            "mpesa_credentials_enc, llm_credentials_enc FROM businesses WHERE id=?",
            (business_id,),
        ).fetchone()
    assert b"wa-secret" not in row["whatsapp_token_enc"].encode()
    assert b"wa-app-secret" not in row["whatsapp_app_secret_enc"].encode()
    assert b"wa-verify-token" not in row["whatsapp_verify_token_enc"].encode()
    assert b"mpesa-secret" not in row["mpesa_credentials_enc"].encode()
    assert platform.integrations(business_id)["mpesa"]["MPESA_CONSUMER_SECRET"] == "mpesa-secret"
    assert platform.integrations(business_id)["embedding_consent"] is True
    assert platform.integrations(business_id)["llm_data_consent"] is True
    assert platform.verify_whatsapp_token("wa-verify-token")
    assert not platform.verify_whatsapp_token("wrong-token")
    platform.save_catalog(
        business_id,
        products=[{"name": "Flour", "price": 100, "aliases": [], "in_stock": True, "unit": "kg"}],
        info={"delivery": "Within one day"},
    )
    platform.add_document(business_id, "Private policy", [("Only this merchant sees this", [0.1, 0.2])])
    with platform._connect() as connection:
        catalog = connection.execute(
            "SELECT config_json FROM businesses WHERE id=?",
            (business_id,),
        ).fetchone()[0]
        document = connection.execute(
            """
            SELECT knowledge_documents.title, knowledge_chunks.text, knowledge_chunks.embedding_json
            FROM knowledge_documents JOIN knowledge_chunks
              ON knowledge_documents.id = knowledge_chunks.document_id
            WHERE knowledge_documents.business_id=?
            """,
            (business_id,),
        ).fetchone()
    assert "Within one day" not in catalog
    assert "Private policy" not in document["title"]
    assert "Only this merchant sees this" not in document["text"]
    assert "[0.1, 0.2]" not in document["embedding_json"]
    assert platform.business(business_id).info["delivery"] == "Within one day"
    assert platform.list_chunks(business_id)[0]["text"] == "Only this merchant sees this"
    outbound_id = platform.enqueue_outbound(
        business_id,
        "254712345678",
        "Your order is ready",
        "phone-1",
    )
    handoff_id, _ = platform.ensure_handoff(business_id, "conversation-id", "Customer requested help")
    with platform._connect() as connection:
        outbound = connection.execute(
            "SELECT to_phone, text FROM outbound_messages WHERE id=?",
            (outbound_id,),
        ).fetchone()
        handoff = connection.execute(
            "SELECT reason FROM handoffs WHERE id=?",
            (handoff_id,),
        ).fetchone()
    assert "254712345678" not in outbound["to_phone"]
    assert "Your order is ready" not in outbound["text"]
    assert "Customer requested help" not in handoff["reason"]
    assert platform.ready_outbound()[0]["text"] == "Your order is ready"
    assert platform.list_handoffs(business_id)[0]["reason"] == "Customer requested help"


def test_embedding_provider_requires_tenant_consent(platform, monkeypatch):
    from hermes.rag import EmbeddingClient, build_retriever

    _, business_id = platform.create_business(name="Shop", email="owner@example.test", password="secure-password-1")
    platform.save_integrations(
        business_id,
        whatsapp_phone_number_id="",
        whatsapp_token="",
        mpesa_credentials={},
        llm_credentials={"embedding_api_key": "embedding-secret"},
        embedding_consent=False,
    )

    def unexpected_embedding(*_args, **_kwargs):
        raise AssertionError("Embedding provider was called without consent")

    monkeypatch.setattr(EmbeddingClient, "embed", unexpected_embedding)
    retriever = build_retriever(platform, business_id)
    assert retriever.embedding_client is None
    retriever.index_document(business_id, "Policy", "Returns accepted within three days.")
    assert retriever.search(business_id, "returns")


def test_withdrawing_embedding_consent_deletes_stored_vectors(platform):
    _, business_id = platform.create_business(
        name="Shop",
        email="owner@example.test",
        password="a-strong-password",
    )
    platform.save_integrations(
        business_id,
        whatsapp_phone_number_id="",
        whatsapp_token="",
        mpesa_credentials={},
        llm_credentials={"embedding_api_key": "embedding-secret"},
        embedding_consent=True,
    )
    platform.add_document(business_id, "Policy", [("Return terms", [0.2, 0.8])])
    assert platform.list_chunks(business_id)[0]["embedding"] == [0.2, 0.8]

    platform.save_integrations(
        business_id,
        whatsapp_phone_number_id="",
        whatsapp_token="",
        mpesa_credentials={},
        llm_credentials={"embedding_api_key": "embedding-secret"},
        embedding_consent=False,
    )

    assert platform.list_chunks(business_id)[0]["embedding"] is None


def test_password_policy_and_duplicate_email(platform):
    with pytest.raises(ValueError, match="12 characters"):
        platform.create_business(name="Shop", email="a@example.test", password="short")
    platform.create_business(name="One", email="a@example.test", password="long-password-1")
    with pytest.raises(ValueError, match="already registered"):
        platform.create_business(name="Two", email="A@example.test", password="long-password-2")


def test_inbound_idempotency_claim_and_expired_lease(platform):
    assert platform.claim_inbound("tenant", "message-1")
    assert not platform.claim_inbound("tenant", "message-1")
    platform.finish_inbound("tenant", "message-1", "done")
    assert not platform.claim_inbound("tenant", "message-1")
    with platform._connect() as connection:
        connection.execute(
            "UPDATE inbound_events SET status='processing', received_at=0 WHERE message_id='message-1'"
        )
    assert platform.claim_inbound("tenant", "message-1")


def test_handoff_queue_is_tenant_scoped_and_resolvable(platform):
    handoff_id, created = platform.ensure_handoff("tenant-a", "conversation-1", "Needs a person")
    assert created is True
    same_id, created_again = platform.ensure_handoff("tenant-a", "conversation-1", "Second notice")
    assert (same_id, created_again) == (handoff_id, False)
    assert platform.list_handoffs("tenant-b") == []
    assert platform.update_handoff("tenant-a", handoff_id, "assigned", "owner@example.test")
    assert platform.list_handoffs("tenant-a")[0]["status"] == "assigned"


def test_outbound_queue_retries_then_sends(platform):
    message_id = platform.enqueue_outbound("tenant", "254712345678", "Hello", "phone-id")
    first = platform.ready_outbound()[0]
    assert first["id"] == message_id
    assert platform.ready_outbound() == []
    platform.finish_outbound(
        message_id,
        claim_token=first["claim_token"],
        success=False,
        error="provider unavailable",
    )
    assert platform.ready_outbound() == []
    with platform._connect() as connection:
        connection.execute(
            "UPDATE outbound_messages SET next_attempt_at=0 WHERE id=?",
            (message_id,),
        )
    retry = platform.ready_outbound()[0]
    platform.finish_outbound(message_id, claim_token=retry["claim_token"], success=True)
    with platform._connect() as connection:
        status = connection.execute("SELECT status FROM outbound_messages WHERE id=?", (message_id,)).fetchone()[0]
    assert status == "sent"


def test_outbound_worker_sends_claimed_message_once(platform, monkeypatch):
    _, business_id = platform.create_business(
        name="Shop",
        email="owner@example.test",
        password="a-strong-password",
    )
    platform.save_integrations(
        business_id,
        whatsapp_phone_number_id="phone-id",
        whatsapp_token="provider-token",
        mpesa_credentials={},
        llm_credentials={},
    )
    runtime = Runtime(db_path=platform.db_path, llm=MockLLM(), mpesa=MpesaSimulator())
    message_id = runtime.platform.enqueue_outbound(
        business_id,
        "254712345678",
        "Order received",
        "phone-id",
    )
    sends = []
    monkeypatch.setattr("hermes.worker.whatsapp.send_text", lambda *args, **kwargs: sends.append(args) or True)

    assert process_outbound_queue(runtime) == 1
    assert process_outbound_queue(runtime) == 0
    assert len(sends) == 1
    with runtime.platform._connect() as connection:
        message_status = connection.execute(
            "SELECT status, attempts FROM outbound_messages WHERE id=?",
            (message_id,),
        ).fetchone()
    assert message_status["status"] == "sent"
    assert message_status["attempts"] == 1


def test_document_chunks_and_tenant_scoped_retrieval(platform):
    from hermes.rag import HybridRetriever

    retriever = HybridRetriever(platform)
    retriever.index_document(
        "tenant-a",
        "Returns policy",
        "Unopened products can be returned within three days with a receipt.",
    )
    assert retriever.search("tenant-a", "return unopened products")
    assert retriever.search("tenant-b", "return unopened products") == []


def test_retention_and_account_deletion_remove_tenant_data(platform):
    from hermes.models import Conversation
    from hermes.store import HermesStore

    db_path = platform.db_path
    store = HermesStore(db_path)
    conversation = Conversation(business_id="tenant", customer_phone="254700000000")
    store.save(conversation)
    with store._connect() as connection:
        connection.execute("UPDATE conversations SET updated_at=0 WHERE id=?", (conversation.id,))
    platform.claim_inbound("tenant", "old-event")
    old_outbound_id = platform.enqueue_outbound("tenant", "254700000000", "Old reply", "phone-id")
    with platform._connect() as connection:
        connection.execute("UPDATE inbound_events SET received_at=0 WHERE message_id='old-event'")
        connection.execute(
            "UPDATE outbound_messages SET created_at=0, status='sent' WHERE id=?",
            (old_outbound_id,),
        )
    assert platform.purge_expired_conversations(1) == 1
    assert store.get(conversation.id, business_id="tenant") is None
    with platform._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM inbound_events WHERE message_id='old-event'").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM outbound_messages WHERE id=?",
                (old_outbound_id,),
            ).fetchone()[0]
            == 0
        )

    platform.create_business(name="Shop", email="owner@example.test", password="secure-password-1")
    _, business_id = platform.create_business(
        name="Delete me", email="delete@example.test", password="secure-password-2"
    )
    platform.ensure_handoff(business_id, "c1", "reason")
    platform.delete_business_data(business_id)
    assert platform.business(business_id) is None
    assert platform.list_handoffs(business_id) == []


def test_backup_restore_requires_confirmation_and_preserves_rollback(tmp_path):
    database = tmp_path / "live.db"
    runtime = Runtime(db_path=str(database), llm=MockLLM(), mpesa=MpesaSimulator())
    _, merchant_id = runtime.platform.create_business(
        name="Before backup",
        email="before@example.test",
        password="a-strong-password",
    )
    backup = backup_database(database, tmp_path / "backups" / "snapshot.sqlite3")

    _, later_merchant_id = runtime.platform.create_business(
        name="After backup",
        email="after@example.test",
        password="a-strong-password",
    )
    with pytest.raises(ValueError, match="confirm_replace"):
        restore_database(backup, database)

    rollback = restore_database(backup, database, confirm_replace=True)
    assert rollback is not None and rollback.exists()
    restored = Runtime(db_path=str(database), llm=MockLLM(), mpesa=MpesaSimulator())
    assert restored.platform.business(merchant_id) is not None
    assert restored.platform.business(later_merchant_id) is None
    assert backup.exists()


def test_integration_settings_require_encryption_key_and_https_callback(platform):
    _, business_id = platform.create_business(name="Shop", email="owner@example.test", password="secure-password-1")
    with platform._connect() as connection:
        user_id = connection.execute(
            "SELECT id FROM users WHERE email = ?",
            ("owner@example.test",),
        ).fetchone()["id"]
    platform.verify_email_token(platform.create_email_verification_token(user_id))
    app_runtime = Runtime(db_path=platform.db_path, llm=MockLLM(), mpesa=MpesaSimulator())
    client = TestClient(create_test_app(app_runtime))
    platform.create_session(platform.authenticate("owner@example.test", "secure-password-1")[0])
    token = platform.create_session(platform.authenticate("owner@example.test", "secure-password-1")[0])
    client.cookies.set("hermes_session", token)
    response = client.put(
        "/api/integrations",
        json={
            "whatsapp_phone_number_id": "phone-id",
            "mpesa_credentials": {"MPESA_CALLBACK_URL": "http://insecure.test/callback"},
        },
    )
    assert response.status_code == 400
    assert platform.business(business_id) is not None
