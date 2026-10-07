"""Admin delete audit + identity cascade helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationAppError
from app.infrastructure.database.models import (
    AuthTokenModel,
    DeletionLogModel,
    EmailLogModel,
    PayflowNotificationModel,
    PayflowUserMembershipModel,
    RefreshTokenModel,
    UserModel,
    UserProductAssignmentModel,
)
from app.shared.enums import PayflowRoleCode, PlatformRole

_SECRET_KEYS = {"password_hash", "token_hash"}


def json_safe(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items() if str(k) not in _SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def snapshot_model(obj: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for column in obj.__table__.columns:
        if column.name in _SECRET_KEYS:
            continue
        data[column.name] = json_safe(getattr(obj, column.name))
    return data


def actor_role_label(actor: UserModel) -> str:
    membership = getattr(actor, "payflow_membership", None)
    role = getattr(membership, "role", None) if membership else None
    if role is not None:
        return f"{actor.role_code} / {role.code}"
    return actor.role_code or "unknown"


def record_deletion(
    db: Session,
    *,
    actor: UserModel,
    module: str,
    entity_type: str,
    entity_id: str | int | UUID,
    entity_label: str,
    source: str | None,
    record_snapshot: dict | list | None,
    related_deleted: list[dict[str, Any]] | None = None,
    activity: str = "delete",
) -> DeletionLogModel:
    row = DeletionLogModel(
        module=module,
        entity_type=entity_type,
        entity_id=str(entity_id),
        entity_label=(entity_label or "")[:512],
        activity=activity,
        source=(source or "")[:512] or None,
        actor_user_id=actor.id,
        actor_name=actor.full_name,
        actor_email=actor.email,
        actor_role=actor_role_label(actor),
        record_snapshot=json_safe(record_snapshot),
        related_deleted=json_safe(related_deleted or []),
    )
    db.add(row)
    return row


def serialize_log(row: DeletionLogModel) -> dict[str, Any]:
    return {
        "id": row.id,
        "module": row.module,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "entity_label": row.entity_label,
        "activity": row.activity,
        "source": row.source,
        "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
        "actor_name": row.actor_name,
        "actor_email": row.actor_email,
        "actor_role": row.actor_role,
        "record_snapshot": row.record_snapshot,
        "related_deleted": row.related_deleted or [],
        "created_at": row.created_at,
    }


def list_deletion_logs(
    db: Session,
    *,
    module: str | None = None,
    search: str | None = None,
    entity_type: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    query = db.query(DeletionLogModel)
    if module:
        query = query.filter(DeletionLogModel.module == module)
    if entity_type:
        query = query.filter(DeletionLogModel.entity_type == entity_type)
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(
            or_(
                DeletionLogModel.entity_label.ilike(like),
                DeletionLogModel.entity_id.ilike(like),
                DeletionLogModel.actor_name.ilike(like),
                DeletionLogModel.actor_email.ilike(like),
                DeletionLogModel.source.ilike(like),
            )
        )
    rows = query.order_by(DeletionLogModel.id.desc()).limit(min(limit, 500)).all()
    return [serialize_log(r) for r in rows]


def related_item(entity_type: str, item_id: Any, label: str) -> dict[str, str]:
    return {"entity_type": entity_type, "id": str(item_id), "label": label}


def purge_user_identity(
    db: Session,
    user: UserModel,
    *,
    actor: UserModel,
) -> list[dict[str, str]]:
    """Hard-delete a person and PayFlow/auth children. Caller commits + logs."""
    if user.role_code == PlatformRole.PLATFORM_SUPER_ADMIN.value:
        raise ValidationAppError("The Platform Super Admin cannot be deleted")
    if user.id == actor.id:
        raise ValidationAppError("You cannot delete your own account")

    from sqlalchemy.orm import joinedload

    membership = (
        db.query(PayflowUserMembershipModel)
        .options(joinedload(PayflowUserMembershipModel.role))
        .filter(PayflowUserMembershipModel.user_id == user.id)
        .first()
    )
    if membership and membership.role and membership.role.code == PayflowRoleCode.OPERATIONS_ADMIN.value:
        remaining = (
            db.query(PayflowUserMembershipModel)
            .filter(
                PayflowUserMembershipModel.payflow_role_id == membership.payflow_role_id,
                PayflowUserMembershipModel.user_id != user.id,
            )
            .count()
        )
        if remaining == 0:
            raise ValidationAppError("Cannot delete the last Operations Admin")

    related: list[dict[str, str]] = []

    for token in db.query(AuthTokenModel).filter(AuthTokenModel.user_id == user.id).all():
        related.append(related_item("auth_token", token.id, token.token_type))
        db.delete(token)

    for token in db.query(RefreshTokenModel).filter(RefreshTokenModel.user_id == user.id).all():
        related.append(related_item("refresh_token", token.id, "refresh"))
        db.delete(token)

    for note in db.query(PayflowNotificationModel).filter(PayflowNotificationModel.user_id == user.id).all():
        related.append(related_item("notification", note.id, note.title))
        db.delete(note)

    logs = db.query(EmailLogModel).filter(EmailLogModel.related_user_id == user.id).all()
    for log in logs:
        log.related_user_id = None

    for link in db.query(UserProductAssignmentModel).filter(UserProductAssignmentModel.user_id == user.id).all():
        related.append(related_item("product_assignment", link.id, str(link.product_id)))
        db.delete(link)

    if membership:
        for assignment in list(membership.client_assignments or []):
            related.append(
                related_item(
                    "client_assignment",
                    assignment.id,
                    str(assignment.client_id),
                )
            )
            db.delete(assignment)
        related.append(
            related_item("payflow_membership", membership.id, str(membership.payflow_role_id))
        )
        db.delete(membership)

    db.delete(user)
    return related
