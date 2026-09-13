"""Minimal SMTP email delivery (issue #27).

Configured via env vars (see ``.env.example`` / ``Settings``). When email is
disabled or SMTP is incomplete, delivery is skipped with an explicit status
so onboarding can still complete in local/dev.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from throughline.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailDeliveryResult:
    """Outcome of an outbound email attempt."""

    status: str  # sent | skipped | failed
    detail: str | None = None


def email_configured() -> bool:
    """Return True when SMTP settings are present and email is enabled."""
    if not settings.email_enabled:
        return False
    return bool(
        settings.smtp_host.strip()
        and settings.smtp_from.strip()
        and settings.smtp_port > 0
    )


def send_email(*, to: str, subject: str, body_text: str) -> EmailDeliveryResult:
    """Send a plain-text email via SMTP, or skip when not configured."""
    if not settings.email_enabled:
        return EmailDeliveryResult(
            status="skipped",
            detail="EMAIL_ENABLED is false; outbound email disabled",
        )
    if not email_configured():
        return EmailDeliveryResult(
            status="skipped",
            detail="SMTP not fully configured (SMTP_HOST / SMTP_FROM / SMTP_PORT)",
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = to
    message.set_content(body_text)

    try:
        if settings.smtp_use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                if settings.smtp_user.strip():
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
                if settings.smtp_user.strip():
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        detail = str(exc)[:500] if str(exc) else "SMTP delivery failed"
        logger.warning("email send failed to=%s detail=%s", to, detail)
        return EmailDeliveryResult(status="failed", detail=detail)

    logger.info("email sent to=%s subject=%s", to, subject)
    return EmailDeliveryResult(status="sent")
