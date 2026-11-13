import pytest

from hermes.mailer import send_verification_email, smtp_is_configured


def test_smtp_verification_mail_uses_starttls_and_authentication(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def starttls(self, *, context):
            calls.append(("starttls", context))

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            calls.append(("send", message))

    monkeypatch.setattr("hermes.mailer.smtplib.SMTP", FakeSMTP)
    monkeypatch.setenv("HERMES_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("HERMES_SMTP_PORT", "587")
    monkeypatch.setenv("HERMES_SMTP_USERNAME", "hermes")
    monkeypatch.setenv("HERMES_SMTP_PASSWORD", "smtp-secret")
    monkeypatch.setenv("HERMES_EMAIL_FROM", "Hermes <no-reply@example.test>")
    monkeypatch.setenv("HERMES_SMTP_STARTTLS", "true")
    monkeypatch.delenv("HERMES_SMTP_USE_SSL", raising=False)

    assert smtp_is_configured()
    send_verification_email("owner@example.test", "https://hermes.example.test/?verify=opaque-token")

    assert calls[0] == ("connect", "smtp.example.test", 587, 15)
    assert calls[1][0] == "starttls"
    assert calls[2] == ("login", "hermes", "smtp-secret")
    message = calls[3][1]
    assert message["To"] == "owner@example.test"
    assert "opaque-token" in message.get_content()


def test_smtp_rejects_non_https_public_verification_link(monkeypatch):
    monkeypatch.setenv("HERMES_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("HERMES_EMAIL_FROM", "no-reply@example.test")
    with pytest.raises(ValueError, match="must use HTTPS"):
        send_verification_email("owner@example.test", "http://hermes.example.test/?verify=token")


def test_smtp_requires_encryption(monkeypatch):
    monkeypatch.setenv("HERMES_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("HERMES_EMAIL_FROM", "no-reply@example.test")
    monkeypatch.setenv("HERMES_SMTP_STARTTLS", "false")
    monkeypatch.delenv("HERMES_SMTP_USE_SSL", raising=False)
    with pytest.raises(ValueError, match="SSL or STARTTLS"):
        send_verification_email("owner@example.test", "https://hermes.example.test/?verify=token")
