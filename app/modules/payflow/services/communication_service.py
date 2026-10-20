from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowCommunicationModel,
    PayflowStrategyModel,
    UserModel,
)
from app.infrastructure.email.sendgrid_service import send_collection_email
from app.infrastructure.sms.twilio_service import send_collection_sms
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import PayflowCommChannel, PayflowCommPurpose, PayflowCommStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def _date_meta(when: datetime | None = None) -> tuple[str, str, str]:
    """Return (date_bucket, date_label, time_label) for list KPIs and filters."""
    now = when or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone()
    today = datetime.now(local.tzinfo).date()
    d = local.date()
    if d == today:
        bucket = "Today"
    elif d == today - timedelta(days=1):
        bucket = "Yesterday"
    elif d >= today - timedelta(days=6):
        bucket = "Last 7 Days"
    else:
        bucket = "This Month"
    date_label = local.strftime("%d %b %Y")
    time_label = local.strftime("%H:%M")
    return bucket, date_label, time_label


def _payment_link_url(account: PayflowAccountModel | None, has_link: bool) -> str | None:
    if not has_link or not account or not account.account_reference:
        return None
    base = (settings.WORKFLOW_PAYMENT_LINK_BASE or "").rstrip("/")
    if not base:
        return None
    return f"{base}/{account.account_reference}"


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
        has_link = bool(row.payment_link)
        date_bucket = row.date_bucket
        date_label = row.date_label
        time_label = row.time_label
        if not date_bucket or not date_label or not time_label:
            fallback_bucket, fallback_date, fallback_time = _date_meta(row.created_at)
            date_bucket = date_bucket or fallback_bucket
            date_label = date_label or fallback_date
            time_label = time_label or fallback_time
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
            "case_status": (account.collection_status if account else None),
            "channel": row.channel,
            "purpose": row.purpose,
            "status": row.status,
            "workflow_name": row.workflow_name,
            "engagement": row.engagement,
            "date_bucket": date_bucket,
            "date_label": date_label,
            "time_label": time_label,
            "subject": row.subject,
            "body_lines": list(row.body_lines or []),
            "payment_link": has_link,
            "payment_link_url": _payment_link_url(account, has_link),
            "why_message": row.why_message,
            "why_channel": row.why_channel,
            "why_timing": row.why_timing,
            "events": list(row.events or []),
            "balance": float(row.balance or 0),
            "review_id": row.review_id,
            "drop_off_segment": _drop_off_segment(row.status, has_link),
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
                        r.code or "",
                        str(r.id),
                        (r.account.customer_name if r.account else ""),
                        (r.account.account_reference if r.account else ""),
                        (r.account.case_reference if r.account else ""),
                        r.purpose or "",
                        r.channel or "",
                        r.workflow_name or "",
                        r.status or "",
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

    def send_workflow_step(
        self,
        *,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        channel: str,
        purpose: str,
        subject: str,
        body: str,
        payment_link: str | None = None,
        run_id: int | None = None,
        client_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a workflow communication via providers and persist the comm record.

        Workflow engine must call this instead of provider APIs directly.
        """
        channel_norm = (channel or "Email").strip()
        purpose_norm = purpose or "Payment Reminder"
        send_status = "logged"
        recipient = ""
        skip_reason: str | None = None
        provider_error: str | None = None

        if channel_norm == "SMS":
            phone = (account.phone_mobile or account.phone_work or "").strip()
            if not phone:
                skip_reason = "no_phone"
            else:
                recipient = phone
                sms = send_collection_sms(
                    self.db,
                    to_phone=phone,
                    body=body,
                    sms_type="collection_workflow",
                    account_id=account.id,
                    run_id=run_id,
                    client_id=client_id or account.client_id,
                    commit=False,
                )
                send_status = sms.status
                account.last_sms_sent_date = _utcnow().date()
                if sms.status == "failed":
                    provider_error = sms.error or "failed"
        else:
            email = (account.email or "").strip()
            if not email:
                skip_reason = "no_email"
            else:
                recipient = email
                log = send_collection_email(
                    self.db,
                    to_email=email,
                    subject=subject,
                    body=body,
                    email_type="collection_workflow",
                    action_link=payment_link,
                    commit=False,
                )
                send_status = log.status
                account.last_email_sent_date = _utcnow().date()
                if log.status == "failed":
                    provider_error = "sendgrid_failed"

        if skip_reason:
            return {
                "skipped": skip_reason,
                "channel": channel_norm,
                "status": "skipped",
                "communication_id": None,
            }

        now = _utcnow()
        account.last_contact_date = now.date()
        code = f"COM-{uuid4().hex[:10].upper()}"
        comm_status = (
            PayflowCommStatus.SENT.value
            if send_status in {"sent", "logged"}
            else PayflowCommStatus.FAILED.value
        )
        date_bucket, date_label, time_label = _date_meta(now)
        # Open/read tracking starts empty; SMS stays not-trackable in the UI when null.
        engagement = None
        comm = PayflowCommunicationModel(
            code=code,
            client_id=client_id or account.client_id,
            account_id=account.id,
            channel=channel_norm,
            purpose=purpose_norm,
            status=comm_status,
            workflow_name=strategy.name,
            engagement=engagement,
            date_bucket=date_bucket,
            date_label=date_label,
            time_label=time_label,
            subject=subject if channel_norm != "SMS" else None,
            body_lines=body.splitlines()[:40],
            payment_link=bool(payment_link),
            why_message="Workflow communication step",
            why_channel=channel_norm,
            why_timing="Scheduled by workflow worker",
            events=[{"at": now.isoformat(), "label": f"{channel_norm} {send_status}"}],
            balance=float(account.outstanding_balance or 0),
        )
        self.db.add(comm)
        self.db.flush()
        return {
            "skipped": None,
            "channel": channel_norm,
            "status": send_status,
            "comm_status": comm_status,
            "recipient": recipient,
            "communication_id": comm.id,
            "provider_error": provider_error,
            "code": code,
        }

    def update_status(
        self,
        communication_id: int,
        status: str,
        *,
        event_label: str | None = None,
        commit: bool = True,
    ) -> PayflowCommunicationModel:
        row = self.db.get(PayflowCommunicationModel, communication_id)
        if not row:
            raise NotFoundError("Communication not found")
        previous = row.status
        row.status = status
        events = list(row.events or [])
        events.append(
            {
                "at": _utcnow().isoformat(),
                "label": event_label or f"Status → {status}",
                "detail": f"{previous} → {status}",
            }
        )
        row.events = events[-40:]
        if commit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        try:
            from app.modules.payflow.services.workflow_execution_service import (
                WorkflowExecutionService,
            )

            WorkflowExecutionService(self.db).on_communication_event(
                communication_id, status, commit=commit
            )
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger("app.comms").exception(
                "workflow advance failed for communication_id=%s", communication_id
            )
        return row

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
