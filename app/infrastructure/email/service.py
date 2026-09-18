import logging
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.infrastructure.database.models import EmailLogModel

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
    """
    Persist outbound email to email_logs and print to console.
    SMTP is not used in this phase — copy action_link from the table / API.
    """
    row = EmailLogModel(
        to_email=to_email.lower(),
        subject=subject,
        body=body,
        email_type=email_type,
        action_link=action_link,
        related_user_id=related_user_id,
        status="logged",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    logger.info(
        "EMAIL logged id=%s to=%s type=%s subject=%s link=%s",
        row.id,
        to_email,
        email_type,
        subject,
        action_link,
    )
    print(
        f"\n=== EMAIL (saved to email_logs id={row.id}) ===\n"
        f"To: {to_email}\n"
        f"Type: {email_type}\n"
        f"Subject: {subject}\n"
        f"Link: {action_link or '(none)'}\n"
        f"{body}\n"
        f"==============================================\n"
    )
    return row
