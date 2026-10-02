import logging
import smtplib
from email.message import EmailMessage
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import EmailLogModel

logger = logging.getLogger("app.email")


def _smtp_configured() -> bool:
    return bool((settings.SMTP_HOST or "").strip())


def _try_smtp_send(*, to_email: str, subject: str, body: str) -> tuple[bool, str | None]:
    """Attempt SMTP delivery. Returns (ok, error_message)."""
    if not _smtp_configured():
        return False, "smtp_not_configured"
    host = settings.SMTP_HOST.strip()
    port = int(settings.SMTP_PORT or 587)
    from_addr = (settings.SMTP_FROM or settings.SMTP_USER or "noreply@payflow.local").strip()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
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
        return True, None
    except Exception as exc:  # noqa: BLE001 — surface any SMTP failure into email_logs
        logger.exception("SMTP send failed to=%s", to_email)
        return False, str(exc)[:500]


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
    """
    Persist outbound email to email_logs and print to console.
    When SMTP_* env is set, also attempt real delivery and update status.
    """
    status = "logged"
    smtp_error: str | None = None
    if _smtp_configured():
        ok, smtp_error = _try_smtp_send(to_email=to_email, subject=subject, body=body)
        status = "sent" if ok else "failed"

    row = EmailLogModel(
        to_email=to_email.lower(),
        subject=subject,
        body=body if not smtp_error else f"{body}\n\n[SMTP error: {smtp_error}]",
        email_type=email_type,
        action_link=action_link,
        related_user_id=related_user_id,
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    logger.info(
        "EMAIL %s id=%s to=%s type=%s subject=%s link=%s",
        status,
        row.id,
        to_email,
        email_type,
        subject,
        action_link,
    )
    print(
        f"\n=== EMAIL ({status} → email_logs id={row.id}) ===\n"
        f"To: {to_email}\n"
        f"Type: {email_type}\n"
        f"Subject: {subject}\n"
        f"Link: {action_link or '(none)'}\n"
        f"{body}\n"
        f"==============================================\n"
    )
    return row
