from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError, NotFoundError
from app.infrastructure.database.models import PayflowCommunicationModel, UserModel
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import PayflowCommChannel, PayflowCommPurpose, PayflowCommStatus

DROP_OFF_SEGMENTS = [
    "Delivered but Not Opened / Read",
    "Opened / Read but Did Not Click",
    "Clicked but Did Not Initiate Payment",
]


def _drop_off_segment(status: str, payment_link: bool) -> str | None:
    if status == PayflowCommStatus.DELIVERED.value:
        return DROP_OFF_SEGMENTS[0]
    if status == PayflowCommStatus.OPENED_READ.value and payment_link:
        return DROP_OFF_SEGMENTS[1]
    if status == PayflowCommStatus.PAYMENT_LINK_CLICKED.value:
        return DROP_OFF_SEGMENTS[2]
    return None


class PayflowCommunicationService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _visible_client_ids(self, user: UserModel) -> list[int] | None:
        if self.access.is_operations_admin(user):
            return None
        ctx = self.access.build_context(user)
        return list(ctx.get("client_ids") or [])

    def _assert_view(self, user: UserModel, client_id: int) -> None:
        if self.access.is_operations_admin(user):
            return
        if not self.access.can_see_client(user, client_id):
            raise ForbiddenError("Communication access denied")
        if not self.access.can(user, "view_communications", client_id):
            raise ForbiddenError("View Communications permission required")

    def _serialize(self, row: PayflowCommunicationModel) -> dict[str, Any]:
        account = row.account
        client = row.client
        return {
            "id": row.id,
            "code": row.code,
            "client_id": row.client_id,
            "client_code": client.code if client else None,
            "client_name": client.name if client else None,
            "account_id": row.account_id,
            "customer_name": account.customer_name if account else None,
            "account_reference": account.account_reference if account else None,
            "case_reference": account.case_reference if account else None,
            "channel": row.channel,
            "purpose": row.purpose,
            "status": row.status,
            "workflow_name": row.workflow_name,
            "engagement": row.engagement,
            "date_bucket": row.date_bucket,
            "date_label": row.date_label,
            "time_label": row.time_label,
            "subject": row.subject,
            "body_lines": list(row.body_lines or []),
            "payment_link": bool(row.payment_link),
            "why_message": row.why_message,
            "why_channel": row.why_channel,
            "why_timing": row.why_timing,
            "events": list(row.events or []),
            "balance": float(row.balance or 0),
            "review_id": row.review_id,
            "drop_off_segment": _drop_off_segment(row.status, bool(row.payment_link)),
            "brand_name": client.brand_name or (client.name if client else None),
            "sender_name": client.sender_name,
            "email_from": client.email_from,
            "sms_sender_id": client.sms_sender_id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def list_communications(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        account_id: int | None = None,
        status: str | None = None,
        channel: str | None = None,
        purpose: str | None = None,
        workflow: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        allowed = self._visible_client_ids(user)
        q = self.db.query(PayflowCommunicationModel).options(
            joinedload(PayflowCommunicationModel.client),
            joinedload(PayflowCommunicationModel.account),
        )
        if allowed is not None:
            if not allowed:
                return self._empty()
            q = q.filter(PayflowCommunicationModel.client_id.in_(allowed))
        if client_id is not None:
            self._assert_view(user, client_id)
            q = q.filter(PayflowCommunicationModel.client_id == client_id)
        if account_id is not None:
            q = q.filter(PayflowCommunicationModel.account_id == account_id)
        if status:
            q = q.filter(PayflowCommunicationModel.status == status)
        if channel:
            q = q.filter(PayflowCommunicationModel.channel == channel)
        if purpose:
            q = q.filter(PayflowCommunicationModel.purpose == purpose)
        if workflow:
            q = q.filter(PayflowCommunicationModel.workflow_name == workflow)

        rows = q.order_by(PayflowCommunicationModel.id.desc()).all()
        if not self.access.is_operations_admin(user):
            rows = [
                r for r in rows if self.access.can(user, "view_communications", r.client_id)
            ]
        if search:
            term = search.strip().lower()
            rows = [
                r
                for r in rows
                if term
                in " ".join(
                    [
                        (r.account.customer_name if r.account else ""),
                        (r.account.account_reference if r.account else ""),
                        r.purpose or "",
                        r.channel or "",
                        r.workflow_name or "",
                    ]
                ).lower()
            ]

        summary = {
            "sent_today": sum(1 for r in rows if r.date_bucket == "Today"),
            "delivered": sum(
                1
                for r in rows
                if r.status
                in {
                    PayflowCommStatus.DELIVERED.value,
                    PayflowCommStatus.OPENED_READ.value,
                    PayflowCommStatus.PAYMENT_LINK_CLICKED.value,
                }
            ),
            "engaged": sum(
                1
                for r in rows
                if r.status
                in {
                    PayflowCommStatus.OPENED_READ.value,
                    PayflowCommStatus.PAYMENT_LINK_CLICKED.value,
                }
            ),
            "clicks": sum(
                1 for r in rows if r.status == PayflowCommStatus.PAYMENT_LINK_CLICKED.value
            ),
            "failed": sum(1 for r in rows if r.status == PayflowCommStatus.FAILED.value),
        }
        drop_off = {
            seg: sum(1 for r in rows if _drop_off_segment(r.status, bool(r.payment_link)) == seg)
            for seg in DROP_OFF_SEGMENTS
        }
        workflows = sorted({r.workflow_name for r in rows if r.workflow_name})
        return {
            "communications": [self._serialize(r) for r in rows],
            "summary": summary,
            "drop_off": drop_off,
            "statuses": [s.value for s in PayflowCommStatus],
            "channels": [c.value for c in PayflowCommChannel if c != PayflowCommChannel.WHATSAPP],
            "purposes": [p.value for p in PayflowCommPurpose],
            "workflows": workflows,
            "drop_off_segments": DROP_OFF_SEGMENTS,
        }

    def get_communication(self, user: UserModel, communication_id: int) -> dict[str, Any]:
        row = (
            self.db.query(PayflowCommunicationModel)
            .options(
                joinedload(PayflowCommunicationModel.client),
                joinedload(PayflowCommunicationModel.account),
            )
            .filter(PayflowCommunicationModel.id == communication_id)
            .first()
        )
        if not row:
            raise NotFoundError("Communication not found")
        self._assert_view(user, row.client_id)
        return self._serialize(row)

    def _empty(self) -> dict[str, Any]:
        return {
            "communications": [],
            "summary": {
                "sent_today": 0,
                "delivered": 0,
                "engaged": 0,
                "clicks": 0,
                "failed": 0,
            },
            "drop_off": {seg: 0 for seg in DROP_OFF_SEGMENTS},
            "statuses": [s.value for s in PayflowCommStatus],
            "channels": [c.value for c in PayflowCommChannel if c != PayflowCommChannel.WHATSAPP],
            "purposes": [p.value for p in PayflowCommPurpose],
            "workflows": [],
            "drop_off_segments": DROP_OFF_SEGMENTS,
        }
