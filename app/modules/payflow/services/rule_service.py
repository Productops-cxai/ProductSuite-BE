from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowClientModel,
    PayflowHumanReviewModel,
    PayflowRuleModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.deletion import record_deletion, snapshot_model
from app.shared.enums import PayflowRuleLogic, PayflowRuleStatus, PayflowRuleType

RULE_CATEGORIES = [
    "Amount",
    "Collection Attempts",
    "Payment Status",
    "Payment Activity",
    "Promise-to-Pay",
    "Payment Plan",
    "Workflow",
    "Communication",
    "Customer Risk",
    "AI Confidence",
]

RULE_ACTIONS = [
    "Require Human Review",
    "Hold Action",
    "Prevent Communication",
    "Request Reassessment",
    "Escalate Case",
    "Apply / Change Workflow",
]

RULE_FIELDS = [
    {"label": "Outstanding Balance", "category": "Amount", "type": "currency"},
    {"label": "Original Balance", "category": "Amount", "type": "currency"},
    {"label": "Recovered Amount", "category": "Amount", "type": "currency"},
    {"label": "Unsuccessful Attempts", "category": "Collection Attempts", "type": "number"},
    {"label": "Total Contact Attempts", "category": "Collection Attempts", "type": "number"},
    {"label": "Days Since Last Contact", "category": "Collection Attempts", "type": "number"},
    {
        "label": "Payment Status",
        "category": "Payment Status",
        "type": "enum",
        "options": ["Unpaid", "Partially Paid", "Paid", "Failed", "Refunded"],
    },
    {"label": "Days Past Due", "category": "Payment Status", "type": "number"},
    {"label": "Failed Payment Attempts", "category": "Payment Activity", "type": "number"},
    {"label": "Last Payment Amount", "category": "Payment Activity", "type": "currency"},
    {
        "label": "Promise-to-Pay Status",
        "category": "Promise-to-Pay",
        "type": "enum",
        "options": ["None", "Active", "Kept", "Broken"],
    },
    {"label": "Days Until Promise Date", "category": "Promise-to-Pay", "type": "number"},
    {
        "label": "Payment Plan Status",
        "category": "Payment Plan",
        "type": "enum",
        "options": ["None", "On Track", "At Risk", "Defaulted"],
    },
    {"label": "Missed Installments", "category": "Payment Plan", "type": "number"},
    {
        "label": "Workflow",
        "category": "Workflow",
        "type": "enum",
        "options": [
            "Early Stage Collection",
            "Progressive Reminder",
            "Promise-to-Pay Follow-Up",
            "Payment Plan Monitoring",
            "Escalated Collection",
        ],
    },
    {"label": "Workflow Stage", "category": "Workflow", "type": "text"},
    {
        "label": "Channel",
        "category": "Communication",
        "type": "enum",
        "options": ["Email", "SMS", "WhatsApp"],
    },
    {"label": "Messages Sent (7 days)", "category": "Communication", "type": "number"},
    {"label": "Customer Reply Content", "category": "Communication", "type": "text"},
    {
        "label": "Customer Risk Level",
        "category": "Customer Risk",
        "type": "enum",
        "options": ["Low", "Medium", "High"],
    },
    {"label": "Dispute Flag", "category": "Customer Risk", "type": "enum", "options": ["Yes", "No"]},
    {"label": "AI Confidence", "category": "AI Confidence", "type": "percent"},
    {"label": "AI Recommendation Type", "category": "AI Confidence", "type": "text"},
]


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "rule"


def _stamp_date() -> str:
    return datetime.now(timezone.utc).strftime("%d %b %Y")


def _condition_summary(conditions: list[dict], logic: str) -> str:
    if not conditions:
        return "No conditions configured"
    joiner = " AND " if logic == "ALL" else " OR "
    parts = []
    for c in conditions:
        field = c.get("field") or ""
        operator = (c.get("operator") or "").lower()
        value = c.get("value") or "—"
        parts.append(f"{field} {operator} {value}")
    return joiner.join(parts)


class PayflowRuleService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def catalog(self) -> dict[str, Any]:
        return {
            "categories": RULE_CATEGORIES,
            "actions": RULE_ACTIONS,
            "fields": RULE_FIELDS,
            "statuses": [s.value for s in PayflowRuleStatus],
            "types": [t.value for t in PayflowRuleType],
            "logics": [l.value for l in PayflowRuleLogic],
        }

    def _visible_client_ids(self, user: UserModel) -> list[int] | None:
        if self.access.is_operations_admin(user):
            return None
        ctx = self.access.build_context(user)
        return list(ctx.get("client_ids") or [])

    def can_create_for_client(self, user: UserModel, client_id: int) -> bool:
        if self.access.is_operations_admin(user):
            return True
        return self.access.can(user, "create_edit_client_rules", client_id)

    def can_edit_rule(self, user: UserModel, rule: PayflowRuleModel) -> bool:
        if self.access.is_operations_admin(user):
            return True
        if rule.rule_type == PayflowRuleType.SYSTEM.value or rule.client_id is None:
            return False
        return self.can_create_for_client(user, rule.client_id)

    def _assert_view(self, user: UserModel, rule: PayflowRuleModel) -> None:
        if self.access.is_operations_admin(user):
            return
        allowed = self._visible_client_ids(user) or []
        if rule.client_id is None:
            # System rules visible if user can view rules on any assigned client
            if not any(self.access.can(user, "view_rules", cid) for cid in allowed):
                raise ForbiddenError("Rule access denied")
            return
        if rule.client_id not in allowed:
            raise ForbiddenError("Rule access denied")
        if not self.access.can(user, "view_rules", rule.client_id):
            raise ForbiddenError("Rule access denied")

    def _governance_usage_by_rule_name(self) -> dict[str, list[str]]:
        """Map rule name → client names that selected it under governance_rules."""
        usage: dict[str, list[str]] = {}
        clients = self.db.query(PayflowClientModel).all()
        for client in clients:
            for rule_name in client.governance_rules or []:
                if not rule_name or not client.name:
                    continue
                bucket = usage.setdefault(str(rule_name), [])
                if client.name not in bucket:
                    bucket.append(client.name)
        for names in usage.values():
            names.sort()
        return usage

    def _applied_to_names(
        self,
        row: PayflowRuleModel,
        *,
        governance_usage: dict[str, list[str]] | None = None,
    ) -> list[str]:
        """Client display names where this rule applies."""
        if row.rule_type == PayflowRuleType.CLIENT.value or row.client_id is not None:
            client = row.client
            if client and client.name:
                return [client.name]
            return []
        usage = governance_usage if governance_usage is not None else self._governance_usage_by_rule_name()
        return list(usage.get(row.name or "", []))

    def _serialize(
        self,
        row: PayflowRuleModel,
        user: UserModel | None = None,
        *,
        governance_usage: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        client = row.client
        conditions = list(row.conditions or [])
        data = {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "description": row.description,
            "rule_type": row.rule_type,
            "client_id": row.client_id,
            "client_code": client.code if client else None,
            "client_name": client.name if client else None,
            "category": row.category,
            "logic": row.logic,
            "conditions": conditions,
            "condition_summary": _condition_summary(conditions, row.logic),
            "action": row.action,
            "status": row.status,
            "created_by": row.created_by,
            "triggers_7d": row.triggers_7d or 0,
            "applied_to": self._applied_to_names(row, governance_usage=governance_usage),
            "history": list(row.history or []),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "last_updated_label": row.updated_at.strftime("%d %b %Y") if row.updated_at else None,
        }
        if user is not None:
            data["can_edit"] = self.can_edit_rule(user, row)
        return data

    def list_rules(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        status: str | None = None,
        category: str | None = None,
        action: str | None = None,
        rule_type: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        allowed = self._visible_client_ids(user)
        q = self.db.query(PayflowRuleModel).options(joinedload(PayflowRuleModel.client))
        if allowed is not None:
            if not allowed:
                return self._empty_list(user)
            # System rules + client rules in scope
            q = q.filter(
                (PayflowRuleModel.client_id.is_(None))
                | (PayflowRuleModel.client_id.in_(allowed))
            )
        if client_id is not None:
            # This client's own rules + system rules assigned on the client (governance_rules).
            # Do not return every system rule — only those selected for this client.
            q = q.filter(
                (PayflowRuleModel.client_id == client_id)
                | (PayflowRuleModel.client_id.is_(None))
            )
        if status:
            q = q.filter(PayflowRuleModel.status == status)
        if category:
            q = q.filter(PayflowRuleModel.category == category)
        if action:
            q = q.filter(PayflowRuleModel.action == action)
        if rule_type:
            q = q.filter(PayflowRuleModel.rule_type == rule_type)

        rows = q.order_by(PayflowRuleModel.name.asc()).all()
        if client_id is not None:
            client = (
                self.db.query(PayflowClientModel)
                .filter(PayflowClientModel.id == int(client_id))
                .first()
            )
            assigned = {
                str(name)
                for name in (client.governance_rules or [])
                if name
            } if client is not None else set()
            rows = [
                r
                for r in rows
                if r.client_id == int(client_id)
                or (
                    (r.rule_type == PayflowRuleType.SYSTEM.value or r.client_id is None)
                    and (r.name or "") in assigned
                )
            ]
        if not self.access.is_operations_admin(user):
            filtered = []
            for r in rows:
                try:
                    self._assert_view(user, r)
                    filtered.append(r)
                except ForbiddenError:
                    continue
            rows = filtered

        if search:
            term = search.strip().lower()
            rows = [
                r
                for r in rows
                if term in (r.name or "").lower()
                or term in (r.description or "").lower()
                or term in (r.action or "").lower()
            ]

        can_create = self.access.is_operations_admin(user)
        if not can_create and allowed:
            can_create = any(self.can_create_for_client(user, cid) for cid in allowed)

        summary = {
            "active": sum(1 for r in rows if r.status == PayflowRuleStatus.ACTIVE.value),
            "system": sum(1 for r in rows if r.rule_type == PayflowRuleType.SYSTEM.value),
            "client": sum(1 for r in rows if r.rule_type == PayflowRuleType.CLIENT.value),
            "triggers_7d": sum(r.triggers_7d or 0 for r in rows),
        }
        governance_usage = self._governance_usage_by_rule_name()
        return {
            "rules": [
                self._serialize(r, user, governance_usage=governance_usage) for r in rows
            ],
            "summary": summary,
            "can_create": can_create,
            **self.catalog(),
        }

    def get_rule(self, user: UserModel, rule_id: int) -> dict[str, Any]:
        row = self._get_row(rule_id)
        self._assert_view(user, row)
        data = self._serialize(row, user)
        # Recent triggers from reviews
        reviews = (
            self.db.query(PayflowHumanReviewModel)
            .options(
                joinedload(PayflowHumanReviewModel.account),
                joinedload(PayflowHumanReviewModel.client),
            )
            .filter(PayflowHumanReviewModel.rule_id == row.id)
            .order_by(PayflowHumanReviewModel.created_at.desc())
            .limit(10)
            .all()
        )
        data["recent_triggers"] = [
            {
                "review_id": rev.id,
                "review_code": rev.code,
                "customer_name": rev.account.customer_name if rev.account else None,
                "account_reference": rev.account.account_reference if rev.account else None,
                "client_name": rev.client.name if rev.client else None,
                "status": rev.status,
                "waiting_minutes": rev.waiting_minutes,
            }
            for rev in reviews
        ]
        return data

    def _validate_rule_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValidationAppError("Rule name is required")
        category = payload.get("category") or ""
        if category not in RULE_CATEGORIES:
            raise ValidationAppError("Invalid category")
        action = payload.get("action") or ""
        if action not in RULE_ACTIONS:
            raise ValidationAppError("Invalid action")
        logic = payload.get("logic") or PayflowRuleLogic.ALL.value
        if logic not in {l.value for l in PayflowRuleLogic}:
            raise ValidationAppError("Invalid logic")
        conditions = payload.get("conditions") or []
        if not isinstance(conditions, list) or not conditions:
            raise ValidationAppError("At least one condition is required")
        for c in conditions:
            if not (c.get("field") and c.get("operator") and str(c.get("value", "")).strip()):
                raise ValidationAppError("Each condition needs field, operator and value")
        status = payload.get("status") or PayflowRuleStatus.DRAFT.value
        if status not in {s.value for s in PayflowRuleStatus}:
            raise ValidationAppError("Invalid status")
        return {
            "name": name,
            "description": (payload.get("description") or "").strip() or None,
            "category": category,
            "action": action,
            "logic": logic,
            "conditions": conditions,
            "status": status,
        }

    def create_rule(self, user: UserModel, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a rule, or update when payload includes ``id`` (same POST /rules)."""
        rule_id = payload.get("id")
        if rule_id is not None:
            return self.update_rule(user, int(rule_id), payload)

        fields = self._validate_rule_payload(payload)
        rule_type = payload.get("rule_type") or PayflowRuleType.CLIENT.value
        if rule_type not in {t.value for t in PayflowRuleType}:
            raise ValidationAppError("Invalid rule type")
        client_id = payload.get("client_id")
        if rule_type == PayflowRuleType.SYSTEM.value:
            if not self.access.is_operations_admin(user):
                raise ForbiddenError("Only Operations Admin can create system rules")
            client_id = None
        else:
            if client_id is None:
                raise ValidationAppError("Client is required for client rules")
            if not self.can_create_for_client(user, int(client_id)):
                raise ForbiddenError("Create / Edit Client Rules permission required")
            client = (
                self.db.query(PayflowClientModel)
                .filter(PayflowClientModel.id == int(client_id))
                .first()
            )
            if not client:
                raise NotFoundError("Client not found")

        code = self._unique_code(_slugify(fields["name"]))
        applied_to: list[str] = []
        if client_id is not None:
            client = (
                self.db.query(PayflowClientModel)
                .filter(PayflowClientModel.id == int(client_id))
                .first()
            )
            if client and client.name:
                applied_to = [client.name]

        row = PayflowRuleModel(
            code=code,
            name=fields["name"],
            description=fields["description"],
            rule_type=rule_type,
            client_id=int(client_id) if client_id is not None else None,
            category=fields["category"],
            logic=fields["logic"],
            conditions=fields["conditions"],
            action=fields["action"],
            status=fields["status"],
            created_by=user.full_name,
            triggers_7d=0,
            applied_to=applied_to,
            history=[
                {
                    "at": _stamp_date(),
                    "change": (
                        "Rule activated"
                        if fields["status"] == PayflowRuleStatus.ACTIVE.value
                        else "Draft created"
                    ),
                    "by": user.full_name,
                }
            ],
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        if row.status == PayflowRuleStatus.ACTIVE.value:
            self._enqueue_retroactive_human_reviews(row, actor_name=user.full_name)
        return self._serialize(row, user)

    def update_rule(
        self, user: UserModel, rule_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        row = self._get_row(rule_id)
        if not self.can_edit_rule(user, row):
            raise ForbiddenError("Create / Edit Client Rules permission required")

        fields = self._validate_rule_payload(payload)
        prev_status = row.status

        # Client / system ownership stays on the existing rule (not reassigned via edit).
        row.name = fields["name"]
        row.description = fields["description"]
        row.category = fields["category"]
        row.logic = fields["logic"]
        row.conditions = fields["conditions"]
        row.action = fields["action"]
        row.status = fields["status"]

        if row.client_id is not None:
            client = (
                self.db.query(PayflowClientModel)
                .filter(PayflowClientModel.id == row.client_id)
                .first()
            )
            row.applied_to = [client.name] if client and client.name else list(row.applied_to or [])

        history = list(row.history or [])
        if prev_status != fields["status"]:
            if fields["status"] == PayflowRuleStatus.ACTIVE.value:
                change = "Rule activated"
            elif fields["status"] == PayflowRuleStatus.INACTIVE.value:
                change = "Rule deactivated"
            else:
                change = f"Status set to {fields['status']}"
        else:
            change = "Rule updated"
        history.append({"at": _stamp_date(), "change": change, "by": user.full_name})
        row.history = history

        self.db.commit()
        self.db.refresh(row)
        # Re-scan on save while Active so condition edits (and prior failed
        # matches) pick up existing accounts. No-op unless action is RHR.
        if row.status == PayflowRuleStatus.ACTIVE.value:
            self._enqueue_retroactive_human_reviews(row, actor_name=user.full_name)
        return self._serialize(row, user)

    def activate(self, user: UserModel, rule_id: int) -> dict[str, Any]:
        row = self._get_row(rule_id)
        if not self.can_edit_rule(user, row):
            raise ForbiddenError("Create / Edit Client Rules permission required")
        if row.status == PayflowRuleStatus.ACTIVE.value:
            # Idempotent re-scan: recovers accounts that should already have been queued.
            self._enqueue_retroactive_human_reviews(row, actor_name=user.full_name)
            return self._serialize(row, user)
        row.status = PayflowRuleStatus.ACTIVE.value
        history = list(row.history or [])
        history.append({"at": _stamp_date(), "change": "Rule activated", "by": user.full_name})
        row.history = history
        self.db.commit()
        self.db.refresh(row)
        self._enqueue_retroactive_human_reviews(row, actor_name=user.full_name)
        return self._serialize(row, user)

    def deactivate(self, user: UserModel, rule_id: int) -> dict[str, Any]:
        row = self._get_row(rule_id)
        if not self.can_edit_rule(user, row):
            raise ForbiddenError("Create / Edit Client Rules permission required")
        if row.status != PayflowRuleStatus.ACTIVE.value:
            raise ValidationAppError("Only active rules can be deactivated")
        row.status = PayflowRuleStatus.INACTIVE.value
        history = list(row.history or [])
        history.append({"at": _stamp_date(), "change": "Rule deactivated", "by": user.full_name})
        row.history = history
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(row, user)

    def delete_rule(self, user: UserModel, rule_id: int, *, source: str | None = None) -> dict[str, Any]:
        if not self.access.is_operations_admin(user):
            raise ForbiddenError("Operations Admin access required")
        row = self._get_row(rule_id)
        snapshot = snapshot_model(row)
        related: list[dict[str, str]] = []
        reviews = (
            self.db.query(PayflowHumanReviewModel)
            .filter(PayflowHumanReviewModel.rule_id == row.id)
            .all()
        )
        for review in reviews:
            related.append({"entity_type": "review_unlink", "id": str(review.id), "label": review.code})
            review.rule_id = None
        record_deletion(
            self.db,
            actor=user,
            module="payflow",
            entity_type="rule",
            entity_id=row.id,
            entity_label=row.name,
            source=source,
            record_snapshot=snapshot,
            related_deleted=related,
        )
        self.db.delete(row)
        self.db.commit()
        return {"message": "Rule deleted"}

    def _enqueue_retroactive_human_reviews(
        self, rule: PayflowRuleModel, *, actor_name: str
    ) -> int:
        """
        When a Require Human Review rule becomes Active, scan existing accounts now
        (not only on the next workflow step). Same rule+account open reviews are skipped.
        Does not change the worker step-time path.
        """
        if rule.status != PayflowRuleStatus.ACTIVE.value:
            return 0
        if rule.action != "Require Human Review":
            return 0

        from app.modules.payflow.services.review_service import (
            OPEN_STATUSES,
            PayflowReviewService,
        )
        from app.modules.payflow.services.rule_runtime_service import RuleRuntimeService

        runtime = RuleRuntimeService(self.db)
        review_svc = PayflowReviewService(self.db)
        created = 0
        for account in runtime.candidate_accounts_for_rule(rule):
            if not runtime.matches_rule(rule, account):
                continue
            already_open = (
                self.db.query(PayflowHumanReviewModel)
                .filter(
                    PayflowHumanReviewModel.account_id == account.id,
                    PayflowHumanReviewModel.rule_id == rule.id,
                    PayflowHumanReviewModel.status.in_(list(OPEN_STATUSES)),
                )
                .first()
            )
            if already_open:
                continue
            review_svc.enqueue_from_rule(
                account_id=account.id,
                rule_id=rule.id,
                observed_value=f"${float(account.outstanding_balance or 0):,.2f} outstanding",
                actor_name=actor_name or "Rule activation scan",
                commit=False,
                notify=False,
            )
            created += 1
        if created:
            self.db.commit()
        return created

    def _get_row(self, rule_id: int) -> PayflowRuleModel:
        row = (
            self.db.query(PayflowRuleModel)
            .options(joinedload(PayflowRuleModel.client))
            .filter(PayflowRuleModel.id == rule_id)
            .first()
        )
        if not row:
            raise NotFoundError("Rule not found")
        return row

    def _unique_code(self, base: str) -> str:
        code = base
        n = 2
        while self.db.query(PayflowRuleModel).filter(PayflowRuleModel.code == code).first():
            code = f"{base}-{n}"
            n += 1
        return code

    def _empty_list(self, user: UserModel) -> dict[str, Any]:
        return {
            "rules": [],
            "summary": {"active": 0, "system": 0, "client": 0, "triggers_7d": 0},
            "can_create": self.access.is_operations_admin(user),
            **self.catalog(),
        }
