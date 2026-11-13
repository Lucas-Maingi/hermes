"""SMTP delivery for account verification messages."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit


def smtp_is_configured() -> bool:
    return all(
        os.getenv(name)
        for name in ("HERMES_SMTP_HOST", "HERMES_EMAIL_FROM")
    )


def send_verification_email(recipient: str, verification_url: str) -> None:
    host = os.getenv("HERMES_SMTP_HOST", "")
    sender = os.getenv("HERMES_EMAIL_FROM", "")
    if not host or not sender:
        raise RuntimeError("SMTP host and sender address must be configured")
    parsed_url = urlsplit(verification_url)
    if parsed_url.scheme != "https" and parsed_url.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Verification links must use HTTPS outside localhost")

    message = EmailMessage()
    message["Subject"] = "Verify your Hermes workspace email"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        "Verify your email address to activate your Hermes workspace:\n\n"
        f"{verification_url}\n\n"
        "This link expires in 24 hours. If you did not request this, you can ignore this message."
    )

    port = int(os.getenv("HERMES_SMTP_PORT", "587"))
    username = os.getenv("HERMES_SMTP_USERNAME", "")
    password = os.getenv("HERMES_SMTP_PASSWORD", "")
    use_ssl = os.getenv("HERMES_SMTP_USE_SSL", "").lower() == "true"
    use_starttls = os.getenv("HERMES_SMTP_STARTTLS", "true").lower() == "true"
    if not use_ssl and not use_starttls:
        raise ValueError("SMTP must use SSL or STARTTLS")
    context = ssl.create_default_context()
    client = (
        smtplib.SMTP_SSL(host, port, timeout=15, context=context)
        if use_ssl
        else smtplib.SMTP(host, port, timeout=15)
    )
    with client:
        if not use_ssl and use_starttls:
            client.starttls(context=context)
        if username:
            client.login(username, password)
        client.send_message(message)
