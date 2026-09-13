"""Outbound email helpers (issue #27 diagnostic onboarding)."""

from throughline.email.smtp import EmailDeliveryResult, send_email

__all__ = ["EmailDeliveryResult", "send_email"]
