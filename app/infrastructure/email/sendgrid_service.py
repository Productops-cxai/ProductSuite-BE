"""Collection email via SendGrid Web API. Always writes email_logs; log-only when key empty."""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import EmailLogModel

logger = logging.getLogger("app.email.sendgrid")


def _configured() -> bool:
    return bool((settings.SENDGRID_API_KEY or "").strip() and (settings.SENDGRID_FROM_EMAIL or "").strip())


def _try_send(
    *,
    to_email: str,
    subject: str,
    body: str,
    custom_args: dict[str, str] | None = None,
) -> tuple[bool, str | None, str | None]:
    """Returns (ok, error_message, provider_message_id)."""
    if not _configured():
        return False, "sendgrid_not_configured", None
    personalization: dict[str, Any] = {"to": [{"email": to_email}]}
    if custom_args:
        personalization["custom_args"] = {str(k): str(v) for k, v in custom_args.items()}
    payload = {
        "personalizations": [personalization],
        "from": {
            "email": settings.SENDGRID_FROM_EMAIL.strip(),
            "name": (settings.SENDGRID_FROM_NAME or "PayFlow").strip(),
        },
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
        "tracking_settings": {
            "click_tracking": {"enable": True, "enable_text": True},
            "open_tracking": {"enable": True},
        },
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={
                    "Authorization": f"Bearer {settings.SENDGRID_API_KEY.strip()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if resp.status_code in (200, 202):
            return True, None, resp.headers.get("X-Message-Id")
        return False, f"sendgrid_http_{resp.status_code}: {resp.text[:400]}", None
    except Exception as exc:  # noqa: BLE001
        logger.exception("SendGrid send failed to=%s", to_email)
        return False, str(exc)[:500], None


def send_collection_email(
    db: Session,
    *,
    to_email: str,
    subject: str,
    body: str,
    email_type: str = "collection_workflow",
    action_link: Optional[str] = None,
    related_user_id: Optional[UUID] = None,
    custom_args: dict[str, str] | None = None,
    commit: bool = True,
) -> EmailLogModel:
    status = "logged"
    err: str | None = None
    provider_id: str | None = None
    if _configured():
        ok, err, provider_id = _try_send(
            to_email=to_email,
            subject=subject,
            body=body,
            custom_args=custom_args,
        )
        status = "sent" if ok else "failed"

    stored_body = body
    if err:
        stored_body = f"{body}\n\n[SendGrid: {err}]"
    if provider_id:
        stored_body = f"{stored_body}\n\n[provider_id={provider_id}]"

    row = EmailLogModel(
        to_email=to_email.lower(),
        subject=subject,
        body=stored_body,
        email_type=email_type,
        action_link=action_link,
        related_user_id=related_user_id,
        status=status,
    )
    setattr(row, "provider_message_id", provider_id)
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
        setattr(row, "provider_message_id", provider_id)
    else:
        db.flush()
    logger.info(
        "COLLECTION_EMAIL %s id=%s to=%s type=%s",
        status,
        row.id,
        to_email,
        email_type,
    )
    return row
