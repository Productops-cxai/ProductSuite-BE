"""Suite / identity outbound email (activation, password reset, notifications).

Delivery order matches collection mail: SendGrid → SMTP → log-only.
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import EmailLogModel
from app.infrastructure.email.collection_provider import send_collection_email

logger = logging.getLogger("app.email")


def send_email(
    db: Session,
    *,
    to_email: str,
    subject: str,
    body: str,
    email_type: str,
    action_link: Optional[str] = None,
    related_user_id: Optional[UUID] = None,
) -> EmailLogModel:
    """Persist to email_logs and deliver via SendGrid (preferred) or SMTP.

    When neither provider is configured, status stays ``logged`` (dev / email-less).
    Uses the same branded HTML + CTA button template as collection mail
    (Activate Account / Reset Password). No unsubscribe footer on identity mail.
    """
    # Branded sender only for identity CTAs — leave notifications on default From.
    suite_from = None
    if (email_type or "").strip().lower() in {"activation", "password_reset"}:
        suite_from = (settings.SENDGRID_FROM_NAME or "").strip() or "Platform Suite"
    row = send_collection_email(
        db,
        to_email=to_email,
        subject=subject,
        body=body,
        email_type=email_type,
        action_link=action_link,
        related_user_id=related_user_id,
        from_name=suite_from,
        commit=True,
    )

    is_prod = (settings.APP_ENV or "").strip().lower() == "production"
    provider = getattr(row, "provider_name", "?")
    # Logger only — never print() here. Windows cp1252 consoles raise
    # UnicodeEncodeError on arrows/smart quotes and that 500'd person invites.
    logger.info(
        "EMAIL %s provider=%s id=%s to=%s type=%s subject=%s link=%s",
        row.status,
        provider,
        row.id,
        to_email,
        email_type,
        subject,
        "(redacted)" if is_prod else action_link,
    )
    if not is_prod:
        logger.info(
            "EMAIL body id=%s type=%s\n%s",
            row.id,
            email_type,
            body,
        )
    return row
