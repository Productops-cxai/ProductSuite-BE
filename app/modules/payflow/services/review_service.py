from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowHumanReviewModel,
    PayflowRuleModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import PayflowReviewPriority, PayflowReviewStatus

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


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%d %b · %H:%M")


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


class PayflowReviewService:
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

    def _serialize(self, row: PayflowHumanReviewModel, user: UserModel | None = None) -> dict[str, Any]:
        account = row.account
        client = row.client
        rule = row.rule
        data = {
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
            "days_past_due": row.days_past_due,
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
            "waiting_minutes": row.waiting_minutes,
            "waiting_label": _format_waiting(row.waiting_minutes),
            "status": row.status,
            "assigned_supervisor": row.assigned_supervisor,
            "final_action": row.final_action,
            "guidance": row.guidance,
            "rejection_reason": row.rejection_reason,
            "hold_until": row.hold_until,
            "history": list(row.history or []),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        if user is not None:
            data["can_decide"] = self.can_decide(user, row.client_id)
            data["can_modify"] = self.can_modify(user, row.client_id)
        return data

    def list_reviews(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        status: str | None = None,
        priority: str | None = None,
        reason: str | None = None,
        search: str | None = None,
        waiting_bucket: str | None = None,
    ) -> dict[str, Any]:
        allowed = self._visible_client_ids(user)
        q = self.db.query(PayflowHumanReviewModel).options(
            joinedload(PayflowHumanReviewModel.client),
            joinedload(PayflowHumanReviewModel.account),
            joinedload(PayflowHumanReviewModel.rule),
        )
        if allowed is not None:
            if not allowed:
                return self._empty_list()
            q = q.filter(PayflowHumanReviewModel.client_id.in_(allowed))
        if client_id is not None:
            self._assert_view(user, client_id)
            q = q.filter(PayflowHumanReviewModel.client_id == client_id)
        if status:
            q = q.filter(PayflowHumanReviewModel.status == status)
        if priority:
            q = q.filter(PayflowHumanReviewModel.priority == priority)
        if reason:
            q = q.filter(PayflowHumanReviewModel.reason == reason)

        rows = q.order_by(PayflowHumanReviewModel.waiting_minutes.desc()).all()
        if not self.access.is_operations_admin(user):
            rows = [
                r
                for r in rows
                if self.access.can(user, "view_human_reviews", r.client_id)
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
                        r.reason or "",
                        (r.rule.name if r.rule else ""),
                        r.proposed_action or "",
                    ]
                ).lower()
            ]
        if waiting_bucket and waiting_bucket != "Any Age":
            rows = [r for r in rows if _in_waiting_bucket(r.waiting_minutes, waiting_bucket)]

        awaiting = [r for r in rows if r.status == PayflowReviewStatus.AWAITING_REVIEW.value]
        summary = {
            "awaiting": len(awaiting),
            "high_priority": sum(
                1 for r in awaiting if r.priority == PayflowReviewPriority.HIGH.value
            ),
            "due_today": sum(1 for r in awaiting if r.waiting_minutes < 1440),
            "on_hold": sum(1 for r in rows if r.status == PayflowReviewStatus.ON_HOLD.value),
        }
        return {
            "reviews": [self._serialize(r, user) for r in rows],
            "summary": summary,
            "statuses": [s.value for s in PayflowReviewStatus],
            "priorities": [p.value for p in PayflowReviewPriority],
            "reasons": REVIEW_REASONS,
            "proposed_actions": PROPOSED_ACTIONS,
            "rejection_reasons": REJECTION_REASONS,
            "waiting_buckets": [
                "Any Age",
                "Under 30 min",
                "30 min – 2 hours",
                "2 – 24 hours",
                "Over 24 hours",
            ],
        }

    def get_review(self, user: UserModel, review_id: int) -> dict[str, Any]:
        row = self._get_row(review_id)
        self._assert_view(user, row.client_id)
        return self._serialize(row, user)

    def approve(
        self, user: UserModel, review_id: int, *, note: str | None = None
    ) -> dict[str, Any]:
        row = self._require_awaiting(user, review_id, decide=True)
        row.status = PayflowReviewStatus.APPROVED.value
        row.final_action = row.proposed_action
        row.assigned_supervisor = user.full_name
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
        self._clear_account_flag(row)
        self.db.commit()
        self.db.refresh(row)
        self._mark_review_notifications_read(row.id)
        return self._serialize(row, user)

    def modify(
        self,
        user: UserModel,
        review_id: int,
        *,
        action: str,
        guidance: str | None = None,
    ) -> dict[str, Any]:
        row = self._require_awaiting(user, review_id, decide=True, modify=True)
        if action not in PROPOSED_ACTIONS:
            raise ValidationAppError("Invalid modified action")
        row.status = PayflowReviewStatus.MODIFIED.value
        row.final_action = action
        row.guidance = guidance
        row.assigned_supervisor = user.full_name
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
        self._clear_account_flag(row)
        self.db.commit()
        self.db.refresh(row)
        self._mark_review_notifications_read(row.id)
        return self._serialize(row, user)

    def reject(
        self,
        user: UserModel,
        review_id: int,
        *,
        reason: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        row = self._require_awaiting(user, review_id, decide=True)
        if reason not in REJECTION_REASONS:
            raise ValidationAppError("Invalid rejection reason")
        row.status = PayflowReviewStatus.REJECTED.value
        row.rejection_reason = reason
        row.final_action = "Proposed action cancelled — case returned for reassessment"
        row.assigned_supervisor = user.full_name
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
        self._clear_account_flag(row)
        self.db.commit()
        self.db.refresh(row)
        self._mark_review_notifications_read(row.id)
        return self._serialize(row, user)

    def hold(
        self,
        user: UserModel,
        review_id: int,
        *,
        until: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        row = self._require_awaiting(user, review_id, decide=True)
        hold_until = (until or "").strip() or "Further review"
        row.status = PayflowReviewStatus.ON_HOLD.value
        row.hold_until = hold_until
        row.final_action = "Collection activity paused"
        row.assigned_supervisor = user.full_name
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
        self.db.commit()
        self.db.refresh(row)
        self._mark_review_notifications_read(row.id)
        return self._serialize(row, user)

    def _get_row(self, review_id: int) -> PayflowHumanReviewModel:
        row = (
            self.db.query(PayflowHumanReviewModel)
            .options(
                joinedload(PayflowHumanReviewModel.client),
                joinedload(PayflowHumanReviewModel.account),
                joinedload(PayflowHumanReviewModel.rule),
            )
            .filter(PayflowHumanReviewModel.id == review_id)
            .first()
        )
        if not row:
            raise NotFoundError("Review not found")
        return row

    def _require_awaiting(
        self,
        user: UserModel,
        review_id: int,
        *,
        decide: bool = False,
        modify: bool = False,
    ) -> PayflowHumanReviewModel:
        row = self._get_row(review_id)
        self._assert_view(user, row.client_id)
        if decide and not self.can_decide(user, row.client_id):
            raise ForbiddenError("Approve Human Reviews permission required")
        if modify and not self.can_modify(user, row.client_id):
            raise ForbiddenError("Modify / Guide AI Recommendation permission required")
        if row.status != PayflowReviewStatus.AWAITING_REVIEW.value:
            raise ValidationAppError("Only awaiting reviews can be decided")
        return row

    def _append_history(self, row: PayflowHumanReviewModel, entries: list[dict]) -> None:
        history = list(row.history or [])
        history.extend(entries)
        row.history = history

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

    def _clear_account_flag(self, row: PayflowHumanReviewModel) -> None:
        account = (
            self.db.query(PayflowAccountModel)
            .filter(PayflowAccountModel.id == row.account_id)
            .first()
        )
        if account:
            account.human_review = False

    def _empty_list(self) -> dict[str, Any]:
        return {
            "reviews": [],
            "summary": {"awaiting": 0, "high_priority": 0, "due_today": 0, "on_hold": 0},
            "statuses": [s.value for s in PayflowReviewStatus],
            "priorities": [p.value for p in PayflowReviewPriority],
            "reasons": REVIEW_REASONS,
            "proposed_actions": PROPOSED_ACTIONS,
            "rejection_reasons": REJECTION_REASONS,
            "waiting_buckets": [
                "Any Age",
                "Under 30 min",
                "30 min – 2 hours",
                "2 – 24 hours",
                "Over 24 hours",
            ],
        }
