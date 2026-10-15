from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowCommunicationModel,
    PayflowHumanReviewAuditModel,
    PayflowHumanReviewModel,
    PayflowRuleModel,
    PayflowStrategyModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import (
    PayflowCollectionStatus,
    PayflowReviewPriority,
    PayflowReviewStatus,
)

PROPOSED_ACTIONS = [
    "Move to stronger collection treatment",
    "Change communication strategy",
    "Modify payment treatment",
    "Send settlement offer",
    "Continue current treatment with adjusted communication",
    "Hold collection activity pending dispute review",
    "Pause outreach and request updated contact details",
]

REVIEW_REASONS = [
    "Repeated unsuccessful attempts",
    "Low decision confidence",
    "Payment arrangement exception",
    "High balance treatment",
    "Customer dispute raised",
    "Hardship signal detected",
]

REJECTION_REASONS = [
    "Not appropriate for customer context",
    "Insufficient information",
    "Incorrect treatment",
    "Client policy consideration",
    "Other",
]

WAITING_BUCKETS = [
    "Any Age",
    "Under 30 min",
    "30 min – 2 hours",
    "2 – 24 hours",
    "Over 24 hours",
]

OPEN_STATUSES = {
    PayflowReviewStatus.AWAITING_REVIEW.value,
    PayflowReviewStatus.ON_HOLD.value,
}

PRIORITY_RANK = {
    PayflowReviewPriority.HIGH.value: 3,
    PayflowReviewPriority.MEDIUM.value: 2,
    PayflowReviewPriority.NORMAL.value: 1,
}

SORT_FIELDS = {
    "waiting_minutes",
    "priority",
    "created_at",
    "status",
    "outstanding",
    "customer",
}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%d %b · %H:%M")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _format_waiting(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 1440:
        h = minutes // 60
        m = minutes % 60
        return f"{h}h" if m == 0 else f"{h}h {m}m"
    d = minutes // 1440
    h = (minutes % 1440) // 60
    return f"{d}d" if h == 0 else f"{d}d {h}h"


def _in_waiting_bucket(minutes: int, bucket: str) -> bool:
    if bucket == "Under 30 min":
        return minutes < 30
    if bucket == "30 min – 2 hours":
        return 30 <= minutes < 120
    if bucket == "2 – 24 hours":
        return 120 <= minutes < 1440
    if bucket == "Over 24 hours":
        return minutes >= 1440
    return True


def _as_uuid(value: str | UUID | None) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationAppError("Invalid user id") from exc


class PayflowReviewService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    # ------------------------------------------------------------------ access
    def _visible_client_ids(self, user: UserModel) -> list[int] | None:
        if self.access.is_operations_admin(user):
            return None
        ctx = self.access.build_context(user)
        return list(ctx.get("client_ids") or [])

    def _assert_view(self, user: UserModel, client_id: int) -> None:
        if self.access.is_operations_admin(user):
            return
        if not self.access.can_see_client(user, client_id):
            raise ForbiddenError("Review access denied")
        if not self.access.can(user, "view_human_reviews", client_id):
            raise ForbiddenError("Review access denied")

    def can_decide(self, user: UserModel, client_id: int) -> bool:
        if self.access.is_operations_admin(user):
            return True
        return self.access.can(user, "approve_human_reviews", client_id)

    def can_modify(self, user: UserModel, client_id: int) -> bool:
        if self.access.is_operations_admin(user):
            return True
        return self.access.can(user, "modify_guide_ai_recommendation", client_id)

    def can_release(self, user: UserModel, client_id: int) -> bool:
        return self.can_decide(user, client_id)

    # ------------------------------------------------------------------ helpers
    def _waiting_minutes(self, row: PayflowHumanReviewModel) -> int:
        created = row.created_at
        if not created:
            return int(row.waiting_minutes or 0)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0, int((_utcnow() - created).total_seconds() // 60))

    def _sync_waiting(self, row: PayflowHumanReviewModel) -> int:
        minutes = self._waiting_minutes(row)
        row.waiting_minutes = minutes
        return minutes

    def _catalogs(self) -> dict[str, Any]:
        return {
            "statuses": [s.value for s in PayflowReviewStatus],
            "priorities": [p.value for p in PayflowReviewPriority],
            "reasons": REVIEW_REASONS,
            "proposed_actions": PROPOSED_ACTIONS,
            "rejection_reasons": REJECTION_REASONS,
            "waiting_buckets": WAITING_BUCKETS,
        }

    def _empty_list(
        self, *, page: int = 1, page_size: int = 50, sort_by: str | None = None, sort_dir: str | None = None
    ) -> dict[str, Any]:
        return {
            "reviews": [],
            "summary": {"awaiting": 0, "high_priority": 0, "due_today": 0, "on_hold": 0},
            "page": page,
            "page_size": page_size,
            "total": 0,
            "total_pages": 0,
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            **self._catalogs(),
        }

    def _next_code(self) -> str:
        latest = (
            self.db.query(PayflowHumanReviewModel)
            .order_by(PayflowHumanReviewModel.id.desc())
            .first()
        )
        n = (latest.id + 1) if latest else 1001
        return f"rev-{n}"

    def _snapshot(self, row: PayflowHumanReviewModel) -> dict[str, Any]:
        return {
            "status": row.status,
            "proposed_action": row.proposed_action,
            "final_action": row.final_action,
            "guidance": row.guidance,
            "rejection_reason": row.rejection_reason,
            "hold_until": row.hold_until,
            "assigned_supervisor": row.assigned_supervisor,
            "priority": row.priority,
            "decision_version": row.decision_version or 1,
        }

    def _append_history(self, row: PayflowHumanReviewModel, entries: list[dict]) -> None:
        history = list(row.history or [])
        history.extend(entries)
        row.history = history

    def _record_audit(
        self,
        row: PayflowHumanReviewModel,
        *,
        event: str,
        detail: str | None = None,
        actor: UserModel | None = None,
        actor_name: str | None = None,
        before_status: str | None = None,
        after_status: str | None = None,
        before_values: dict | None = None,
        after_values: dict | None = None,
    ) -> None:
        self.db.add(
            PayflowHumanReviewAuditModel(
                review_id=row.id,
                event=event,
                detail=detail,
                actor_user_id=actor.id if actor else None,
                actor_name=(actor.full_name if actor else None) or actor_name,
                before_status=before_status,
                after_status=after_status,
                before_values=before_values,
                after_values=after_values,
            )
        )

    def _bump_version(self, row: PayflowHumanReviewModel) -> None:
        row.decision_version = int(row.decision_version or 1) + 1

    def _build_default_context(self, account: PayflowAccountModel) -> list[dict[str, str]]:
        ptp = "None Active"
        if account.ptp_code or account.ptp_due_date:
            due = account.ptp_due_date.isoformat() if account.ptp_due_date else "open"
            ptp = f"Active · due {due}"
        last_comm = "—"
        if account.last_sms_sent_date:
            last_comm = f"SMS · {account.last_sms_sent_date.isoformat()}"
        elif account.last_email_sent_date:
            last_comm = f"Email · {account.last_email_sent_date.isoformat()}"
        elif account.last_contact_date:
            last_comm = account.last_contact_date.isoformat()
        payment_status = "Unpaid"
        if float(account.recovered_balance or 0) > 0 and float(account.outstanding_balance or 0) > 0:
            payment_status = "Partially Paid"
        elif float(account.outstanding_balance or 0) <= 0:
            payment_status = "Paid"
        return [
            {"label": "Previous Attempts", "value": "—"},
            {"label": "Last Communication", "value": last_comm},
            {"label": "Last Customer Engagement", "value": "—"},
            {"label": "Payment Status", "value": payment_status},
            {"label": "Promise-to-Pay", "value": ptp},
        ]

    def _resolve_strategy(
        self, account: PayflowAccountModel, strategy_id: int | None
    ) -> PayflowStrategyModel | None:
        if strategy_id:
            return (
                self.db.query(PayflowStrategyModel)
                .filter(
                    PayflowStrategyModel.id == strategy_id,
                    PayflowStrategyModel.client_id == account.client_id,
                )
                .first()
            )
        if not account.current_workflow:
            return None
        return (
            self.db.query(PayflowStrategyModel)
            .filter(
                PayflowStrategyModel.client_id == account.client_id,
                or_(
                    PayflowStrategyModel.name == account.current_workflow,
                    PayflowStrategyModel.code == account.current_workflow,
                ),
            )
            .order_by(PayflowStrategyModel.updated_at.desc())
            .first()
        )

    def _block_account_for_review(
        self, account: PayflowAccountModel, *, proposed_action: str, stamp: str
    ) -> None:
        account.human_review = True
        account.collection_status = PayflowCollectionStatus.HUMAN_REVIEW.value
        account.next_action = "Awaiting human review decision"
        account.last_action = "Human review required"
        events = list(account.timeline or [])
        events.append(
            {
                "label": "Human review created",
                "detail": proposed_action,
                "at": stamp,
            }
        )
        account.timeline = events[-40:]

    def _apply_case_decision(
        self,
        row: PayflowHumanReviewModel,
        *,
        decision: str,
        final_action: str,
        actor_name: str,
    ) -> None:
        account = (
            self.db.query(PayflowAccountModel)
            .filter(PayflowAccountModel.id == row.account_id)
            .with_for_update()
            .first()
        )
        if not account:
            return
        stamp = _stamp()
        if decision == "hold":
            account.human_review = True
            account.collection_status = PayflowCollectionStatus.HUMAN_REVIEW.value
            account.next_action = "Collection activity paused"
            account.last_action = f"Human review held by {actor_name}"
        elif decision == "reject":
            account.human_review = False
            if account.collection_status == PayflowCollectionStatus.HUMAN_REVIEW.value:
                account.collection_status = PayflowCollectionStatus.ACTIVE.value
            account.next_action = "Reassess collection treatment"
            account.last_action = f"Human review rejected by {actor_name}"
        else:
            # approve / modify — case-level treatment only (never mutates strategy definition)
            account.human_review = False
            if account.collection_status == PayflowCollectionStatus.HUMAN_REVIEW.value:
                account.collection_status = PayflowCollectionStatus.ACTIVE.value
            account.next_action = final_action
            account.last_action = f"Human review {decision} by {actor_name}"
        events = list(account.timeline or [])
        events.append(
            {
                "label": f"Human review {decision}",
                "detail": final_action,
                "at": stamp,
                "review_id": row.id,
            }
        )
        account.timeline = events[-40:]

    def _available_actions(self, row: PayflowHumanReviewModel, user: UserModel) -> list[str]:
        actions: list[str] = []
        if row.status == PayflowReviewStatus.AWAITING_REVIEW.value and self.can_decide(
            user, row.client_id
        ):
            actions.extend(["approve", "reject", "hold"])
            if self.can_modify(user, row.client_id):
                actions.append("modify")
        if row.status == PayflowReviewStatus.ON_HOLD.value and self.can_release(
            user, row.client_id
        ):
            actions.append("release")
        return actions

    def _load_communications(self, account_id: int, limit: int = 8) -> list[dict[str, Any]]:
        rows = (
            self.db.query(PayflowCommunicationModel)
            .filter(PayflowCommunicationModel.account_id == account_id)
            .order_by(PayflowCommunicationModel.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": c.id,
                "code": c.code,
                "channel": c.channel,
                "purpose": c.purpose,
                "status": c.status,
                "engagement": c.engagement,
                "date_label": c.date_label,
                "time_label": c.time_label,
                "subject": c.subject,
            }
            for c in rows
        ]

    def _payment_context(self, account: PayflowAccountModel | None) -> dict[str, Any] | None:
        if not account:
            return None
        return {
            "last_payment_amount": float(account.last_payment_amount)
            if account.last_payment_amount is not None
            else None,
            "last_payment_date": account.last_payment_date.isoformat()
            if account.last_payment_date
            else None,
            "last_payment_is_ptp": account.last_payment_is_ptp,
            "ptp_code": account.ptp_code,
            "ptp_amount": float(account.ptp_amount) if account.ptp_amount is not None else None,
            "ptp_due_date": account.ptp_due_date.isoformat() if account.ptp_due_date else None,
            "paid_amount": float(account.paid_amount) if account.paid_amount is not None else None,
        }

    def _workflow_context(self, row: PayflowHumanReviewModel) -> dict[str, Any]:
        account = row.account
        strategy = row.strategy
        if strategy is None and row.strategy_id:
            strategy = self.db.get(PayflowStrategyModel, row.strategy_id)
        return {
            "strategy_id": strategy.id if strategy else row.strategy_id,
            "strategy_name": strategy.name if strategy else None,
            "strategy_code": strategy.code if strategy else None,
            "workflow_version": row.workflow_version
            if row.workflow_version is not None
            else (strategy.version if strategy else None),
            "exception_step_id": row.exception_step_id,
            "current_workflow": account.current_workflow if account else None,
            "collection_status": account.collection_status if account else None,
            "last_action": account.last_action if account else None,
            "next_action": account.next_action if account else None,
            "action_blocked": bool(account.human_review) if account else False,
        }

    def _serialize(
        self,
        row: PayflowHumanReviewModel,
        user: UserModel | None = None,
        *,
        include_detail: bool = False,
    ) -> dict[str, Any]:
        account = row.account
        client = row.client
        rule = row.rule
        waiting = self._waiting_minutes(row)
        data: dict[str, Any] = {
            "id": row.id,
            "code": row.code,
            "client_id": row.client_id,
            "client_code": client.code if client else None,
            "client_name": client.name if client else None,
            "account_id": row.account_id,
            "customer_name": account.customer_name if account else None,
            "account_reference": account.account_reference if account else None,
            "case_reference": account.case_reference if account else None,
            "original_balance": float(account.original_balance) if account else 0,
            "outstanding_balance": float(account.outstanding_balance) if account else 0,
            "recovered_balance": float(account.recovered_balance) if account else 0,
            "days_past_due": int(
                row.days_past_due
                if row.days_past_due is not None
                else ((account.days_past_due or 0) if account else 0)
            ),
            "current_workflow": account.current_workflow if account else None,
            "priority": row.priority,
            "reason": row.reason,
            "rule_id": row.rule_id,
            "rule_code": rule.code if rule else None,
            "rule_name": rule.name if rule else None,
            "condition_text": row.condition_text,
            "observed_value": row.observed_value,
            "proposed_action": row.proposed_action,
            "confidence": row.confidence,
            "explanation": list(row.explanation or []),
            "context": list(row.context or []),
            "timeline": list(row.timeline or []),
            "waiting_minutes": waiting,
            "waiting_label": _format_waiting(waiting),
            "status": row.status,
            "assigned_supervisor": row.assigned_supervisor,
            "assigned_user_id": str(row.assigned_user_id) if row.assigned_user_id else None,
            "decided_by_user_id": str(row.decided_by_user_id) if row.decided_by_user_id else None,
            "decided_at": row.decided_at,
            "final_action": row.final_action,
            "guidance": row.guidance,
            "rejection_reason": row.rejection_reason,
            "hold_until": row.hold_until,
            "history": list(row.history or []),
            "decision_version": row.decision_version or 1,
            "strategy_id": row.strategy_id,
            "workflow_version": row.workflow_version,
            "exception_step_id": row.exception_step_id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "audit_trail": [],
            "communications": [],
            "payment_context": None,
            "workflow_context": None,
            "available_actions": [],
        }
        if user is not None:
            data["can_decide"] = self.can_decide(user, row.client_id)
            data["can_modify"] = self.can_modify(user, row.client_id)
            data["can_release"] = self.can_release(user, row.client_id)
            data["available_actions"] = self._available_actions(row, user)
        if include_detail:
            data["payment_context"] = self._payment_context(account)
            data["workflow_context"] = self._workflow_context(row)
            data["communications"] = self._load_communications(row.account_id)
            audits = (
                self.db.query(PayflowHumanReviewAuditModel)
                .filter(PayflowHumanReviewAuditModel.review_id == row.id)
                .order_by(PayflowHumanReviewAuditModel.id.asc())
                .all()
            )
            data["audit_trail"] = [
                {
                    "id": a.id,
                    "event": a.event,
                    "detail": a.detail,
                    "actor_name": a.actor_name,
                    "before_status": a.before_status,
                    "after_status": a.after_status,
                    "before_values": a.before_values,
                    "after_values": a.after_values,
                    "created_at": a.created_at,
                }
                for a in audits
            ]
        return data

    def _base_query(self):
        return self.db.query(PayflowHumanReviewModel).options(
            joinedload(PayflowHumanReviewModel.client),
            joinedload(PayflowHumanReviewModel.account),
            joinedload(PayflowHumanReviewModel.rule),
            joinedload(PayflowHumanReviewModel.strategy),
        )

    def _get_row(self, review_id: int, *, for_update: bool = False) -> PayflowHumanReviewModel:
        if for_update:
            locked = (
                self.db.query(PayflowHumanReviewModel)
                .filter(PayflowHumanReviewModel.id == review_id)
                .with_for_update()
                .first()
            )
            if not locked:
                raise NotFoundError("Review not found")
            # Re-load with relationships after the row lock (joinedload + FOR UPDATE
            # is not portable across dialects).
            self.db.refresh(locked)
        row = (
            self._base_query()
            .filter(PayflowHumanReviewModel.id == review_id)
            .first()
        )
        if not row:
            raise NotFoundError("Review not found")
        return row

    def _assert_decision_version(
        self, row: PayflowHumanReviewModel, expected: int | None
    ) -> None:
        if expected is None:
            return
        current = int(row.decision_version or 1)
        if expected != current:
            raise ValidationAppError(
                "Review was updated by another user. Refresh and try again."
            )

    def _require_status(
        self,
        user: UserModel,
        review_id: int,
        *,
        allowed_statuses: set[str],
        decide: bool = False,
        modify: bool = False,
        release: bool = False,
        decision_version: int | None = None,
    ) -> PayflowHumanReviewModel:
        row = self._get_row(review_id, for_update=True)
        self._assert_view(user, row.client_id)
        if decide and not self.can_decide(user, row.client_id):
            raise ForbiddenError("Approve Human Reviews permission required")
        if modify and not self.can_modify(user, row.client_id):
            raise ForbiddenError("Modify / Guide AI Recommendation permission required")
        if release and not self.can_release(user, row.client_id):
            raise ForbiddenError("Approve Human Reviews permission required")
        if row.status not in allowed_statuses:
            raise ValidationAppError(
                f"Review cannot be processed from status '{row.status}'"
            )
        self._assert_decision_version(row, decision_version)
        return row

    def _resume_workflow_after_review(
        self, review_id: int, *, continue_workflow: bool
    ) -> None:
        try:
            from app.modules.payflow.services.workflow_execution_service import (
                WorkflowExecutionService,
            )

            WorkflowExecutionService(self.db).on_human_review_decided(
                review_id, continue_workflow=continue_workflow
            )
        except Exception:
            import logging

            logging.getLogger("app.review").exception(
                "Failed to resume workflow after review_id=%s", review_id
            )

    def _mark_review_notifications_read(self, review_id: int) -> None:
        try:
            from app.modules.payflow.services.notification_service import (
                PayflowNotificationService,
            )

            PayflowNotificationService(self.db).mark_entity_read(
                entity_type="human_review", entity_id=review_id
            )
        except Exception:
            pass

    def _notify_awaiting(self, row: PayflowHumanReviewModel) -> None:
        try:
            from app.modules.payflow.services.notification_service import (
                PayflowNotificationService,
            )

            PayflowNotificationService(self.db).notify_review_awaiting(row, send_mail=False)
        except Exception:
            pass

    def _notify_decided(self, row: PayflowHumanReviewModel, actor: UserModel) -> None:
        try:
            from app.modules.payflow.services.notification_service import (
                PayflowNotificationService,
            )
            from app.shared.enums import PayflowNotificationType

            svc = PayflowNotificationService(self.db)
            customer = row.account.customer_name if row.account else "Customer"
            users = svc.recipients_for_client(row.client_id)
            svc.notify(
                users=users,
                notification_type=PayflowNotificationType.REVIEW_DECIDED.value,
                title=f"{customer} · review {row.status.lower()}",
                body=row.final_action or row.status,
                link=f"/payflow/review/{row.id}",
                client_id=row.client_id,
                entity_type="human_review",
                entity_id=str(row.id),
                send_mail=False,
                email_type="review_decided",
                actor_user_id=actor.id,
            )
        except Exception:
            pass

    def _sort_rows(
        self, rows: list[PayflowHumanReviewModel], sort_by: str, sort_dir: str
    ) -> list[PayflowHumanReviewModel]:
        reverse = sort_dir == "desc"

        def key(r: PayflowHumanReviewModel):
            if sort_by == "priority":
                return PRIORITY_RANK.get(r.priority, 0)
            if sort_by == "created_at":
                return r.created_at or datetime.min.replace(tzinfo=timezone.utc)
            if sort_by == "status":
                return r.status or ""
            if sort_by == "outstanding":
                return float(r.account.outstanding_balance) if r.account else 0.0
            if sort_by == "customer":
                return (r.account.customer_name or "").lower() if r.account else ""
            return self._waiting_minutes(r)

        return sorted(rows, key=key, reverse=reverse)

    def _summary_for_rows(self, rows: list[PayflowHumanReviewModel]) -> dict[str, int]:
        awaiting = [
            r for r in rows if r.status == PayflowReviewStatus.AWAITING_REVIEW.value
        ]
        return {
            "awaiting": len(awaiting),
            "high_priority": sum(
                1 for r in awaiting if r.priority == PayflowReviewPriority.HIGH.value
            ),
            "due_today": sum(1 for r in awaiting if self._waiting_minutes(r) < 1440),
            "on_hold": sum(
                1 for r in rows if r.status == PayflowReviewStatus.ON_HOLD.value
            ),
        }

    def _filter_permission_rows(
        self, user: UserModel, rows: list[PayflowHumanReviewModel]
    ) -> list[PayflowHumanReviewModel]:
        if self.access.is_operations_admin(user):
            return rows
        return [
            r for r in rows if self.access.can(user, "view_human_reviews", r.client_id)
        ]

    # ------------------------------------------------------------------ create / lifecycle
    def create_review(
        self,
        *,
        account_id: int,
        reason: str,
        proposed_action: str,
        priority: str = PayflowReviewPriority.NORMAL.value,
        rule_id: int | None = None,
        condition_text: str | None = None,
        observed_value: str | None = None,
        confidence: float | None = None,
        explanation: list[str] | None = None,
        context: list[dict[str, str]] | None = None,
        strategy_id: int | None = None,
        workflow_version: int | None = None,
        exception_step_id: str | None = None,
        assigned_user_id: str | UUID | None = None,
        assigned_supervisor: str | None = None,
        actor: UserModel | None = None,
        actor_name: str | None = None,
        notify: bool = True,
        commit: bool = True,
    ) -> PayflowHumanReviewModel:
        reason = (reason or "").strip()
        proposed_action = (proposed_action or "").strip()
        if not reason:
            raise ValidationAppError("Review reason is required")
        if not proposed_action:
            raise ValidationAppError("Proposed action is required")
        if priority not in {p.value for p in PayflowReviewPriority}:
            raise ValidationAppError("Invalid review priority")

        account = (
            self.db.query(PayflowAccountModel)
            .filter(PayflowAccountModel.id == account_id)
            .with_for_update()
            .first()
        )
        if not account:
            raise NotFoundError("Account not found")
        # Ensure client relationship is available for notifications / serialization.
        _ = account.client

        open_q = self.db.query(PayflowHumanReviewModel).filter(
            PayflowHumanReviewModel.account_id == account_id,
            PayflowHumanReviewModel.status.in_(list(OPEN_STATUSES)),
        )
        if rule_id is not None:
            open_q = open_q.filter(PayflowHumanReviewModel.rule_id == rule_id)
        existing = open_q.order_by(PayflowHumanReviewModel.id.desc()).first()
        if existing:
            return existing

        rule = None
        if rule_id is not None:
            rule = self.db.query(PayflowRuleModel).filter(PayflowRuleModel.id == rule_id).first()
            if not rule:
                raise NotFoundError("Rule not found")
            if rule.client_id is not None and rule.client_id != account.client_id:
                raise ValidationAppError("Rule does not belong to this account's client")
            if rule.action and rule.action != "Require Human Review":
                # Only governance actions that require human intervention create reviews.
                raise ValidationAppError(
                    "Only rules with action 'Require Human Review' can create reviews"
                )

        strategy = self._resolve_strategy(account, strategy_id)
        assignee_id = _as_uuid(assigned_user_id)
        assignee_name = assigned_supervisor
        if assignee_id and not assignee_name:
            user = self.db.get(UserModel, assignee_id)
            if user:
                assignee_name = user.full_name

        stamp = _stamp()
        timeline = [
            {"at": stamp, "label": "Human Review Created"},
        ]
        if rule:
            timeline.insert(0, {"at": stamp, "label": f"{rule.name} Triggered"})

        history = [
            {
                "at": stamp,
                "event": "Human review created",
                "detail": rule.name if rule else reason,
                "by": (actor.full_name if actor else None) or actor_name,
            },
            {
                "at": stamp,
                "event": "PayFlow proposed action recorded",
                "detail": (
                    f"{proposed_action} · confidence {int(confidence)}%"
                    if confidence is not None
                    else proposed_action
                ),
            },
        ]
        if assignee_name:
            history.append(
                {
                    "at": stamp,
                    "event": "Assigned to supervisor",
                    "detail": assignee_name,
                }
            )

        row = PayflowHumanReviewModel(
            code=self._next_code(),
            client_id=account.client_id,
            account_id=account.id,
            rule_id=rule.id if rule else None,
            strategy_id=strategy.id if strategy else strategy_id,
            workflow_version=workflow_version
            if workflow_version is not None
            else (strategy.version if strategy else None),
            exception_step_id=exception_step_id,
            priority=priority,
            reason=reason,
            condition_text=condition_text,
            observed_value=observed_value,
            proposed_action=proposed_action,
            confidence=confidence,
            explanation=list(explanation or []),
            context=list(context or self._build_default_context(account)),
            timeline=timeline,
            waiting_minutes=0,
            status=PayflowReviewStatus.AWAITING_REVIEW.value,
            assigned_supervisor=assignee_name,
            assigned_user_id=assignee_id,
            history=history,
            days_past_due=int(account.days_past_due or 0),
            decision_version=1,
        )
        self.db.add(row)
        self.db.flush()

        self._block_account_for_review(account, proposed_action=proposed_action, stamp=stamp)
        if rule:
            rule.triggers_7d = int(rule.triggers_7d or 0) + 1

        self._record_audit(
            row,
            event="Human review created",
            detail=rule.name if rule else reason,
            actor=actor,
            actor_name=actor_name or "PayFlow",
            before_status=None,
            after_status=row.status,
            before_values=None,
            after_values=self._snapshot(row),
        )

        if commit:
            self.db.commit()
            self.db.refresh(row)
            if notify:
                # Reload relationships for notification copy.
                row = self._get_row(row.id)
                self._notify_awaiting(row)
        return row

    def create_review_for_user(
        self, user: UserModel, payload: dict[str, Any]
    ) -> dict[str, Any]:
        account = (
            self.db.query(PayflowAccountModel)
            .filter(PayflowAccountModel.id == payload["account_id"])
            .first()
        )
        if not account:
            raise NotFoundError("Account not found")
        self._assert_view(user, account.client_id)
        if not self.can_decide(user, account.client_id) and not self.access.is_operations_admin(
            user
        ):
            # Creating from API is an ops/supervisor governance action.
            if not self.access.can(user, "approve_human_reviews", account.client_id):
                raise ForbiddenError("Approve Human Reviews permission required")

        row = self.create_review(
            account_id=payload["account_id"],
            reason=payload["reason"],
            proposed_action=payload["proposed_action"],
            priority=payload.get("priority") or PayflowReviewPriority.NORMAL.value,
            rule_id=payload.get("rule_id"),
            condition_text=payload.get("condition_text"),
            observed_value=payload.get("observed_value"),
            confidence=payload.get("confidence"),
            explanation=payload.get("explanation") or [],
            context=[c if isinstance(c, dict) else c.model_dump() for c in (payload.get("context") or [])],
            strategy_id=payload.get("strategy_id"),
            workflow_version=payload.get("workflow_version"),
            exception_step_id=payload.get("exception_step_id"),
            assigned_user_id=payload.get("assigned_user_id"),
            assigned_supervisor=payload.get("assigned_supervisor"),
            actor=user,
            notify=True,
            commit=True,
        )
        return self._serialize(self._get_row(row.id), user, include_detail=True)

    def ensure_review_for_account(
        self,
        account: PayflowAccountModel,
        *,
        reason: str = "High balance treatment",
        proposed_action: str = "Move to stronger collection treatment",
        priority: str = PayflowReviewPriority.NORMAL.value,
        actor_name: str = "CRM import",
        commit: bool = False,
    ) -> PayflowHumanReviewModel | None:
        """Create an open review when an account enters Human Review status."""
        if not account.human_review and account.collection_status != (
            PayflowCollectionStatus.HUMAN_REVIEW.value
        ):
            return None
        existing = (
            self.db.query(PayflowHumanReviewModel)
            .filter(
                PayflowHumanReviewModel.account_id == account.id,
                PayflowHumanReviewModel.status.in_(list(OPEN_STATUSES)),
            )
            .first()
        )
        if existing:
            return existing
        return self.create_review(
            account_id=account.id,
            reason=reason,
            proposed_action=proposed_action,
            priority=priority,
            observed_value=f"${float(account.outstanding_balance or 0):,.2f} outstanding",
            actor_name=actor_name,
            notify=False,
            commit=commit,
        )

    def enqueue_from_rule(
        self,
        *,
        account_id: int,
        rule_id: int,
        observed_value: str | None = None,
        proposed_action: str | None = None,
        confidence: float | None = None,
        explanation: list[str] | None = None,
        strategy_id: int | None = None,
        workflow_version: int | None = None,
        exception_step_id: str | None = None,
        priority: str = PayflowReviewPriority.NORMAL.value,
        actor_name: str = "PayFlow governance",
        commit: bool = True,
        notify: bool = True,
    ) -> PayflowHumanReviewModel:
        """
        Create a Human Review when an evaluated governance rule requires intervention.
        Routine workflow outcomes must call this only for 'Require Human Review' rules.
        """
        rule = self.db.query(PayflowRuleModel).filter(PayflowRuleModel.id == rule_id).first()
        if not rule:
            raise NotFoundError("Rule not found")
        if rule.action != "Require Human Review":
            raise ValidationAppError(
                "Routine workflow outcomes do not create Human Review requests "
                "unless the rule action is 'Require Human Review'"
            )
        condition_text = None
        conditions = list(rule.conditions or [])
        if conditions:
            parts = [
                f"{c.get('field', '')} {c.get('operator', '')} {c.get('value', '')}".strip()
                for c in conditions
                if isinstance(c, dict)
            ]
            condition_text = f" {rule.logic} ".join(p for p in parts if p) or None

        return self.create_review(
            account_id=account_id,
            rule_id=rule.id,
            reason=rule.name,
            proposed_action=proposed_action
            or "Continue current treatment with adjusted communication",
            priority=priority,
            condition_text=condition_text,
            observed_value=observed_value,
            confidence=confidence,
            explanation=explanation,
            strategy_id=strategy_id,
            workflow_version=workflow_version,
            exception_step_id=exception_step_id,
            actor_name=actor_name,
            notify=notify,
            commit=commit,
        )

    def assign_review(
        self,
        user: UserModel,
        review_id: int,
        *,
        assigned_user_id: str | None = None,
        assigned_supervisor: str | None = None,
    ) -> dict[str, Any]:
        row = self._require_status(
            user,
            review_id,
            allowed_statuses=OPEN_STATUSES,
            decide=True,
        )
        before = self._snapshot(row)
        before_status = row.status
        assignee_id = _as_uuid(assigned_user_id)
        name = (assigned_supervisor or "").strip() or None
        if assignee_id:
            assignee = self.db.get(UserModel, assignee_id)
            if not assignee:
                raise NotFoundError("Assignee not found")
            if not self.access.can_see_client(assignee, row.client_id) and not self.access.is_operations_admin(
                assignee
            ):
                raise ValidationAppError("Assignee does not have access to this client")
            name = name or assignee.full_name
        row.assigned_user_id = assignee_id
        row.assigned_supervisor = name
        self._append_history(
            row,
            [
                {
                    "at": _stamp(),
                    "event": "Assigned to supervisor" if name else "Assignment cleared",
                    "detail": name,
                    "by": user.full_name,
                }
            ],
        )
        self._bump_version(row)
        self._record_audit(
            row,
            event="Review assignment updated",
            detail=name,
            actor=user,
            before_status=before_status,
            after_status=row.status,
            before_values=before,
            after_values=self._snapshot(row),
        )
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(self._get_row(row.id), user, include_detail=True)

    # ------------------------------------------------------------------ list / detail
    def list_reviews(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        status: str | None = None,
        priority: str | None = None,
        reason: str | None = None,
        rule_id: int | None = None,
        proposed_action: str | None = None,
        search: str | None = None,
        waiting_bucket: str | None = None,
        sort_by: str = "waiting_minutes",
        sort_dir: str = "desc",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        if status and status not in {s.value for s in PayflowReviewStatus}:
            raise ValidationAppError("Invalid status filter")
        if priority and priority not in {p.value for p in PayflowReviewPriority}:
            raise ValidationAppError("Invalid priority filter")
        if waiting_bucket and waiting_bucket not in WAITING_BUCKETS:
            raise ValidationAppError("Invalid waiting bucket filter")
        if sort_by not in SORT_FIELDS:
            raise ValidationAppError("Invalid sort field")
        sort_dir = sort_dir.lower()
        if sort_dir not in {"asc", "desc"}:
            raise ValidationAppError("Invalid sort direction")
        page = max(1, int(page or 1))
        page_size = min(200, max(1, int(page_size or 50)))

        allowed = self._visible_client_ids(user)
        q = self._base_query()
        if allowed is not None:
            if not allowed:
                return self._empty_list(
                    page=page, page_size=page_size, sort_by=sort_by, sort_dir=sort_dir
                )
            q = q.filter(PayflowHumanReviewModel.client_id.in_(allowed))
        if client_id is not None:
            self._assert_view(user, client_id)
            q = q.filter(PayflowHumanReviewModel.client_id == client_id)

        # Summary uses client-scoped visibility (not table filters), matching Lovable KPIs.
        summary_rows = self._filter_permission_rows(user, q.all())
        summary = self._summary_for_rows(summary_rows)

        if status:
            q = q.filter(PayflowHumanReviewModel.status == status)
        if priority:
            q = q.filter(PayflowHumanReviewModel.priority == priority)
        if reason:
            q = q.filter(PayflowHumanReviewModel.reason == reason)
        if rule_id is not None:
            q = q.filter(PayflowHumanReviewModel.rule_id == rule_id)
        if proposed_action:
            q = q.filter(PayflowHumanReviewModel.proposed_action == proposed_action)

        rows = self._filter_permission_rows(user, q.all())

        if search:
            term = search.strip().lower()
            rows = [
                r
                for r in rows
                if term
                in " ".join(
                    [
                        (r.account.customer_name if r.account else "") or "",
                        (r.account.account_reference if r.account else "") or "",
                        (r.account.case_reference if r.account else "") or "",
                        r.reason or "",
                        (r.rule.name if r.rule else "") or "",
                        r.proposed_action or "",
                        r.code or "",
                    ]
                ).lower()
            ]
        if waiting_bucket and waiting_bucket != "Any Age":
            rows = [r for r in rows if _in_waiting_bucket(self._waiting_minutes(r), waiting_bucket)]

        rows = self._sort_rows(rows, sort_by, sort_dir)
        total = len(rows)
        total_pages = (total + page_size - 1) // page_size if total else 0
        start = (page - 1) * page_size
        page_rows = rows[start : start + page_size]

        return {
            "reviews": [self._serialize(r, user) for r in page_rows],
            "summary": summary,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            **self._catalogs(),
        }

    def get_review(self, user: UserModel, review_id: int) -> dict[str, Any]:
        row = self._get_row(review_id)
        self._assert_view(user, row.client_id)
        if not self.access.is_operations_admin(user) and not self.access.can(
            user, "view_human_reviews", row.client_id
        ):
            raise ForbiddenError("Review access denied")
        return self._serialize(row, user, include_detail=True)

    # ------------------------------------------------------------------ decisions
    def approve(
        self,
        user: UserModel,
        review_id: int,
        *,
        note: str | None = None,
        decision_version: int | None = None,
    ) -> dict[str, Any]:
        row = self._require_status(
            user,
            review_id,
            allowed_statuses={PayflowReviewStatus.AWAITING_REVIEW.value},
            decide=True,
            decision_version=decision_version,
        )
        before = self._snapshot(row)
        before_status = row.status
        row.status = PayflowReviewStatus.APPROVED.value
        row.final_action = row.proposed_action
        row.assigned_supervisor = user.full_name
        row.decided_by_user_id = user.id
        row.decided_at = _utcnow()
        self._append_history(
            row,
            [
                {
                    "at": _stamp(),
                    "event": "Supervisor approved recommendation",
                    "detail": note or row.proposed_action,
                    "by": user.full_name,
                },
                {
                    "at": _stamp(),
                    "event": "Approved action released to collection case",
                    "by": user.full_name,
                },
            ],
        )
        self._apply_case_decision(
            row,
            decision="approved",
            final_action=row.final_action or row.proposed_action,
            actor_name=user.full_name,
        )
        self._bump_version(row)
        self._record_audit(
            row,
            event="Supervisor approved recommendation",
            detail=note or row.proposed_action,
            actor=user,
            before_status=before_status,
            after_status=row.status,
            before_values=before,
            after_values=self._snapshot(row),
        )
        self.db.commit()
        self.db.refresh(row)
        self._mark_review_notifications_read(row.id)
        self._notify_decided(row, user)
        self._resume_workflow_after_review(row.id, continue_workflow=True)
        return self._serialize(self._get_row(row.id), user, include_detail=True)

    def modify(
        self,
        user: UserModel,
        review_id: int,
        *,
        action: str,
        guidance: str | None = None,
        decision_version: int | None = None,
    ) -> dict[str, Any]:
        row = self._require_status(
            user,
            review_id,
            allowed_statuses={PayflowReviewStatus.AWAITING_REVIEW.value},
            decide=True,
            modify=True,
            decision_version=decision_version,
        )
        if action not in PROPOSED_ACTIONS:
            raise ValidationAppError("Invalid modified action")
        before = self._snapshot(row)
        before_status = row.status
        row.status = PayflowReviewStatus.MODIFIED.value
        row.final_action = action
        row.guidance = guidance
        row.assigned_supervisor = user.full_name
        row.decided_by_user_id = user.id
        row.decided_at = _utcnow()
        self._append_history(
            row,
            [
                {
                    "at": _stamp(),
                    "event": "Supervisor modified recommendation",
                    "detail": f"Final action: {action}",
                    "by": user.full_name,
                },
                {
                    "at": _stamp(),
                    "event": "Adjusted action released to collection case",
                    "detail": guidance,
                    "by": user.full_name,
                },
            ],
        )
        self._apply_case_decision(
            row, decision="modified", final_action=action, actor_name=user.full_name
        )
        self._bump_version(row)
        self._record_audit(
            row,
            event="Supervisor modified recommendation",
            detail=f"Final action: {action}",
            actor=user,
            before_status=before_status,
            after_status=row.status,
            before_values=before,
            after_values=self._snapshot(row),
        )
        self.db.commit()
        self.db.refresh(row)
        self._mark_review_notifications_read(row.id)
        self._notify_decided(row, user)
        self._resume_workflow_after_review(row.id, continue_workflow=True)
        return self._serialize(self._get_row(row.id), user, include_detail=True)

    def reject(
        self,
        user: UserModel,
        review_id: int,
        *,
        reason: str,
        comment: str | None = None,
        decision_version: int | None = None,
    ) -> dict[str, Any]:
        row = self._require_status(
            user,
            review_id,
            allowed_statuses={PayflowReviewStatus.AWAITING_REVIEW.value},
            decide=True,
            decision_version=decision_version,
        )
        if reason not in REJECTION_REASONS:
            raise ValidationAppError("Invalid rejection reason")
        before = self._snapshot(row)
        before_status = row.status
        row.status = PayflowReviewStatus.REJECTED.value
        row.rejection_reason = reason
        row.final_action = "Proposed action cancelled — case returned for reassessment"
        row.assigned_supervisor = user.full_name
        row.decided_by_user_id = user.id
        row.decided_at = _utcnow()
        detail = reason if not comment else f"{reason} · {comment}"
        self._append_history(
            row,
            [
                {
                    "at": _stamp(),
                    "event": "Supervisor rejected recommendation",
                    "detail": detail,
                    "by": user.full_name,
                },
                {
                    "at": _stamp(),
                    "event": "Case returned for reassessment",
                    "by": user.full_name,
                },
            ],
        )
        self._apply_case_decision(
            row,
            decision="reject",
            final_action=row.final_action,
            actor_name=user.full_name,
        )
        self._bump_version(row)
        self._record_audit(
            row,
            event="Supervisor rejected recommendation",
            detail=detail,
            actor=user,
            before_status=before_status,
            after_status=row.status,
            before_values=before,
            after_values=self._snapshot(row),
        )
        self.db.commit()
        self.db.refresh(row)
        self._mark_review_notifications_read(row.id)
        self._notify_decided(row, user)
        self._resume_workflow_after_review(row.id, continue_workflow=False)
        return self._serialize(self._get_row(row.id), user, include_detail=True)

    def hold(
        self,
        user: UserModel,
        review_id: int,
        *,
        until: str | None = None,
        reason: str | None = None,
        decision_version: int | None = None,
    ) -> dict[str, Any]:
        row = self._require_status(
            user,
            review_id,
            allowed_statuses={PayflowReviewStatus.AWAITING_REVIEW.value},
            decide=True,
            decision_version=decision_version,
        )
        before = self._snapshot(row)
        before_status = row.status
        hold_until = (until or "").strip() or "Further review"
        row.status = PayflowReviewStatus.ON_HOLD.value
        row.hold_until = hold_until
        row.final_action = "Collection activity paused"
        row.assigned_supervisor = user.full_name
        row.decided_by_user_id = user.id
        row.decided_at = _utcnow()
        self._append_history(
            row,
            [
                {
                    "at": _stamp(),
                    "event": f"Held until {hold_until}",
                    "detail": reason,
                    "by": user.full_name,
                }
            ],
        )
        self._apply_case_decision(
            row,
            decision="hold",
            final_action=row.final_action,
            actor_name=user.full_name,
        )
        self._bump_version(row)
        self._record_audit(
            row,
            event=f"Held until {hold_until}",
            detail=reason,
            actor=user,
            before_status=before_status,
            after_status=row.status,
            before_values=before,
            after_values=self._snapshot(row),
        )
        self.db.commit()
        self.db.refresh(row)
        self._mark_review_notifications_read(row.id)
        self._notify_decided(row, user)
        return self._serialize(self._get_row(row.id), user, include_detail=True)

    def release(
        self,
        user: UserModel,
        review_id: int,
        *,
        note: str | None = None,
        decision_version: int | None = None,
    ) -> dict[str, Any]:
        """Release a held review back to Awaiting Review for decision."""
        row = self._require_status(
            user,
            review_id,
            allowed_statuses={PayflowReviewStatus.ON_HOLD.value},
            release=True,
            decision_version=decision_version,
        )
        before = self._snapshot(row)
        before_status = row.status
        row.status = PayflowReviewStatus.AWAITING_REVIEW.value
        row.hold_until = None
        row.final_action = None
        row.assigned_supervisor = user.full_name
        account = (
            self.db.query(PayflowAccountModel)
            .filter(PayflowAccountModel.id == row.account_id)
            .with_for_update()
            .first()
        )
        if account:
            account.human_review = True
            account.collection_status = PayflowCollectionStatus.HUMAN_REVIEW.value
            account.next_action = "Awaiting human review decision"
            account.last_action = f"Human review released by {user.full_name}"
            events = list(account.timeline or [])
            events.append(
                {
                    "label": "Human review released",
                    "detail": note or "Hold released — awaiting decision",
                    "at": _stamp(),
                    "review_id": row.id,
                }
            )
            account.timeline = events[-40:]
        self._append_history(
            row,
            [
                {
                    "at": _stamp(),
                    "event": "Hold released — returned to awaiting review",
                    "detail": note,
                    "by": user.full_name,
                }
            ],
        )
        self._bump_version(row)
        self._record_audit(
            row,
            event="Hold released",
            detail=note,
            actor=user,
            before_status=before_status,
            after_status=row.status,
            before_values=before,
            after_values=self._snapshot(row),
        )
        self.db.commit()
        self.db.refresh(row)
        self._notify_awaiting(self._get_row(row.id))
        return self._serialize(self._get_row(row.id), user, include_detail=True)

    def is_action_blocked(self, account_id: int) -> bool:
        """True when mandatory review approval is outstanding for the case."""
        open_review = (
            self.db.query(PayflowHumanReviewModel.id)
            .filter(
                PayflowHumanReviewModel.account_id == account_id,
                PayflowHumanReviewModel.status.in_(list(OPEN_STATUSES)),
            )
            .first()
        )
        if open_review:
            return True
        account = self.db.get(PayflowAccountModel, account_id)
        return bool(account and account.human_review)
