from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationEventModel,
    PayflowCommunicationModel,
    PayflowStrategyModel,
    UserModel,
)
from app.infrastructure.email.collection_provider import (
    active_collection_provider,
    send_collection_email,
)
from app.infrastructure.sms.twilio_service import (
    normalize_e164,
    send_collection_sms,
    truncate_sms_body,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import PayflowCommChannel, PayflowCommPurpose, PayflowCommStatus

logger = logging.getLogger("app.comms")

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _valid_email(value: str | None) -> bool:
    text = (value or "").strip()
    return bool(text and _EMAIL_RE.match(text) and len(text) <= 255)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


DROP_OFF_SEGMENTS = [
    "Delivered but Not Opened / Read",
    "Opened / Read but Did Not Click",
    "Clicked but Did Not Initiate Payment",
]

_STATUS_RANK = {
    PayflowCommStatus.PREPARED.value: 10,
    PayflowCommStatus.AWAITING_GOVERNANCE.value: 15,
    PayflowCommStatus.APPROVED.value: 20,
    PayflowCommStatus.SCHEDULED.value: 25,
    PayflowCommStatus.QUEUED.value: 28,
    PayflowCommStatus.SENT.value: 30,
    PayflowCommStatus.DELIVERED.value: 40,
    PayflowCommStatus.OPENED_READ.value: 50,
    PayflowCommStatus.PAYMENT_LINK_CLICKED.value: 60,
    PayflowCommStatus.FAILED.value: 100,
    PayflowCommStatus.SUPPRESSED.value: 100,
}


def _drop_off_segment(status: str, payment_link: bool, payment_initiated: bool = False) -> str | None:
    if status == PayflowCommStatus.DELIVERED.value:
        return DROP_OFF_SEGMENTS[0]
    if status == PayflowCommStatus.OPENED_READ.value and payment_link:
        return DROP_OFF_SEGMENTS[1]
    if status == PayflowCommStatus.PAYMENT_LINK_CLICKED.value and not payment_initiated:
        return DROP_OFF_SEGMENTS[2]
    return None


def _date_meta(when: datetime | None = None) -> tuple[str, str, str]:
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
    return bucket, local.strftime("%d %b %Y"), local.strftime("%H:%M")


def _redact_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
        # Keep path shape but strip query/fragment (payment tokens often live there).
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:  # noqa: BLE001
        return "[redacted]"


def _days_since(start: datetime | None) -> int | None:
    if not start:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return max(0, (_utcnow() - start).days)


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

    def _resolve_payment_link_url(
        self, row: PayflowCommunicationModel, account: PayflowAccountModel | None
    ) -> str | None:
        try:
            from app.modules.payflow.services.payment_link_service import (
                PayflowPaymentLinkService,
            )

            return PayflowPaymentLinkService(self.db).url_for_communication(row, account)
        except Exception:  # noqa: BLE001
            return None

    def _ensure_payment_url(
        self, account: PayflowAccountModel, *, communication_id: int | None = None
    ) -> str | None:
        try:
            from app.modules.payflow.services.payment_link_service import (
                PayflowPaymentLinkService,
            )

            link = PayflowPaymentLinkService(self.db).ensure_active_link(
                account, communication_id=communication_id, commit=False
            )
            return PayflowPaymentLinkService(self.db).public_url(link)
        except Exception:  # noqa: BLE001
            logger.exception("failed to ensure payment link for account_id=%s", account.id)
            return None

    def _append_json_event(self, row: PayflowCommunicationModel, label: str, detail: str | None = None) -> None:
        events = list(row.events or [])
        events.append({"at": _utcnow().isoformat(), "label": label, "detail": detail})
        row.events = events[-80:]

    def _record_event_row(
        self,
        row: PayflowCommunicationModel,
        *,
        event_type: str,
        occurred_at: datetime | None = None,
        detail: str | None = None,
        provider_event_id: str | None = None,
        payload: dict | None = None,
        label: str | None = None,
    ) -> bool:
        """Persist provider/internal event. Returns False if duplicate provider_event_id."""
        if provider_event_id:
            exists = (
                self.db.query(PayflowCommunicationEventModel.id)
                .filter(PayflowCommunicationEventModel.provider_event_id == provider_event_id)
                .first()
            )
            if exists:
                return False
        when = occurred_at or _utcnow()
        self.db.add(
            PayflowCommunicationEventModel(
                communication_id=row.id,
                provider_event_id=provider_event_id,
                event_type=event_type,
                occurred_at=when,
                detail=detail,
                payload=payload,
            )
        )
        self._append_json_event(row, label or event_type.replace("_", " ").title(), detail)
        return True

    def _advance_status(self, row: PayflowCommunicationModel, new_status: str) -> None:
        current = _STATUS_RANK.get(row.status or "", 0)
        incoming = _STATUS_RANK.get(new_status, 0)
        # Terminal Failed/Suppressed can overwrite lower ranks; never regress engagement ladder.
        if incoming >= 100 or incoming > current:
            row.status = new_status

    def _engagement_label(self, row: PayflowCommunicationModel) -> str | None:
        tracking = row.engagement_tracking or (
            "not_available" if (row.channel or "") == "SMS" else "available"
        )
        if tracking == "not_available":
            return None
        if row.payment_completed:
            return "Payment completed"
        if row.payment_initiated:
            return "Payment initiated"
        if (row.clicks_count or 0) > 0 or row.status == PayflowCommStatus.PAYMENT_LINK_CLICKED.value:
            return "Payment link clicked"
        if (row.opens_count or 0) > 0 or row.status == PayflowCommStatus.OPENED_READ.value:
            return "Opened / Read"
        if row.delivered_at or row.status == PayflowCommStatus.DELIVERED.value:
            return "Delivered · no open yet"
        return row.engagement

    def _serialize(self, row: PayflowCommunicationModel, *, user: UserModel | None = None) -> dict[str, Any]:
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

        tracking = row.engagement_tracking or (
            "not_available" if (row.channel or "") == "SMS" else "available"
        )
        delivered_at = row.delivered_at
        days_wo_open = None
        days_wo_click = None
        if tracking == "available" and delivered_at:
            if not row.first_opened_at:
                days_wo_open = _days_since(delivered_at)
            if not row.first_clicked_at:
                days_wo_click = _days_since(delivered_at)

        can_view_rule = False
        if user and row.rule_id and row.client_id:
            can_view_rule = self.access.is_operations_admin(user) or self.access.can(
                user, "view_rules", row.client_id
            )

        eligibility_status = row.eligibility_status
        if not eligibility_status:
            if row.status == PayflowCommStatus.SUPPRESSED.value:
                eligibility_status = "Blocked"
            elif row.status == PayflowCommStatus.FAILED.value:
                eligibility_status = "Failed"
            elif row.status == PayflowCommStatus.SCHEDULED.value:
                eligibility_status = "Scheduled"
            elif row.status in {
                PayflowCommStatus.QUEUED.value,
                PayflowCommStatus.SENT.value,
                PayflowCommStatus.DELIVERED.value,
                PayflowCommStatus.OPENED_READ.value,
                PayflowCommStatus.PAYMENT_LINK_CLICKED.value,
            }:
                eligibility_status = "Eligible"
            else:
                eligibility_status = "Eligible"

        recipient = row.recipient
        if not recipient and account:
            if (row.channel or "") == "SMS":
                recipient = (account.phone_mobile or account.phone_work or None)
            else:
                recipient = account.email

        provider_error = row.provider_error
        if not provider_error and row.status == PayflowCommStatus.FAILED.value:
            for ev in reversed(list(row.events or [])):
                if isinstance(ev, dict) and (
                    "fail" in (ev.get("label") or "").lower()
                    or "bounce" in (ev.get("label") or "").lower()
                ):
                    provider_error = ev.get("detail")
                    break

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
            "recipient": recipient,
            "provider_error": provider_error,
            "workflow_name": row.workflow_name,
            "engagement": self._engagement_label(row),
            "engagement_tracking": tracking,
            "date_bucket": date_bucket,
            "date_label": date_label,
            "time_label": time_label,
            "subject": row.subject,
            "body_lines": list(row.body_lines or []),
            "payment_link": has_link,
            "payment_link_url": self._resolve_payment_link_url(row, account) if has_link else None,
            "why_message": row.why_message,
            "why_channel": row.why_channel,
            "why_timing": row.why_timing,
            "events": list(row.events or []),
            "balance": float(row.balance or 0),
            "review_id": row.review_id,
            "drop_off_segment": _drop_off_segment(
                row.status, has_link, bool(row.payment_initiated)
            ),
            "brand_name": client.brand_name or (client.name if client else None),
            "sender_name": client.sender_name,
            "email_from": client.email_from,
            "sms_sender_id": client.sms_sender_id,
            "provider": row.provider,
            "provider_message_id": row.provider_message_id,
            "sent_at": row.sent_at or row.created_at,
            "delivered_at": row.delivered_at,
            "opens_count": int(row.opens_count or 0),
            "clicks_count": int(row.clicks_count or 0),
            "first_opened_at": row.first_opened_at,
            "last_opened_at": row.last_opened_at,
            "first_clicked_at": row.first_clicked_at,
            "last_clicked_at": row.last_clicked_at,
            "last_clicked_url": _redact_url(row.last_clicked_url),
            "days_since_delivery_without_open": days_wo_open,
            "days_since_delivery_without_click": days_wo_click,
            "payment_initiated": bool(row.payment_initiated),
            "payment_completed": bool(row.payment_completed),
            "eligibility_status": eligibility_status,
            "restriction_reason": row.restriction_reason,
            "rule_id": row.rule_id if can_view_rule else None,
            "rule_name": row.rule_name if can_view_rule else None,
            "rule_code": row.rule_code if can_view_rule else None,
            "next_eligible_at": row.next_eligible_at,
            "can_view_rule": can_view_rule,
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
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
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
        # Apply workflow after facet options are built so the Workflow dropdown
        # still lists every workflow available under the other filters.
        if date_from is not None:
            q = q.filter(PayflowCommunicationModel.created_at >= date_from)
        if date_to is not None:
            q = q.filter(PayflowCommunicationModel.created_at <= date_to)

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

        workflows = sorted({r.workflow_name for r in rows if r.workflow_name})
        if workflow:
            rows = [r for r in rows if r.workflow_name == workflow]

        total = len(rows)
        page = rows[offset : offset + max(1, min(limit, 500))]
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
                or (r.opens_count or 0) > 0
                or (r.clicks_count or 0) > 0
            ),
            "clicks": sum(
                1
                for r in rows
                if r.status == PayflowCommStatus.PAYMENT_LINK_CLICKED.value
                or (r.clicks_count or 0) > 0
            ),
            "failed": sum(1 for r in rows if r.status == PayflowCommStatus.FAILED.value),
        }
        drop_off = {
            seg: sum(
                1
                for r in rows
                if _drop_off_segment(r.status, bool(r.payment_link), bool(r.payment_initiated))
                == seg
            )
            for seg in DROP_OFF_SEGMENTS
        }
        return {
            "communications": [self._serialize(r, user=user) for r in page],
            "summary": summary,
            "drop_off": drop_off,
            "statuses": [s.value for s in PayflowCommStatus],
            "channels": [c.value for c in PayflowCommChannel if c != PayflowCommChannel.WHATSAPP],
            "purposes": [p.value for p in PayflowCommPurpose],
            "workflows": workflows,
            "drop_off_segments": DROP_OFF_SEGMENTS,
            "total": total,
            "limit": limit,
            "offset": offset,
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
        return self._serialize(row, user=user)

    def evaluate_send_eligibility(
        self,
        account: PayflowAccountModel,
        *,
        channel: str,
    ) -> dict[str, Any]:
        """Pre-send gates: channel flags, consent, contact, holds."""
        channel_norm = (channel or "Email").strip()
        client: PayflowClientModel | None = account.client
        if account.provincial_hold:
            return {
                "ok": False,
                "eligibility_status": "Blocked",
                "restriction_reason": "Account is on provincial hold",
                "next_eligible_at": None,
            }
        status = (account.collection_status or "").strip().lower()
        if status in {"resolved", "closed", "ineligible", "paid"}:
            return {
                "ok": False,
                "eligibility_status": "Blocked",
                "restriction_reason": f"Case status '{account.collection_status}' prevents outreach",
                "next_eligible_at": None,
            }
        if channel_norm == "SMS":
            if client is not None and client.channel_sms is False:
                return {
                    "ok": False,
                    "eligibility_status": "Blocked",
                    "restriction_reason": "SMS channel is disabled for this client",
                    "next_eligible_at": None,
                }
            if getattr(account, "sms_opt_out", False):
                return {
                    "ok": False,
                    "eligibility_status": "Blocked",
                    "restriction_reason": "Customer opted out of SMS (STOP)",
                    "next_eligible_at": None,
                }
            phone = (account.phone_mobile or account.phone_work or "").strip()
            if not phone:
                return {
                    "ok": False,
                    "eligibility_status": "Blocked",
                    "restriction_reason": "No valid mobile/work phone on file",
                    "next_eligible_at": None,
                }
            if not normalize_e164(phone):
                return {
                    "ok": False,
                    "eligibility_status": "Blocked",
                    "restriction_reason": "Phone number is not a valid E.164 / NANP number",
                    "next_eligible_at": None,
                }
        else:
            if client is not None and client.channel_email is False:
                return {
                    "ok": False,
                    "eligibility_status": "Blocked",
                    "restriction_reason": "Email channel is disabled for this client",
                    "next_eligible_at": None,
                }
            email = (account.email or "").strip()
            if not email:
                return {
                    "ok": False,
                    "eligibility_status": "Blocked",
                    "restriction_reason": "No valid email on file",
                    "next_eligible_at": None,
                }
            if not _valid_email(email):
                return {
                    "ok": False,
                    "eligibility_status": "Blocked",
                    "restriction_reason": "Email address format is invalid",
                    "next_eligible_at": None,
                }
            if account.email_consent is False:
                return {
                    "ok": False,
                    "eligibility_status": "Blocked",
                    "restriction_reason": "Customer has not consented to email",
                    "next_eligible_at": None,
                }
        return {"ok": True, "eligibility_status": "Eligible", "restriction_reason": None, "next_eligible_at": None}

    def record_blocked_communication(
        self,
        *,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel | Any | None,
        channel: str,
        purpose: str,
        subject: str | None = None,
        body: str | None = None,
        payment_link: bool = False,
        restriction_reason: str,
        eligibility_status: str = "Blocked",
        rule_id: int | None = None,
        rule_name: str | None = None,
        rule_code: str | None = None,
        next_eligible_at: datetime | None = None,
        client_id: int | None = None,
    ) -> PayflowCommunicationModel:
        now = _utcnow()
        date_bucket, date_label, time_label = _date_meta(now)
        channel_norm = (channel or "Email").strip()
        code = f"COM-{uuid4().hex[:10].upper()}"
        body_lines = (body or "").splitlines()[:40] if body else []
        comm = PayflowCommunicationModel(
            code=code,
            client_id=client_id or account.client_id,
            account_id=account.id,
            channel=channel_norm,
            purpose=purpose or "Payment Reminder",
            status=PayflowCommStatus.SUPPRESSED.value
            if eligibility_status == "Blocked"
            else PayflowCommStatus.FAILED.value,
            workflow_name=strategy.name if strategy else account.current_workflow,
            engagement=None,
            engagement_tracking="not_available" if channel_norm == "SMS" else "available",
            date_bucket=date_bucket,
            date_label=date_label,
            time_label=time_label,
            subject=subject if channel_norm != "SMS" else None,
            body_lines=body_lines,
            payment_link=bool(payment_link),
            why_message="Communication blocked by governance or eligibility checks",
            why_channel=channel_norm,
            why_timing="Evaluated at workflow execution time",
            events=[],
            balance=float(account.outstanding_balance or 0),
            eligibility_status=eligibility_status,
            restriction_reason=restriction_reason,
            rule_id=rule_id,
            rule_name=rule_name,
            rule_code=rule_code,
            next_eligible_at=next_eligible_at,
        )
        self.db.add(comm)
        self.db.flush()
        self._record_event_row(
            comm,
            event_type="governance_blocked",
            detail=restriction_reason,
            label="Governance Check Blocked",
        )
        return comm

    def send_workflow_step(
        self,
        *,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel | Any,
        channel: str,
        purpose: str,
        subject: str,
        body: str,
        payment_link: str | None = None,
        run_id: int | None = None,
        client_id: int | None = None,
    ) -> dict[str, Any]:
        channel_norm = (channel or "Email").strip()
        purpose_norm = purpose or "Payment Reminder"

        gate = self.evaluate_send_eligibility(account, channel=channel_norm)
        if not gate.get("ok"):
            blocked = self.record_blocked_communication(
                account=account,
                strategy=strategy,
                channel=channel_norm,
                purpose=purpose_norm,
                subject=subject,
                body=body,
                payment_link=bool(payment_link),
                restriction_reason=str(gate.get("restriction_reason") or "Not eligible"),
                eligibility_status=str(gate.get("eligibility_status") or "Blocked"),
                next_eligible_at=gate.get("next_eligible_at"),
                client_id=client_id,
            )
            return {
                "skipped": "eligibility",
                "channel": channel_norm,
                "status": "blocked",
                "communication_id": blocked.id,
                "code": blocked.code,
                "restriction_reason": blocked.restriction_reason,
            }

        now = _utcnow()
        date_bucket, date_label, time_label = _date_meta(now)
        code = f"COM-{uuid4().hex[:10].upper()}"
        email_provider = active_collection_provider()
        tracking = (
            "not_available"
            if channel_norm == "SMS" or email_provider in {"smtp", "log"}
            else "available"
        )
        client = account.client
        if client is None and account.client_id:
            client = self.db.get(PayflowClientModel, account.client_id)
        brand_from = (client.email_from if client else None) or None
        brand_name = None
        if client:
            brand_name = client.sender_name or client.brand_name or client.name
        comm = PayflowCommunicationModel(
            code=code,
            client_id=client_id or account.client_id,
            account_id=account.id,
            channel=channel_norm,
            purpose=purpose_norm,
            status=PayflowCommStatus.PREPARED.value,
            workflow_name=strategy.name,
            engagement=None,
            engagement_tracking=tracking,
            date_bucket=date_bucket,
            date_label=date_label,
            time_label=time_label,
            subject=subject if channel_norm != "SMS" else None,
            body_lines=body.splitlines()[:40],
            payment_link=bool(payment_link),
            why_message="Workflow communication step",
            why_channel=channel_norm,
            why_timing="Scheduled by workflow worker",
            events=[],
            balance=float(account.outstanding_balance or 0),
            eligibility_status="Eligible",
            provider=(
                "twilio"
                if channel_norm == "SMS"
                else email_provider
            ),
        )
        self.db.add(comm)
        self.db.flush()
        self._record_event_row(comm, event_type="prepared", label="Communication Prepared")

        # Opaque tokenized payment URL (reuse Active link; never account_reference).
        action_url: str | None = None
        if payment_link:
            action_url = self._ensure_payment_url(account, communication_id=comm.id)
            if action_url and channel_norm == "SMS" and action_url not in body:
                body = f"{body.rstrip()}\nPay: {action_url}"
                comm.body_lines = body.splitlines()[:40]

        send_status = "logged"
        recipient = ""
        provider_error: str | None = None
        provider_message_id: str | None = None

        custom_args = {
            "communication_id": str(comm.id),
            "communication_code": code,
        }

        if channel_norm == "SMS":
            phone = normalize_e164(account.phone_mobile or account.phone_work) or (
                account.phone_mobile or account.phone_work or ""
            ).strip()
            recipient = phone
            sms = send_collection_sms(
                self.db,
                to_phone=phone,
                body=truncate_sms_body(body),
                sms_type="collection_workflow",
                account_id=account.id,
                run_id=run_id,
                client_id=client_id or account.client_id,
                commit=False,
            )
            send_status = sms.status
            provider_message_id = getattr(sms, "provider_id", None)
            account.last_sms_sent_date = now.date()
            if sms.status == "failed":
                provider_error = sms.error or "failed"
        else:
            email = (account.email or "").strip()
            recipient = email
            # Prefer client branding on the wire; fall back to global SENDGRID_/SMTP_FROM.
            rendered_body = body
            if brand_name and "{{brand_name}}" not in body:
                # Already rendered templates may include brand; leave body as-is.
                pass
            log = send_collection_email(
                self.db,
                to_email=email,
                subject=subject,
                body=rendered_body,
                email_type="collection_workflow",
                action_link=action_url,
                custom_args=custom_args if email_provider == "sendgrid" else None,
                from_email=brand_from,
                from_name=brand_name,
                commit=False,
            )
            send_status = log.status
            provider_message_id = getattr(log, "provider_message_id", None)
            provider_name = getattr(log, "provider_name", None) or email_provider
            comm.provider = provider_name
            account.last_email_sent_date = now.date()
            if log.status == "failed":
                provider_error = getattr(log, "error", None) or f"{provider_name}_failed"

        account.last_contact_date = now.date()
        comm.provider_message_id = provider_message_id
        comm.recipient = recipient
        comm.provider_error = provider_error
        if send_status == "queued":
            comm.status = PayflowCommStatus.QUEUED.value
            comm.sent_at = now
            self._record_event_row(
                comm,
                event_type="queued",
                label=f"{channel_norm} Queued",
                detail=f"recipient={recipient}",
            )
        elif send_status in {"sent", "logged"}:
            comm.status = PayflowCommStatus.SENT.value
            comm.sent_at = now
            self._record_event_row(
                comm,
                event_type="sent",
                label=f"{channel_norm} Sent" if send_status == "sent" else f"{channel_norm} Logged",
                detail=f"recipient={recipient}",
            )
        else:
            comm.status = PayflowCommStatus.FAILED.value
            self._record_event_row(
                comm,
                event_type="failed",
                label=f"{channel_norm} Failed",
                detail=provider_error,
            )
            try:
                from app.modules.payflow.services.operational_exception_service import (
                    PayflowOperationalExceptionService,
                )
                from app.shared.enums import PayflowOperationalExceptionType

                PayflowOperationalExceptionService(self.db).raise_exception(
                    exception_type=PayflowOperationalExceptionType.COMMUNICATION_FAILURE.value,
                    reason=provider_error or f"{channel_norm} send failed",
                    client_id=comm.client_id,
                    account_id=comm.account_id,
                    source_entity_type="communication",
                    source_entity_id=comm.id,
                    severity="High",
                    retriable=True,
                    link=f"/payflow/comms/{comm.id}",
                    notify=True,
                    commit=False,
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to raise communication exception on send")

        return {
            "skipped": None,
            "channel": channel_norm,
            "status": send_status,
            "comm_status": comm.status,
            "recipient": recipient,
            "communication_id": comm.id,
            "provider_error": provider_error,
            "provider_message_id": provider_message_id,
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
        self._advance_status(row, status)
        self._record_event_row(
            row,
            event_type=status.lower().replace(" ", "_").replace("/", "_"),
            label=event_label or f"Status → {status}",
            detail=f"{previous} → {row.status}",
        )
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
                communication_id, row.status, commit=commit
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "workflow advance failed for communication_id=%s", communication_id
            )
        return row

    def apply_provider_event(
        self,
        *,
        event_type: str,
        occurred_at: datetime | None = None,
        communication_id: int | None = None,
        communication_code: str | None = None,
        provider_message_id: str | None = None,
        provider_event_id: str | None = None,
        url: str | None = None,
        payload: dict | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        row: PayflowCommunicationModel | None = None
        if communication_id:
            row = self.db.get(PayflowCommunicationModel, communication_id)
        if row is None and communication_code:
            row = (
                self.db.query(PayflowCommunicationModel)
                .filter(PayflowCommunicationModel.code == communication_code)
                .first()
            )
        if row is None and provider_message_id:
            row = (
                self.db.query(PayflowCommunicationModel)
                .filter(PayflowCommunicationModel.provider_message_id == provider_message_id)
                .first()
            )
        if row is None:
            return {"matched": False, "reason": "communication_not_found"}

        when = occurred_at or _utcnow()
        et = (event_type or "").lower().strip()
        recorded = self._record_event_row(
            row,
            event_type=et,
            occurred_at=when,
            detail=url,
            provider_event_id=provider_event_id,
            payload=payload,
            label=et.replace("_", " ").title(),
        )
        if not recorded:
            return {"matched": True, "duplicate": True, "communication_id": row.id}

        payment_base = (settings.WORKFLOW_PAYMENT_LINK_BASE or "").rstrip("/")
        is_payment_click = bool(url and payment_base and payment_base in url)

        fail_detail = None
        if isinstance(payload, dict):
            fail_detail = (
                payload.get("ErrorMessage")
                or payload.get("error_message")
                or payload.get("reason")
                or payload.get("ErrorCode")
            )
            if fail_detail is not None:
                fail_detail = str(fail_detail)[:500]

        if et in {"queued", "accepted"}:
            self._advance_status(row, PayflowCommStatus.QUEUED.value)
            row.sent_at = row.sent_at or when
        elif et in {"processed", "sending", "sent"}:
            self._advance_status(row, PayflowCommStatus.SENT.value)
            row.sent_at = row.sent_at or when
        elif et in {"delivered"}:
            self._advance_status(row, PayflowCommStatus.DELIVERED.value)
            row.delivered_at = row.delivered_at or when
        elif et in {"open", "opened"}:
            row.opens_count = int(row.opens_count or 0) + 1
            row.first_opened_at = row.first_opened_at or when
            row.last_opened_at = when
            self._advance_status(row, PayflowCommStatus.OPENED_READ.value)
        elif et in {"click", "clicked"}:
            row.clicks_count = int(row.clicks_count or 0) + 1
            row.first_clicked_at = row.first_clicked_at or when
            row.last_clicked_at = when
            if url:
                row.last_clicked_url = url[:1024]
            if is_payment_click or row.payment_link:
                self._advance_status(row, PayflowCommStatus.PAYMENT_LINK_CLICKED.value)
                try:
                    from app.modules.payflow.services.payment_link_service import (
                        PayflowPaymentLinkService,
                    )

                    PayflowPaymentLinkService(self.db).record_click_by_url(url, commit=False)
                except Exception:  # noqa: BLE001
                    logger.exception("payment link click record failed")
            else:
                self._advance_status(row, PayflowCommStatus.OPENED_READ.value)
        elif et in {
            "bounce",
            "bounced",
            "dropped",
            "failed",
            "undelivered",
            "canceled",
            "cancelled",
        }:
            self._advance_status(row, PayflowCommStatus.FAILED.value)
            row.provider_error = fail_detail or row.provider_error or f"Provider {et}"
            row.restriction_reason = row.restriction_reason or row.provider_error
            try:
                from app.modules.payflow.services.operational_exception_service import (
                    PayflowOperationalExceptionService,
                )
                from app.shared.enums import PayflowOperationalExceptionType

                PayflowOperationalExceptionService(self.db).raise_exception(
                    exception_type=PayflowOperationalExceptionType.COMMUNICATION_FAILURE.value,
                    reason=row.provider_error or f"Communication {et}",
                    client_id=row.client_id,
                    account_id=row.account_id,
                    source_entity_type="communication",
                    source_entity_id=row.id,
                    severity="High",
                    retriable=et in {"deferred", "undelivered"},
                    link=f"/payflow/comms/{row.id}",
                    notify=True,
                    commit=False,
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to raise communication exception")
        elif et in {"spamreport", "spam", "unsubscribe", "group_unsubscribe"}:
            self._advance_status(row, PayflowCommStatus.SUPPRESSED.value)
            row.eligibility_status = "Blocked"
            row.restriction_reason = f"Provider event: {et}"
        elif et in {"deferred"}:
            self._append_json_event(row, "Delivery Deferred", "Temporary delay from provider")

        if commit:
            self.db.commit()
        else:
            self.db.flush()
        try:
            from app.modules.payflow.services.workflow_execution_service import (
                WorkflowExecutionService,
            )

            WorkflowExecutionService(self.db).on_communication_event(
                row.id, row.status, commit=commit
            )
        except Exception:  # noqa: BLE001
            logger.exception("workflow advance failed for communication_id=%s", row.id)
        return {"matched": True, "duplicate": False, "communication_id": row.id, "status": row.status}

    def mark_payment_event(
        self,
        account_id: int,
        *,
        event: str,
        commit: bool = True,
    ) -> None:
        """Attach payment initiated/completed to the latest payment-link communication."""
        row = (
            self.db.query(PayflowCommunicationModel)
            .filter(
                PayflowCommunicationModel.account_id == account_id,
                PayflowCommunicationModel.payment_link.is_(True),
            )
            .order_by(PayflowCommunicationModel.id.desc())
            .first()
        )
        if not row:
            return
        if event == "initiated":
            if row.payment_initiated:
                # Idempotent — do not append a second "Payment Initiated" timeline row.
                if commit:
                    self.db.commit()
                else:
                    self.db.flush()
                return
            row.payment_initiated = True
            self._record_event_row(row, event_type="payment_initiated", label="Payment Initiated")
        elif event == "completed":
            if row.payment_completed:
                # Idempotent — prevents duplicate "Payment Completed" from Stripe + workflow paths.
                if commit:
                    self.db.commit()
                else:
                    self.db.flush()
                return
            row.payment_initiated = True
            row.payment_completed = True
            self._record_event_row(row, event_type="payment_completed", label="Payment Completed")
            # Do NOT auto-complete all payment links here — partial / plan first
            # instalments must keep Active links. Full settlement completes links
            # in payment_portal_service.apply_succeeded when balance hits zero.
        if commit:
            self.db.commit()
        else:
            self.db.flush()

    def mark_sms_opt_out_by_phone(self, from_phone: str, *, commit: bool = True) -> int:
        """Set sms_opt_out on accounts matching inbound From number. Returns count updated."""
        normalized = normalize_e164(from_phone)
        if not normalized:
            return 0
        digits = re.sub(r"\D+", "", normalized)
        candidates = (
            self.db.query(PayflowAccountModel)
            .filter(
                (PayflowAccountModel.phone_mobile.isnot(None))
                | (PayflowAccountModel.phone_work.isnot(None))
            )
            .all()
        )
        updated = 0
        for account in candidates:
            for raw in (account.phone_mobile, account.phone_work):
                other = normalize_e164(raw)
                other_digits = re.sub(r"\D+", "", other or "") if other else ""
                if other == normalized or (other_digits and other_digits == digits):
                    if not account.sms_opt_out:
                        account.sms_opt_out = True
                        updated += 1
                    break
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return updated

    def send_manual_sms(
        self,
        user: UserModel,
        account: PayflowAccountModel,
        *,
        body: str,
        purpose: str = "Payment Reminder",
        include_payment_link: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Authorized manual SMS — same eligibility gates as workflow send."""
        if not self.access.is_operations_admin(user) and not self.access.can(
            user, "send_communications", account.client_id
        ):
            raise ForbiddenError("Send Communications permission required")
        self._assert_view(user, account.client_id)

        purpose_norm = (purpose or "Payment Reminder").strip()
        if idempotency_key:
            existing = (
                self.db.query(PayflowCommunicationModel)
                .filter(
                    PayflowCommunicationModel.account_id == account.id,
                    PayflowCommunicationModel.channel == "SMS",
                    PayflowCommunicationModel.why_timing == f"manual:{idempotency_key}",
                )
                .first()
            )
            if existing:
                return {
                    "skipped": "idempotent",
                    "communication_id": existing.id,
                    "code": existing.code,
                    "comm_status": existing.status,
                }

        recent = (
            self.db.query(PayflowCommunicationModel)
            .filter(
                PayflowCommunicationModel.account_id == account.id,
                PayflowCommunicationModel.channel == "SMS",
                PayflowCommunicationModel.purpose == purpose_norm,
                PayflowCommunicationModel.status.in_(
                    [
                        PayflowCommStatus.QUEUED.value,
                        PayflowCommStatus.SENT.value,
                        PayflowCommStatus.PREPARED.value,
                    ]
                ),
                PayflowCommunicationModel.created_at >= _utcnow() - timedelta(minutes=5),
            )
            .order_by(PayflowCommunicationModel.id.desc())
            .first()
        )
        if recent and not idempotency_key:
            return {
                "skipped": "duplicate_window",
                "communication_id": recent.id,
                "code": recent.code,
                "comm_status": recent.status,
                "restriction_reason": "An SMS for this purpose was already sent in the last 5 minutes",
            }

        # Truthy flag → send_workflow_step ensures a secure Active tokenized link.
        payment_url = "1" if include_payment_link else None

        strategy = SimpleNamespace(name="Manual SMS")

        result = self.send_workflow_step(
            account=account,
            strategy=strategy,  # type: ignore[arg-type]
            channel="SMS",
            purpose=purpose_norm,
            subject="",
            body=body,
            payment_link=payment_url,
            run_id=None,
            client_id=account.client_id,
        )
        if result.get("communication_id"):
            row = self.db.get(PayflowCommunicationModel, result["communication_id"])
            if row:
                row.why_message = "Authorized manual SMS"
                row.why_timing = (
                    f"manual:{idempotency_key}" if idempotency_key else "Manual send by authorized user"
                )
                row.workflow_name = "Manual SMS"
                if result.get("skipped") == "eligibility":
                    pass
                else:
                    self.db.flush()
        self.db.commit()
        self._notify_manual_comm_sent(account, result, channel="SMS")
        return result

    def send_manual_email(
        self,
        user: UserModel,
        account: PayflowAccountModel,
        *,
        subject: str,
        body: str,
        purpose: str = "Payment Reminder",
        include_payment_link: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Authorized manual email — same eligibility gates as workflow send."""
        if not self.access.is_operations_admin(user) and not self.access.can(
            user, "send_communications", account.client_id
        ):
            raise ForbiddenError("Send Communications permission required")
        self._assert_view(user, account.client_id)

        purpose_norm = (purpose or "Payment Reminder").strip()
        subject_norm = (subject or "Payment reminder").strip()[:500]
        if idempotency_key:
            existing = (
                self.db.query(PayflowCommunicationModel)
                .filter(
                    PayflowCommunicationModel.account_id == account.id,
                    PayflowCommunicationModel.channel == "Email",
                    PayflowCommunicationModel.why_timing == f"manual:{idempotency_key}",
                )
                .first()
            )
            if existing:
                return {
                    "skipped": "idempotent",
                    "communication_id": existing.id,
                    "code": existing.code,
                    "comm_status": existing.status,
                }

        recent = (
            self.db.query(PayflowCommunicationModel)
            .filter(
                PayflowCommunicationModel.account_id == account.id,
                PayflowCommunicationModel.channel == "Email",
                PayflowCommunicationModel.purpose == purpose_norm,
                PayflowCommunicationModel.status.in_(
                    [
                        PayflowCommStatus.QUEUED.value,
                        PayflowCommStatus.SENT.value,
                        PayflowCommStatus.PREPARED.value,
                        PayflowCommStatus.DELIVERED.value,
                    ]
                ),
                PayflowCommunicationModel.created_at >= _utcnow() - timedelta(minutes=5),
            )
            .order_by(PayflowCommunicationModel.id.desc())
            .first()
        )
        if recent and not idempotency_key:
            return {
                "skipped": "duplicate_window",
                "communication_id": recent.id,
                "code": recent.code,
                "comm_status": recent.status,
                "restriction_reason": "An email for this purpose was already sent in the last 5 minutes",
            }

        payment_url = "1" if include_payment_link else None
        strategy = SimpleNamespace(name="Manual Email")
        result = self.send_workflow_step(
            account=account,
            strategy=strategy,  # type: ignore[arg-type]
            channel="Email",
            purpose=purpose_norm,
            subject=subject_norm,
            body=body,
            payment_link=payment_url,
            run_id=None,
            client_id=account.client_id,
        )
        if result.get("communication_id"):
            row = self.db.get(PayflowCommunicationModel, result["communication_id"])
            if row:
                row.why_message = "Authorized manual email"
                row.why_timing = (
                    f"manual:{idempotency_key}" if idempotency_key else "Manual send by authorized user"
                )
                row.workflow_name = "Manual Email"
                self.db.flush()
        self.db.commit()
        self._notify_manual_comm_sent(account, result, channel="Email")
        return result

    def _notify_manual_comm_sent(
        self, account: PayflowAccountModel, result: dict[str, Any], *, channel: str
    ) -> None:
        if result.get("skipped") or result.get("status") in {"blocked", "failed"}:
            return
        if not result.get("communication_id"):
            return
        try:
            from app.modules.payflow.services.notification_service import (
                PayflowNotificationService,
            )

            PayflowNotificationService(self.db).notify_comm_sent(
                client_id=account.client_id,
                account_id=account.id,
                customer_name=account.customer_name or "Customer",
                account_reference=account.account_reference or str(account.id),
                channel=channel,
                recipient=result.get("recipient"),
                communication_id=result.get("communication_id"),
                communication_code=result.get("code"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("manual %s sent notify failed account_id=%s", channel, account.id)

    def resend_failed_communication(
        self,
        user: UserModel,
        communication_id: int,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Safe retry for a Failed email/SMS — creates a new send; never duplicates a non-failed row."""
        row = (
            self.db.query(PayflowCommunicationModel)
            .options(
                joinedload(PayflowCommunicationModel.account).joinedload(PayflowAccountModel.client),
                joinedload(PayflowCommunicationModel.client),
            )
            .filter(PayflowCommunicationModel.id == communication_id)
            .first()
        )
        if not row:
            raise NotFoundError("Communication not found")
        self._assert_view(user, row.client_id)
        if not self.access.is_operations_admin(user) and not self.access.can(
            user, "send_communications", row.client_id
        ):
            raise ForbiddenError("Send Communications permission required")
        if row.status != PayflowCommStatus.FAILED.value:
            raise ValidationAppError("Only failed communications can be resent")
        account = row.account
        if not account:
            raise NotFoundError("Account not found for communication")

        body = "\n".join(row.body_lines or []) or "Payment reminder"
        key = idempotency_key or f"resend-{row.id}"
        if row.channel == "SMS":
            return self.send_manual_sms(
                user,
                account,
                body=body,
                purpose=row.purpose or "Payment Reminder",
                include_payment_link=bool(row.payment_link),
                idempotency_key=key,
            )
        return self.send_manual_email(
            user,
            account,
            subject=row.subject or "Payment reminder",
            body=body,
            purpose=row.purpose or "Payment Reminder",
            include_payment_link=bool(row.payment_link),
            idempotency_key=key,
        )

    def case_eligibility_snapshot(self, user: UserModel, account: PayflowAccountModel) -> dict[str, Any]:
        self._assert_view(user, account.client_id)
        email_gate = self.evaluate_send_eligibility(account, channel="Email")
        sms_gate = self.evaluate_send_eligibility(account, channel="SMS")
        latest_blocked = (
            self.db.query(PayflowCommunicationModel)
            .filter(
                PayflowCommunicationModel.account_id == account.id,
                PayflowCommunicationModel.eligibility_status.in_(["Blocked", "Deferred", "Failed"]),
            )
            .order_by(PayflowCommunicationModel.id.desc())
            .first()
        )
        can_view_rule = False
        if latest_blocked and latest_blocked.rule_id:
            can_view_rule = self.access.is_operations_admin(user) or self.access.can(
                user, "view_rules", account.client_id
            )
        return {
            "email": email_gate,
            "sms": sms_gate,
            "latest_restriction_reason": latest_blocked.restriction_reason if latest_blocked else None,
            "latest_eligibility_status": latest_blocked.eligibility_status if latest_blocked else None,
            "next_eligible_at": latest_blocked.next_eligible_at if latest_blocked else None,
            "rule_id": latest_blocked.rule_id if latest_blocked and can_view_rule else None,
            "rule_name": latest_blocked.rule_name if latest_blocked and can_view_rule else None,
            "rule_code": latest_blocked.rule_code if latest_blocked and can_view_rule else None,
            "can_view_rule": can_view_rule,
            "communication_id": latest_blocked.id if latest_blocked else None,
        }

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
            "total": 0,
            "limit": 100,
            "offset": 0,
        }
