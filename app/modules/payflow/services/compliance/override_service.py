"""Authorized, time-bounded compliance overrides (FR-13).

The evaluation engine stays pure: it always returns the legal restriction.
This service only clears a matched restriction when an Approved override covers
the account, rule, channel and proposed time — and only when the rule's
``override_policy`` permits it. ``Not Permitted`` can never be overridden.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowComplianceOverrideModel,
    PayflowComplianceRuleModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.modules.payflow.services.compliance import catalog
from app.modules.payflow.services.compliance.engine import EvaluationInput, EvaluationResult
from app.modules.payflow.services.compliance.rule_service import ComplianceRuleService

STATUS_PENDING = "Pending"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"
STATUS_EXPIRED = "Expired"
STATUS_USED = "Used"
STATUSES = [STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED, STATUS_USED]

POLICY_NOT_PERMITTED = "Not Permitted"
POLICY_OPS_ADMIN = "Operations Admin With Reason"
POLICY_CLIENT = "Client Configurable"

# How long an approved override lasts when the requester does not set an end.
_DEFAULT_WINDOW = timedelta(hours=24)
_MAX_WINDOW = timedelta(days=7)

ALL_CHANNELS = "All Channels"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _recompute(result: EvaluationResult) -> EvaluationResult:
    """Rebuild ALLOW / BLOCK / RESCHEDULE / REVIEW_REQUIRED from remaining matches."""
    from app.modules.payflow.services.compliance.engine import (
        BLOCKING_ACTIONS,
        RESCHEDULE_ACTION,
        REVIEW_ACTIONS,
    )

    actions = {m.get("action") for m in result.matches}
    if actions & BLOCKING_ACTIONS:
        result.decision = "BLOCK"
        result.requires_review = False
        result.next_permitted_at = None
    elif actions & REVIEW_ACTIONS:
        result.decision = "REVIEW_REQUIRED"
        result.requires_review = True
        result.next_permitted_at = None
    elif RESCHEDULE_ACTION in actions:
        result.decision = "RESCHEDULE"
        result.requires_review = False
        # Keep any previously computed next window when reschedule matches remain.
    else:
        result.decision = "ALLOW"
        result.requires_review = False
        result.next_permitted_at = None
    result.explanation = (
        "; ".join(f"{m['rule_code']}: {m['explanation']}" for m in result.matches)
        or (
            "All applicable checks passed"
            + (
                f" (override applied: {len(result.overrides_applied)})"
                if result.overrides_applied
                else "."
            )
        )
    )
    if result.overrides_applied and result.decision == "ALLOW" and not result.matches:
        codes = ", ".join(o.get("rule_code") or str(o.get("rule_id")) for o in result.overrides_applied)
        result.explanation = f"All applicable checks passed with authorized override(s): {codes}."
    return result


class ComplianceOverrideService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def serialize(self, row: PayflowComplianceOverrideModel) -> dict[str, Any]:
        rule = self.db.get(PayflowComplianceRuleModel, row.rule_id)
        return {
            "id": row.id,
            "account_id": row.account_id,
            "client_id": row.client_id,
            "rule_id": row.rule_id,
            "rule_code": rule.code if rule else None,
            "rule_name": rule.name if rule else None,
            "override_policy": rule.override_policy if rule else None,
            "channel": row.channel,
            "status": row.status,
            "reason": row.reason,
            "evidence": row.evidence,
            "requested_by": row.requested_by,
            "requested_at": row.requested_at.isoformat() if row.requested_at else None,
            "reviewed_by": row.reviewed_by,
            "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
            "review_note": row.review_note,
            "valid_from": row.valid_from.isoformat() if row.valid_from else None,
            "valid_until": row.valid_until.isoformat() if row.valid_until else None,
            "communication_id": row.communication_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def _account_for(self, user: UserModel, account_id: int) -> PayflowAccountModel:
        account = self.db.get(PayflowAccountModel, account_id)
        if not account:
            raise NotFoundError("Account not found")
        if not self.access.can_see_client(user, account.client_id):
            raise ForbiddenError("Client access denied")
        return account

    def _can_request(self, user: UserModel, client_id: int) -> bool:
        return self.access.is_operations_admin(user) or self.access.can(
            user, "modify_governance", client_id
        )

    def _can_approve(self, user: UserModel, policy: str | None, client_id: int) -> bool:
        if policy == POLICY_OPS_ADMIN:
            return self.access.is_operations_admin(user)
        if policy == POLICY_CLIENT:
            return self.access.is_operations_admin(user) or self.access.can(
                user, "modify_governance", client_id
            )
        return False

    def list_for_account(
        self,
        user: UserModel,
        account_id: int,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        account = self._account_for(user, account_id)
        if not (
            self.access.is_operations_admin(user)
            or self.access.can(user, "view_rules")
            or self.access.can(user, "view_client", account.client_id)
        ):
            raise ForbiddenError("Permission required: view_rules")
        query = self.db.query(PayflowComplianceOverrideModel).filter(
            PayflowComplianceOverrideModel.account_id == account.id
        )
        if status:
            query = query.filter(PayflowComplianceOverrideModel.status == status)
        rows = (
            query.order_by(PayflowComplianceOverrideModel.id.desc())
            .limit(min(max(limit, 1), 200))
            .all()
        )
        return {"overrides": [self.serialize(r) for r in rows], "total": len(rows)}

    def request(
        self,
        user: UserModel,
        *,
        account_id: int,
        rule_id: int,
        reason: str,
        evidence: str,
        channel: str = ALL_CHANNELS,
        valid_until: str | None = None,
        communication_id: int | None = None,
    ) -> dict[str, Any]:
        account = self._account_for(user, account_id)
        if not self._can_request(user, account.client_id):
            raise ForbiddenError("Permission required: modify_governance")
        reason_text = (reason or "").strip()
        evidence_text = (evidence or "").strip()
        if not reason_text:
            raise ValidationAppError("A reason for the override is required")
        if not evidence_text:
            raise ValidationAppError("Evidence for the override is required")
        rule = self.db.get(PayflowComplianceRuleModel, rule_id)
        if not rule or rule.client_id not in (None, account.client_id):
            raise NotFoundError("Compliance rule not found for this account")
        if rule.status != catalog.STATUS_ACTIVE:
            raise ValidationAppError("Only Active rules can be overridden")
        policy = (rule.override_policy or POLICY_NOT_PERMITTED).strip() or POLICY_NOT_PERMITTED
        if policy == POLICY_NOT_PERMITTED:
            raise ForbiddenError(
                "This rule's override policy does not permit an exception"
            )
        channel_text = (channel or ALL_CHANNELS).strip() or ALL_CHANNELS
        if channel_text not in catalog.CHANNELS:
            raise ValidationAppError(
                f"channel must be one of: {', '.join(catalog.CHANNELS)}"
            )
        until = _parse_dt(valid_until)
        now = _utcnow()
        if until is not None:
            if until <= now:
                raise ValidationAppError("valid_until must be in the future")
            if until - now > _MAX_WINDOW:
                raise ValidationAppError("Override window cannot exceed 7 days")
        row = PayflowComplianceOverrideModel(
            account_id=account.id,
            client_id=account.client_id,
            rule_id=rule.id,
            channel=channel_text,
            status=STATUS_PENDING,
            reason=reason_text,
            evidence=evidence_text,
            requested_by=user.full_name,
            requested_by_user_id=user.id,
            requested_at=now,
            communication_id=communication_id,
        )
        self.db.add(row)
        self.db.flush()
        ComplianceRuleService(self.db).audit(
            entity_type="override",
            entity_id=row.id,
            rule=rule,
            client_id=account.client_id,
            account_id=account.id,
            event="Override requested",
            actor=user,
            reason=reason_text,
            before=None,
            after={
                "evidence": evidence_text,
                "channel": channel_text,
                "policy": policy,
                "valid_until": until.isoformat() if until else None,
            },
        )
        # Stash requested end so approve can honour it.
        if until is not None:
            row.valid_until = until
            self.db.flush()
        self.db.commit()
        self.db.refresh(row)
        return self.serialize(row)

    def approve(
        self,
        user: UserModel,
        override_id: int,
        *,
        review_note: str | None = None,
        valid_until: str | None = None,
    ) -> dict[str, Any]:
        row = self.db.get(PayflowComplianceOverrideModel, override_id)
        if not row:
            raise NotFoundError("Override not found")
        if not self.access.can_see_client(user, row.client_id):
            raise ForbiddenError("Client access denied")
        rule = self.db.get(PayflowComplianceRuleModel, row.rule_id)
        if not rule:
            raise NotFoundError("Compliance rule not found")
        policy = (rule.override_policy or POLICY_NOT_PERMITTED).strip() or POLICY_NOT_PERMITTED
        if not self._can_approve(user, policy, row.client_id):
            raise ForbiddenError(
                "You are not authorized to approve an override for this rule's policy"
            )
        if row.status != STATUS_PENDING:
            raise ValidationAppError(f"Only Pending overrides can be approved (status={row.status})")
        now = _utcnow()
        until = _parse_dt(valid_until) or _aware(row.valid_until) or (now + _DEFAULT_WINDOW)
        if until <= now:
            raise ValidationAppError("valid_until must be in the future")
        if until - now > _MAX_WINDOW:
            until = now + _MAX_WINDOW
        before = self.serialize(row)
        row.status = STATUS_APPROVED
        row.reviewed_by = user.full_name
        row.reviewed_by_user_id = user.id
        row.reviewed_at = now
        row.review_note = (review_note or "").strip() or None
        row.valid_from = now
        row.valid_until = until
        self.db.flush()
        ComplianceRuleService(self.db).audit(
            entity_type="override",
            entity_id=row.id,
            rule=rule,
            client_id=row.client_id,
            account_id=row.account_id,
            event="Override approved",
            actor=user,
            reason=row.review_note or row.reason,
            before=before,
            after=self.serialize(row),
            approval="Approved",
        )
        self.db.commit()
        self.db.refresh(row)
        return self.serialize(row)

    def reject(
        self,
        user: UserModel,
        override_id: int,
        *,
        review_note: str,
    ) -> dict[str, Any]:
        row = self.db.get(PayflowComplianceOverrideModel, override_id)
        if not row:
            raise NotFoundError("Override not found")
        if not self.access.can_see_client(user, row.client_id):
            raise ForbiddenError("Client access denied")
        rule = self.db.get(PayflowComplianceRuleModel, row.rule_id)
        policy = (rule.override_policy or POLICY_NOT_PERMITTED).strip() if rule else POLICY_NOT_PERMITTED
        if not self._can_approve(user, policy or POLICY_NOT_PERMITTED, row.client_id):
            raise ForbiddenError(
                "You are not authorized to reject an override for this rule's policy"
            )
        if row.status != STATUS_PENDING:
            raise ValidationAppError(f"Only Pending overrides can be rejected (status={row.status})")
        note = (review_note or "").strip()
        if not note:
            raise ValidationAppError("A rejection note is required")
        before = self.serialize(row)
        row.status = STATUS_REJECTED
        row.reviewed_by = user.full_name
        row.reviewed_by_user_id = user.id
        row.reviewed_at = _utcnow()
        row.review_note = note
        self.db.flush()
        ComplianceRuleService(self.db).audit(
            entity_type="override",
            entity_id=row.id,
            rule=rule,
            client_id=row.client_id,
            account_id=row.account_id,
            event="Override rejected",
            actor=user,
            reason=note,
            before=before,
            after=self.serialize(row),
            approval="Rejected",
        )
        self.db.commit()
        self.db.refresh(row)
        return self.serialize(row)

    def active_for(
        self,
        account_id: int,
        rule_id: int,
        *,
        channel: str,
        at: datetime,
    ) -> PayflowComplianceOverrideModel | None:
        """Approved override covering this account/rule/channel at ``at``, if any."""
        when = _aware(at) or _utcnow()
        rows = (
            self.db.query(PayflowComplianceOverrideModel)
            .filter(
                PayflowComplianceOverrideModel.account_id == account_id,
                PayflowComplianceOverrideModel.rule_id == rule_id,
                PayflowComplianceOverrideModel.status == STATUS_APPROVED,
            )
            .order_by(PayflowComplianceOverrideModel.id.desc())
            .all()
        )
        channel_l = (channel or "").strip().lower()
        for row in rows:
            start, end = _aware(row.valid_from), _aware(row.valid_until)
            if start and when < start:
                continue
            if end and when > end:
                if row.status == STATUS_APPROVED:
                    row.status = STATUS_EXPIRED
                    self.db.flush()
                continue
            row_channel = (row.channel or ALL_CHANNELS).strip()
            if row_channel != ALL_CHANNELS and row_channel.lower() != channel_l:
                continue
            return row
        return None

    def apply_to_result(
        self,
        result: EvaluationResult,
        data: EvaluationInput,
    ) -> EvaluationResult:
        """Clear matched restrictions that an Approved override covers. Engine output otherwise untouched."""
        if result.decision == "ALLOW" or not data.account_id or not result.matches:
            # Still advertise which matches could be requested.
            for match in result.matches:
                policy = match.get("override_policy") or POLICY_NOT_PERMITTED
                match["overrideable"] = bool(
                    match.get("rule_id") and policy != POLICY_NOT_PERMITTED
                )
            return result

        remaining: list[dict[str, Any]] = []
        applied: list[dict[str, Any]] = []
        for match in result.matches:
            rule_id = match.get("rule_id")
            policy = match.get("override_policy") or POLICY_NOT_PERMITTED
            match["overrideable"] = bool(rule_id and policy != POLICY_NOT_PERMITTED)
            if not rule_id or policy == POLICY_NOT_PERMITTED:
                remaining.append(match)
                continue
            row = self.active_for(
                data.account_id, int(rule_id), channel=data.channel, at=data.at
            )
            if not row:
                remaining.append(match)
                continue
            applied.append(
                {
                    "override_id": row.id,
                    "rule_id": rule_id,
                    "rule_code": match.get("rule_code"),
                    "reason": row.reason,
                    "evidence": row.evidence,
                    "approved_by": row.reviewed_by,
                    "valid_until": row.valid_until.isoformat() if row.valid_until else None,
                }
            )
        if not applied:
            return result
        result.matches = remaining
        result.overrides_applied = applied
        return _recompute(result)
