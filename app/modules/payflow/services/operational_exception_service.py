"""Operational exceptions — failures requiring investigation (not Human Review)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowOperationalExceptionModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.modules.payflow.services.notification_service import PayflowNotificationService
from app.shared.enums import (
    PayflowNotificationType,
    PayflowOperationalExceptionSeverity,
    PayflowOperationalExceptionStatus,
    PayflowOperationalExceptionType,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PayflowOperationalExceptionService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _assert_view(self, user: UserModel, client_id: int | None) -> None:
        if self.access.is_operations_admin(user):
            return
        if client_id is None:
            raise ForbiddenError("Operational exception access denied")
        if not self.access.can_see_client(user, client_id):
            raise ForbiddenError("Operational exception access denied")
        if not self.access.can(user, "view_operational_exceptions", client_id):
            raise ForbiddenError("View Operational Exceptions permission required")

    def _assert_manage(self, user: UserModel, client_id: int | None) -> None:
        if self.access.is_operations_admin(user):
            return
        self._assert_view(user, client_id)
        if client_id is None or not self.access.can(
            user, "manage_operational_exceptions", client_id
        ):
            raise ForbiddenError("Acknowledge / Resolve Exceptions permission required")

    def _serialize(self, row: PayflowOperationalExceptionModel) -> dict[str, Any]:
        client = row.client
        account = row.account
        return {
            "id": row.id,
            "exception_type": row.exception_type,
            "severity": row.severity,
            "status": row.status,
            "retriable": bool(row.retriable),
            "client_id": row.client_id,
            "client_name": client.name if client else None,
            "account_id": row.account_id,
            "account_reference": account.account_reference if account else None,
            "customer_name": account.customer_name if account else None,
            "case_reference": account.case_reference if account else None,
            "source_entity_type": row.source_entity_type,
            "source_entity_id": row.source_entity_id,
            "reason": row.reason,
            "diagnostic_ref": row.diagnostic_ref,
            "link": row.link,
            "dedupe_key": row.dedupe_key,
            "acknowledged_by": row.acknowledged_by,
            "acknowledged_at": row.acknowledged_at,
            "resolved_by": row.resolved_by,
            "resolved_at": row.resolved_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def raise_exception(
        self,
        *,
        exception_type: str,
        reason: str,
        client_id: int | None = None,
        account_id: int | None = None,
        source_entity_type: str | None = None,
        source_entity_id: str | int | None = None,
        severity: str = PayflowOperationalExceptionSeverity.MEDIUM.value,
        retriable: bool = True,
        diagnostic_ref: str | None = None,
        link: str | None = None,
        dedupe_key: str | None = None,
        notify: bool = True,
        commit: bool = True,
    ) -> PayflowOperationalExceptionModel | None:
        """Create Open exception or no-op if an unresolved duplicate exists.

        Persists the exception first; notifications are best-effort afterward.
        """
        valid_types = {t.value for t in PayflowOperationalExceptionType}
        if exception_type not in valid_types:
            raise ValidationAppError(f"Invalid exception_type: {exception_type}")

        key = dedupe_key or "|".join(
            [
                exception_type,
                str(client_id or ""),
                str(account_id or ""),
                str(source_entity_type or ""),
                str(source_entity_id or ""),
            ]
        )[:255]

        existing = (
            self.db.query(PayflowOperationalExceptionModel)
            .filter(
                PayflowOperationalExceptionModel.dedupe_key == key,
                PayflowOperationalExceptionModel.status.in_(
                    [
                        PayflowOperationalExceptionStatus.OPEN.value,
                        PayflowOperationalExceptionStatus.ACKNOWLEDGED.value,
                    ]
                ),
            )
            .first()
        )
        if existing:
            return existing

        # Also catch unique constraint race on resolved→new: allow new open after resolve
        # by using key only among open/ack; if unique on all statuses, suffix resolved ids.
        # Model has unique on dedupe_key alone — for resolved rows, rewrite key so new opens work.
        prior_resolved = (
            self.db.query(PayflowOperationalExceptionModel)
            .filter(PayflowOperationalExceptionModel.dedupe_key == key)
            .first()
        )
        if prior_resolved and prior_resolved.status == PayflowOperationalExceptionStatus.RESOLVED.value:
            prior_resolved.dedupe_key = f"{key}#resolved:{prior_resolved.id}"[:255]
            self.db.flush()

        resolved_link = link
        if not resolved_link and account_id:
            resolved_link = f"/payflow/cases/{account_id}"
        elif not resolved_link and source_entity_type == "communication" and source_entity_id:
            resolved_link = f"/payflow/comms/{source_entity_id}"

        row = PayflowOperationalExceptionModel(
            exception_type=exception_type,
            severity=severity or PayflowOperationalExceptionSeverity.MEDIUM.value,
            status=PayflowOperationalExceptionStatus.OPEN.value,
            retriable=bool(retriable),
            client_id=client_id,
            account_id=account_id,
            source_entity_type=source_entity_type,
            source_entity_id=str(source_entity_id) if source_entity_id is not None else None,
            reason=(reason or "Operational failure")[:4000],
            diagnostic_ref=(diagnostic_ref or None),
            link=resolved_link,
            dedupe_key=key,
        )
        self.db.add(row)
        self.db.flush()

        if commit:
            self.db.commit()
            self.db.refresh(row)

        if notify:
            try:
                users = PayflowNotificationService(self.db).recipients_for_client(client_id)
                type_label = exception_type.replace("_", " ").title()
                PayflowNotificationService(self.db).notify(
                    users=users,
                    notification_type=PayflowNotificationType.OPERATIONAL_EXCEPTION.value,
                    title=f"{type_label} · {severity}",
                    body=reason[:280],
                    link=resolved_link or f"/payflow/exceptions?id={row.id}",
                    client_id=client_id,
                    entity_type="operational_exception",
                    entity_id=str(row.id),
                    send_mail=False,
                    dedupe=True,
                )
            except Exception:  # noqa: BLE001
                # Exception record already durable; notification failure must not roll it back.
                pass

        return row

    def list_exceptions(
        self,
        user: UserModel,
        *,
        status: str | None = None,
        exception_type: str | None = None,
        client_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        allowed = None if self.access.is_operations_admin(user) else self.access.build_context(user).get(
            "client_ids"
        )
        q = self.db.query(PayflowOperationalExceptionModel).options(
            joinedload(PayflowOperationalExceptionModel.client),
            joinedload(PayflowOperationalExceptionModel.account),
        )
        if allowed is not None:
            if not allowed:
                return self._empty()
            q = q.filter(PayflowOperationalExceptionModel.client_id.in_(allowed))
        if client_id is not None:
            self._assert_view(user, client_id)
            q = q.filter(PayflowOperationalExceptionModel.client_id == client_id)
        if status:
            q = q.filter(PayflowOperationalExceptionModel.status == status)
        if exception_type:
            q = q.filter(PayflowOperationalExceptionModel.exception_type == exception_type)

        rows = q.order_by(PayflowOperationalExceptionModel.id.desc()).all()
        if not self.access.is_operations_admin(user):
            rows = [
                r
                for r in rows
                if r.client_id is None
                or self.access.can(user, "view_operational_exceptions", r.client_id)
            ]

        open_count = sum(
            1 for r in rows if r.status == PayflowOperationalExceptionStatus.OPEN.value
        )
        ack_count = sum(
            1
            for r in rows
            if r.status == PayflowOperationalExceptionStatus.ACKNOWLEDGED.value
        )
        resolved_count = sum(
            1 for r in rows if r.status == PayflowOperationalExceptionStatus.RESOLVED.value
        )
        total = len(rows)
        page = rows[offset : offset + max(1, min(limit, 500))]
        return {
            "exceptions": [self._serialize(r) for r in page],
            "total": total,
            "open_count": open_count,
            "acknowledged_count": ack_count,
            "resolved_count": resolved_count,
        }

    def get_exception(self, user: UserModel, exception_id: int) -> dict[str, Any]:
        row = (
            self.db.query(PayflowOperationalExceptionModel)
            .options(
                joinedload(PayflowOperationalExceptionModel.client),
                joinedload(PayflowOperationalExceptionModel.account),
            )
            .filter(PayflowOperationalExceptionModel.id == exception_id)
            .first()
        )
        if not row:
            raise NotFoundError("Operational exception not found")
        self._assert_view(user, row.client_id)
        return self._serialize(row)

    def acknowledge(self, user: UserModel, exception_id: int) -> dict[str, Any]:
        row = self.db.get(PayflowOperationalExceptionModel, exception_id)
        if not row:
            raise NotFoundError("Operational exception not found")
        self._assert_manage(user, row.client_id)
        if row.status == PayflowOperationalExceptionStatus.RESOLVED.value:
            raise ValidationAppError("Resolved exceptions cannot be acknowledged")
        if row.status == PayflowOperationalExceptionStatus.OPEN.value:
            row.status = PayflowOperationalExceptionStatus.ACKNOWLEDGED.value
            row.acknowledged_by = user.email or str(user.id)
            row.acknowledged_at = _utcnow()
            self.db.commit()
            self.db.refresh(row)
        return self.get_exception(user, exception_id)

    def resolve(
        self, user: UserModel, exception_id: int, *, note: str | None = None
    ) -> dict[str, Any]:
        row = self.db.get(PayflowOperationalExceptionModel, exception_id)
        if not row:
            raise NotFoundError("Operational exception not found")
        self._assert_manage(user, row.client_id)
        if row.status == PayflowOperationalExceptionStatus.RESOLVED.value:
            return self.get_exception(user, exception_id)
        if row.status == PayflowOperationalExceptionStatus.OPEN.value:
            row.acknowledged_by = row.acknowledged_by or (user.email or str(user.id))
            row.acknowledged_at = row.acknowledged_at or _utcnow()
        row.status = PayflowOperationalExceptionStatus.RESOLVED.value
        row.resolved_by = user.email or str(user.id)
        row.resolved_at = _utcnow()
        if note:
            row.diagnostic_ref = (
                f"{row.diagnostic_ref + ' | ' if row.diagnostic_ref else ''}resolve_note={note[:400]}"
            )
        # Free dedupe key for future recurrence after resolve.
        row.dedupe_key = f"{row.dedupe_key}#resolved:{row.id}"[:255]
        self.db.commit()
        self.db.refresh(row)
        return self.get_exception(user, exception_id)

    def _empty(self) -> dict[str, Any]:
        return {
            "exceptions": [],
            "total": 0,
            "open_count": 0,
            "acknowledged_count": 0,
            "resolved_count": 0,
        }
