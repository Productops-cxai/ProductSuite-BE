"""Evaluate active governance rules against an account at workflow step time."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowRuleModel,
)
from app.shared.enums import (
    PayflowCollectionStatus,
    PayflowRuleLogic,
    PayflowRuleStatus,
    PayflowRuleType,
)


FIELD_GETTERS: dict[str, Any] = {
    "Outstanding Balance": lambda a, _: float(a.outstanding_balance or 0),
    "Original Balance": lambda a, _: float(a.original_balance or 0),
    "Recovered Amount": lambda a, _: float(a.recovered_balance or 0),
    "Days Past Due": lambda a, _: int(a.days_past_due or 0),
    "Last Payment Amount": lambda a, _: float(a.last_payment_amount or 0),
    "Workflow": lambda a, _: a.current_workflow or "",
    "Channel": lambda _a, ctx: (ctx or {}).get("channel") or "",
    "Messages Sent (7 days)": lambda a, ctx: (ctx or {}).get("messages_7d", 0),
    "Dispute Flag": lambda a, _: "Yes" if a.provincial_hold else "No",
}


class RuleRuntimeService:
    def __init__(self, db: Session):
        self.db = db

    def active_rules_for_account(self, account: PayflowAccountModel) -> list[PayflowRuleModel]:
        client = self.db.get(PayflowClientModel, account.client_id)
        assigned_system = {
            str(name) for name in ((client.governance_rules if client else None) or []) if name
        }
        rows = (
            self.db.query(PayflowRuleModel)
            .filter(PayflowRuleModel.status == PayflowRuleStatus.ACTIVE.value)
            .filter(
                (PayflowRuleModel.rule_type == PayflowRuleType.SYSTEM.value)
                | (
                    (PayflowRuleModel.rule_type == PayflowRuleType.CLIENT.value)
                    & (PayflowRuleModel.client_id == account.client_id)
                )
            )
            .order_by(PayflowRuleModel.id.asc())
            .all()
        )
        # System rules apply only when selected on the client's governance settings.
        return [
            r
            for r in rows
            if r.rule_type == PayflowRuleType.CLIENT.value
            or (r.name or "") in assigned_system
        ]

    def messages_sent_7d(self, account_id: int) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
        # Lightweight count — filter in Python if needed for SQLite compat; Postgres OK with created_at.
        rows = (
            self.db.query(PayflowCommunicationModel)
            .filter(PayflowCommunicationModel.account_id == account_id)
            .all()
        )
        count = 0
        for r in rows:
            if r.created_at and r.created_at.timestamp() >= cutoff:
                count += 1
        return count

    def evaluate(
        self,
        account: PayflowAccountModel,
        *,
        channel: str | None = None,
        for_communication: bool = False,
    ) -> list[dict[str, Any]]:
        """Return list of matched rules with action, ordered by id."""
        ctx = {
            "channel": channel or "",
            "messages_7d": self.messages_sent_7d(account.id),
        }
        matched: list[dict[str, Any]] = []
        for rule in self.active_rules_for_account(account):
            if for_communication and rule.action == "Prevent Communication":
                pass  # always evaluate
            if not self._rule_matches(rule, account, ctx):
                continue
            matched.append(
                {
                    "rule_id": rule.id,
                    "name": rule.name,
                    "action": rule.action,
                    "code": rule.code,
                }
            )
        return matched

    def first_blocking_action(
        self,
        account: PayflowAccountModel,
        *,
        channel: str | None = None,
        for_communication: bool = False,
    ) -> dict[str, Any] | None:
        """
        Priority: Prevent Communication (comms only) → Hold Action → Require Human Review
        → Escalate / Reassessment / Change Workflow.
        """
        matched = self.evaluate(account, channel=channel, for_communication=for_communication)
        if not matched:
            return None
        priority = [
            "Prevent Communication",
            "Hold Action",
            "Require Human Review",
            "Escalate Case",
            "Request Reassessment",
            "Apply / Change Workflow",
        ]
        for action in priority:
            if for_communication is False and action == "Prevent Communication":
                continue
            for m in matched:
                if m["action"] == action:
                    return m
        return matched[0]

    def matches_rule(
        self,
        rule: PayflowRuleModel,
        account: PayflowAccountModel,
        *,
        channel: str | None = None,
    ) -> bool:
        """Whether a single rule's conditions match the account (current snapshot)."""
        ctx = {
            "channel": channel or "",
            "messages_7d": self.messages_sent_7d(account.id),
        }
        return self._rule_matches(rule, account, ctx)

    def candidate_accounts_for_rule(self, rule: PayflowRuleModel) -> list[PayflowAccountModel]:
        """
        Accounts that could be evaluated for this rule on activation/create.
        Client rules → that client's accounts; system rules → clients that assigned the rule.
        Resolved accounts are skipped (no retroactive exception queue for closed cases).
        """
        q = self.db.query(PayflowAccountModel).filter(
            PayflowAccountModel.collection_status != PayflowCollectionStatus.RESOLVED.value
        )
        if rule.rule_type == PayflowRuleType.CLIENT.value:
            if rule.client_id is None:
                return []
            q = q.filter(PayflowAccountModel.client_id == rule.client_id)
        else:
            clients = self.db.query(PayflowClientModel).all()
            client_ids = [
                c.id
                for c in clients
                if (rule.name or "")
                in {str(name) for name in (c.governance_rules or []) if name}
            ]
            if not client_ids:
                return []
            q = q.filter(PayflowAccountModel.client_id.in_(client_ids))
        return q.order_by(PayflowAccountModel.id.asc()).all()

    def _rule_matches(self, rule: PayflowRuleModel, account: PayflowAccountModel, ctx: dict) -> bool:
        conditions = list(rule.conditions or [])
        if not conditions:
            return False
        results = [self._condition_matches(c, account, ctx) for c in conditions if isinstance(c, dict)]
        if not results:
            return False
        if (rule.logic or PayflowRuleLogic.ALL.value) == PayflowRuleLogic.ANY.value:
            return any(results)
        return all(results)

    def _condition_matches(self, condition: dict, account: PayflowAccountModel, ctx: dict) -> bool:
        field = condition.get("field") or ""
        operator = (condition.get("operator") or "").strip().lower()
        expected = condition.get("value")
        getter = FIELD_GETTERS.get(field)
        if not getter:
            return False
        actual = getter(account, ctx)
        return _compare(actual, operator, expected)


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    op = operator.replace("_", " ").strip()
    try:
        if op in ("=", "==", "equals", "is", "equal to"):
            return _coerce_eq(actual, expected)
        if op in ("!=", "<>", "not equals", "is not", "not equal to"):
            return not _coerce_eq(actual, expected)
        if op in (">", "greater than"):
            return float(actual) > float(expected)
        if op in (">=", "greater than or equal"):
            return float(actual) >= float(expected)
        if op in ("<", "less than"):
            return float(actual) < float(expected)
        if op in ("<=", "less than or equal"):
            return float(actual) <= float(expected)
        if op in ("contains",):
            return str(expected).lower() in str(actual).lower()
        if op in ("in",):
            options = [x.strip() for x in str(expected).split(",")]
            return str(actual) in options
    except (TypeError, ValueError):
        return False
    return False


def _coerce_eq(actual: Any, expected: Any) -> bool:
    if isinstance(actual, (int, float)) and not isinstance(actual, bool):
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            pass
    return str(actual).strip().lower() == str(expected).strip().lower()
