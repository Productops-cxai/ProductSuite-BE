"""Collection email provider abstraction — SendGrid preferred, SMTP fallback.

Credentials stay in env/settings (never frontend). Empty SendGrid + empty SMTP → log-only.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Optional
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import EmailLogModel

logger = logging.getLogger("app.email.collection")


def sendgrid_configured() -> bool:
    return bool(
        (settings.SENDGRID_API_KEY or "").strip() and (settings.SENDGRID_FROM_EMAIL or "").strip()
    )


def smtp_configured() -> bool:
    return bool((settings.SMTP_HOST or "").strip())


def active_collection_provider() -> str:
    """Which provider will be used for the next collection send."""
    if sendgrid_configured():
        return "sendgrid"
    if smtp_configured():
        return "smtp"
    return "log"


def _resolve_from(
    *,
    from_email: str | None,
    from_name: str | None,
) -> tuple[str, str]:
    email = (from_email or "").strip() or (settings.SENDGRID_FROM_EMAIL or "").strip() or (
        settings.SMTP_FROM or settings.SMTP_USER or "noreply@payflow.local"
    ).strip()
    name = (from_name or "").strip() or (settings.SENDGRID_FROM_NAME or "PayFlow").strip()
    return email, name


def _send_via_sendgrid(
    *,
    to_email: str,
    subject: str,
    body: str,
    from_email: str,
    from_name: str,
    custom_args: dict[str, str] | None,
) -> tuple[bool, str | None, str | None]:
    personalization: dict[str, Any] = {"to": [{"email": to_email}]}
    if custom_args:
        personalization["custom_args"] = {str(k): str(v) for k, v in custom_args.items()}
    payload = {
        "personalizations": [personalization],
        "from": {"email": from_email, "name": from_name},
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
        logger.exception("SendGrid collection send failed to=%s", to_email)
        return False, str(exc)[:500], None


def _send_via_smtp(
    *,
    to_email: str,
    subject: str,
    body: str,
    from_email: str,
    from_name: str,
) -> tuple[bool, str | None, str | None]:
    host = settings.SMTP_HOST.strip()
    port = int(settings.SMTP_PORT or 587)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = to_email
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            if settings.SMTP_USE_TLS:
                smtp.starttls()
            user = (settings.SMTP_USER or "").strip()
            password = settings.SMTP_PASSWORD or ""
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        # SMTP has no stable message id — synthesize a log-scoped id later.
        return True, None, None
    except Exception as exc:  # noqa: BLE001
        logger.exception("SMTP collection send failed to=%s", to_email)
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
    from_email: str | None = None,
    from_name: str | None = None,
    commit: bool = True,
) -> EmailLogModel:
    """Send collection email via SendGrid (if configured) else SMTP else log-only.

    Returns EmailLogModel with dynamic attrs:
    - provider_message_id
    - provider_name (sendgrid | smtp | log)
    - error (optional)
    """
    resolved_from, resolved_name = _resolve_from(from_email=from_email, from_name=from_name)
    provider = active_collection_provider()
    status = "logged"
    err: str | None = None
    provider_id: str | None = None
    # SendGrid rejects content.value of length 0.
    if not (body or "").strip():
        body = "Payment reminder"

    if provider == "sendgrid":
        ok, err, provider_id = _send_via_sendgrid(
            to_email=to_email,
            subject=subject,
            body=body,
            from_email=resolved_from,
            from_name=resolved_name,
            custom_args=custom_args,
        )
        status = "sent" if ok else "failed"
    elif provider == "smtp":
        ok, err, provider_id = _send_via_smtp(
            to_email=to_email,
            subject=subject,
            body=body,
            from_email=resolved_from,
            from_name=resolved_name,
        )
        status = "sent" if ok else "failed"
        if ok and not provider_id:
            provider_id = f"smtp-{to_email.lower()}-{subject[:24]}"

    stored_body = body
    if err:
        stored_body = f"{body}\n\n[{provider}: {err}]"
    if provider_id:
        stored_body = f"{stored_body}\n\n[provider_id={provider_id}]"
    stored_body = f"{stored_body}\n\n[from={resolved_name} <{resolved_from}>]"

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
    setattr(row, "provider_name", provider)
    setattr(row, "error", err)
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
        setattr(row, "provider_message_id", provider_id)
        setattr(row, "provider_name", provider)
        setattr(row, "error", err)
    else:
        db.flush()
    logger.info(
        "COLLECTION_EMAIL %s provider=%s id=%s to=%s type=%s from=%s",
        status,
        provider,
        row.id,
        to_email,
        email_type,
        resolved_from,
    )
    return row
