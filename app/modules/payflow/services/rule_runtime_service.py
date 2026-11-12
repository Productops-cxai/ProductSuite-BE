"""Evaluate active governance rules against an account at workflow step time."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowPaymentAttemptModel,
    PayflowPaymentPlanInstallmentModel,
    PayflowPaymentPlanModel,
    PayflowRuleModel,
)
from app.shared.enums import (
    PayflowCollectionStatus,
    PayflowCommStatus,
    PayflowPaymentAttemptStatus,
    PayflowPaymentInstallmentStatus,
    PayflowPaymentPlanStatus,
    PayflowRuleLogic,
    PayflowRuleStatus,
    PayflowRuleType,
)


# Messages that never left PayFlow (blocked by governance / eligibility, cancelled).
_NOT_SENT_STATUSES = (
    PayflowCommStatus.SUPPRESSED.value,
    PayflowCommStatus.CANCELLED.value,
    PayflowCommStatus.PREPARED.value,
    PayflowCommStatus.AWAITING_GOVERNANCE.value,
)
_MISSED_INSTALLMENT_STATUSES = (
    PayflowPaymentInstallmentStatus.OVERDUE.value,
    PayflowPaymentInstallmentStatus.FAILED.value,
)
MESSAGES_SENT_7D_FIELD = "Messages Sent (7 days)"


def _db(ctx: dict | None) -> Session | None:
    return (ctx or {}).get("db")


def _unsuccessful_attempts(account: PayflowAccountModel, ctx: dict | None) -> int:
    """Messages that were attempted but failed (bounce, undelivered, provider error)."""
    db = _db(ctx)
    if db is None:
        return 0
    return (
        db.query(PayflowCommunicationModel.id)
        .filter(
            PayflowCommunicationModel.account_id == account.id,
            PayflowCommunicationModel.status == PayflowCommStatus.FAILED.value,
        )
        .count()
    )


def _messages_sent_7d(account: PayflowAccountModel, ctx: dict | None) -> int:
    """Messages that really went out in the last 7 days."""
    db = _db(ctx)
    if db is None:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    return (
        db.query(PayflowCommunicationModel.id)
        .filter(
            PayflowCommunicationModel.account_id == account.id,
            PayflowCommunicationModel.created_at >= cutoff,
            PayflowCommunicationModel.status.notin_(
                _NOT_SENT_STATUSES + (PayflowCommStatus.FAILED.value,)
            ),
        )
        .count()
    )


def _failed_payment_attempts(account: PayflowAccountModel, ctx: dict | None) -> int:
    db = _db(ctx)
    if db is None:
        return 0
    return (
        db.query(PayflowPaymentAttemptModel.id)
        .filter(
            PayflowPaymentAttemptModel.account_id == account.id,
            PayflowPaymentAttemptModel.status == PayflowPaymentAttemptStatus.FAILED.value,
        )
        .count()
    )


def _current_plan(account: PayflowAccountModel, ctx: dict | None) -> PayflowPaymentPlanModel | None:
    """The account's latest plan that is still running (not completed / cancelled)."""
    db = _db(ctx)
    if db is None:
        return None
    return (
        db.query(PayflowPaymentPlanModel)
        .filter(
            PayflowPaymentPlanModel.account_id == account.id,
            PayflowPaymentPlanModel.status.notin_(
                [
                    PayflowPaymentPlanStatus.COMPLETED.value,
                    PayflowPaymentPlanStatus.CANCELLED.value,
                ]
            ),
        )
        .order_by(PayflowPaymentPlanModel.id.desc())
        .first()
    )


def _missed_installments(account: PayflowAccountModel, ctx: dict | None) -> int:
    plan = _current_plan(account, ctx)
    if plan is None:
        return 0
    return (
        _db(ctx)
        .query(PayflowPaymentPlanInstallmentModel.id)
        .filter(
            PayflowPaymentPlanInstallmentModel.plan_id == plan.id,
            PayflowPaymentPlanInstallmentModel.status.in_(_MISSED_INSTALLMENT_STATUSES),
        )
        .count()
    )


def _payment_plan_status(account: PayflowAccountModel, ctx: dict | None) -> str:
    """None / On Track / At Risk / Defaulted — the rule builder's options."""
    plan = _current_plan(account, ctx)
    if plan is None:
        return "None"
    if plan.status == PayflowPaymentPlanStatus.DEFAULTED.value:
        return "Defaulted"
    if plan.status == PayflowPaymentPlanStatus.OVERDUE.value or _missed_installments(account, ctx):
        return "At Risk"
    return "On Track"


def _total_contact_attempts(account: PayflowAccountModel, ctx: dict | None) -> int:
    """Messages that went out to the customer (any time, any channel), failed ones included."""
    db = _db(ctx)
    if db is None:
        return 0
    return (
        db.query(PayflowCommunicationModel.id)
        .filter(
            PayflowCommunicationModel.account_id == account.id,
            PayflowCommunicationModel.status.notin_(_NOT_SENT_STATUSES),
        )
        .count()
    )


def _days_since(day: date | None) -> int | None:
    return (datetime.now(timezone.utc).date() - day).days if day else None


def _customer_opted_out(account: PayflowAccountModel, ctx: dict | None) -> str:
    """Yes when the customer opted out of the channel being used (either channel when none is given)."""
    channel = str((ctx or {}).get("channel") or "").strip().lower()
    email_out = account.email_consent is False
    sms_out = bool(account.sms_opt_out)
    if channel == "email":
        return "Yes" if email_out else "No"
    if channel == "sms":
        return "Yes" if sms_out else "No"
    return "Yes" if email_out or sms_out else "No"


def _latest_attempt(account: PayflowAccountModel, ctx: dict | None) -> PayflowPaymentAttemptModel | None:
    db = _db(ctx)
    if db is None:
        return None
    return (
        db.query(PayflowPaymentAttemptModel)
        .filter(PayflowPaymentAttemptModel.account_id == account.id)
        .order_by(PayflowPaymentAttemptModel.id.desc())
        .first()
    )


def _payment_processing_status(account: PayflowAccountModel, ctx: dict | None) -> str:
    """The customer's latest payment: Pending (started, no outcome yet), Succeeded, Failed or None."""
    attempt = _latest_attempt(account, ctx)
    if attempt is None:
        return "None"
    if attempt.status in (PayflowPaymentAttemptStatus.INITIATED.value, PayflowPaymentAttemptStatus.PROCESSING.value):
        return "Pending"
    if attempt.status == PayflowPaymentAttemptStatus.SUCCEEDED.value:
        return "Succeeded"
    if attempt.status == PayflowPaymentAttemptStatus.FAILED.value:
        return "Failed"
    return "None"  # cancelled / expired checkout: nothing in progress


def _payment_status(account: PayflowAccountModel, ctx: dict | None) -> str:
    """Paid (nothing left to pay), Failed (the latest payment failed), Partially Paid, or Unpaid."""
    if float(account.outstanding_balance or 0) <= 0:
        return "Paid"
    if _payment_processing_status(account, ctx) == "Failed":
        return "Failed"
    if float(account.recovered_balance or 0) > 0:
        return "Partially Paid"
    return "Unpaid"


FIELD_GETTERS: dict[str, Any] = {
    "Outstanding Balance": lambda a, _: float(a.outstanding_balance or 0),
    "Original Balance": lambda a, _: float(a.original_balance or 0),
    "Recovered Amount": lambda a, _: float(a.recovered_balance or 0),
    "Channel": lambda _a, ctx: (ctx or {}).get("channel") or "",
    "Unsuccessful Attempts": _unsuccessful_attempts,
    "Failed Payment Attempts": _failed_payment_attempts,
    "Payment Plan Status": _payment_plan_status,
    "Missed Installments": _missed_installments,
    MESSAGES_SENT_7D_FIELD: _messages_sent_7d,
    "Total Contact Attempts": _total_contact_attempts,
    "Days Since Last Contact": lambda a, _: _days_since(a.last_contact_date),
    "Days Past Due": lambda a, _: a.days_past_due,
    "Last Payment Amount": lambda a, _: a.last_payment_amount,
    "Customer Opted Out": _customer_opted_out,
    "Payment Status": _payment_status,
    "Payment Processing Status": _payment_processing_status,
}

# What the runtime does when a rule matches (in this order of precedence, see first_blocking_action).
RUNTIME_ACTIONS = (
    "Prevent Communication",
    "Hold Action",
    "Require Human Review",
    "Escalate Case",
    "Request Reassessment",
    "Apply / Change Workflow",
)


def unenforced_parts(conditions: list[dict] | None, action: str | None) -> list[str]:
    """The parts of a rule PayFlow cannot check or carry out yet ([] = the rule works as written)."""
    problems = [
        f'condition field "{field}"'
        for field in dict.fromkeys(
            str(c.get("field") or "") for c in conditions or [] if isinstance(c, dict)
        )
        if field not in FIELD_GETTERS
    ]
    if action and action not in RUNTIME_ACTIONS:
        problems.append(f'action "{action}"')
    return problems


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
        account = self.db.get(PayflowAccountModel, account_id)
        return _messages_sent_7d(account, {"db": self.db}) if account else 0

    def evaluate(
        self,
        account: PayflowAccountModel,
        *,
        channel: str | None = None,
        for_communication: bool = False,
    ) -> list[dict[str, Any]]:
        """Return list of matched rules with action, ordered by id."""
        ctx = {"channel": channel or "", "db": self.db}
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
        exclude_actions: set[str] | frozenset[str] | None = None,
    ) -> dict[str, Any] | None:
        """
        Priority: Prevent Communication (comms only) → Hold Action → Require Human Review
        → Escalate / Reassessment / Change Workflow.

        Require Human Review and Escalate Case only apply on Communication steps
        (before Email / SMS is sent) — not on Trigger / Wait / Condition / etc.
        """
        matched = self.evaluate(account, channel=channel, for_communication=for_communication)
        if exclude_actions:
            matched = [m for m in matched if m.get("action") not in exclude_actions]
        if not matched:
            return None
        priority = list(RUNTIME_ACTIONS)
        # Only pause for review right before a message is sent — not on every workflow step.
        communication_only = frozenset(
            {"Prevent Communication", "Require Human Review", "Escalate Case"}
        )
        for action in priority:
            if for_communication is False and action in communication_only:
                continue
            for m in matched:
                if m["action"] == action:
                    return m
        return None

    def matches_rule(
        self,
        rule: PayflowRuleModel,
        account: PayflowAccountModel,
        *,
        channel: str | None = None,
    ) -> bool:
        """Whether a single rule's conditions match the account (current snapshot)."""
        ctx = {"channel": channel or "", "db": self.db}
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
    # UI labels (RuleNewPage NUMERIC_OPS / TEXT_OPS / ENUM_OPS) are stored as-is and
    # lowercased by callers — keep aliases for both "… or equal" and "… or equal to".
    op = operator.replace("_", " ").strip().lower()
    try:
        if op in ("=", "==", "equals", "is", "equal to"):
            return _coerce_eq(actual, expected)
        if op in (
            "!=",
            "<>",
            "not equals",
            "does not equal",
            "is not",
            "not equal to",
        ):
            return not _coerce_eq(actual, expected)
        if op in (">", "greater than"):
            return float(actual) > float(expected)
        if op in (">=", "greater than or equal", "greater than or equal to"):
            return float(actual) >= float(expected)
        if op in ("<", "less than"):
            return float(actual) < float(expected)
        if op in ("<=", "less than or equal", "less than or equal to"):
            return float(actual) <= float(expected)
        if op in ("contains",):
            return str(expected).lower() in str(actual).lower()
        if op in ("does not contain", "not contains"):
            return str(expected).lower() not in str(actual).lower()
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
