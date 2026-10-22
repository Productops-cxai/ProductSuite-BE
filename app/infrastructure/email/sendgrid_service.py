"""Backward-compatible collection email entry — delegates to collection_provider."""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.infrastructure.database.models import EmailLogModel
from app.infrastructure.email.collection_provider import (
    active_collection_provider,
    send_collection_email as _send,
    sendgrid_configured,
)

__all__ = [
    "send_collection_email",
    "sendgrid_configured",
    "active_collection_provider",
]


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
    return _send(
        db,
        to_email=to_email,
        subject=subject,
        body=body,
        email_type=email_type,
        action_link=action_link,
        related_user_id=related_user_id,
        custom_args=custom_args,
        from_email=from_email,
        from_name=from_name,
        commit=commit,
    )
