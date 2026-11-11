"""Resolve AI/catalog Condition attributes for the workflow worker.

AI, FE, and validators store Conditions as:
  config.attribute / operator / value  (e.g. Payment Status / Not Equals / Unpaid)

The worker evaluates those labels against live account / communication / payment state.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowCommunicationModel,
    PayflowHumanReviewModel,
    PayflowPaymentLinkModel,
    PayflowPaymentPlanInstallmentModel,
    PayflowPaymentPlanModel,
    PayflowWorkflowRunModel,
)
from app.shared.enums import (
    PayflowCollectionStatus,
    PayflowCommStatus,
    PayflowPaymentInstallmentStatus,
    PayflowPaymentPlanStatus,
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# A plan the customer is on (Defaulted too: the plan broke down, but it is still their arrangement).
_CURRENT_PLAN_STATUSES = (
    PayflowPaymentPlanStatus.ACTIVE.value,
    PayflowPaymentPlanStatus.OVERDUE.value,
    PayflowPaymentPlanStatus.DEFAULTED.value,
)
# The plan is behind: a missed instalment (same meaning as rule_runtime_service's "Missed Installments").
_BEHIND_PLAN_STATUSES = (PayflowPaymentPlanStatus.OVERDUE.value, PayflowPaymentPlanStatus.DEFAULTED.value)
_MISSED_INSTALMENT_STATUSES = (
    PayflowPaymentInstallmentStatus.OVERDUE.value,
    PayflowPaymentInstallmentStatus.FAILED.value,
)
_EPSILON = 0.0001
_BOUNCE_HINTS = ("bounce", "bounced", "dropped", "invalid", "blocked", "undeliverable")

# Catalog operators (and aliases) → comparable form with spaces.
_OP_ALIASES = {
    "eq": "equals",
    "=": "equals",
    "==": "equals",
    "is": "equals",
    "equal to": "equals",
    "neq": "not equals",
    "!=": "not equals",
    "<>": "not equals",
    "not_equals": "not equals",
    "does not equal": "not equals",
    "is not": "not equals",
    "not equal to": "not equals",
    "gt": "greater than",
    ">": "greater than",
    "gte": "greater than or equal",
    ">=": "greater than or equal",
    "lt": "less than",
    "<": "less than",
    "lte": "less than or equal",
    "<=": "less than or equal",
}


def normalize_operator(operator: str | None) -> str:
    raw = (operator or "equals").strip().lower().replace("_", " ")
    return _OP_ALIASES.get(raw, raw)


def compare_values(actual: Any, operator: str | None, expected: Any) -> bool:
    op = normalize_operator(operator)
    try:
        if op in {"equals", "equal to", "is"}:
            return _eq(actual, expected)
        if op in {"not equals", "does not equal", "is not", "not equal to"}:
            return not _eq(actual, expected)
        if op in {"greater than"}:
            return float(_number(actual)) > float(_number(expected))
        if op in {"greater than or equal", "greater than or equal to"}:
            return float(_number(actual)) >= float(_number(expected))
        if op in {"less than"}:
            return float(_number(actual)) < float(_number(expected))
        if op in {"less than or equal", "less than or equal to"}:
            return float(_number(actual)) <= float(_number(expected))
        if op in {"truthy", "true", "is true"}:
            return bool(actual)
        if op in {"falsy", "false", "is false"}:
            return not bool(actual)
        if op in {"in", "one of"}:
            options = (
                expected
                if isinstance(expected, (list, tuple, set))
                else str(expected).split(",")
            )
            return str(actual).strip().lower() in {
                str(o).strip().lower() for o in options
            }
        if op == "contains":
            return str(expected).lower() in str(actual).lower()
        if op in {"does not contain", "not contains"}:
            return str(expected).lower() not in str(actual).lower()
    except (TypeError, ValueError):
        return False
    return bool(actual)


def resolve_condition_actual(
    db: Session,
    account: PayflowAccountModel,
    attribute_or_field: str,
    *,
    ctx: dict[str, Any] | None = None,
    run: PayflowWorkflowRunModel | None = None,
) -> Any:
    """Map catalog `attribute` (or legacy `field`) to a comparable runtime value.

    `run`: the workflow run checking the Condition ("Payment Status" counts payments since it started).
    """
    ctx = ctx or {}
    key = (attribute_or_field or "").strip()
    if not key:
        return None

    # Event context wins when the key is present (webhook / payment hooks).
    if key in ctx:
        return ctx.get(key)

    lowered = key.lower()
    catalog = {
        "payment status": lambda: payment_status(db, account, run=run),
        "email delivery": lambda: channel_delivery(db, account, "Email", ctx),
        "sms delivery": lambda: channel_delivery(db, account, "SMS", ctx),
        "email address": lambda: contact_quality(account.email, kind="email"),
        "mobile number": lambda: contact_quality(account.phone_mobile, kind="phone"),
        "payment link": lambda: payment_link_status(db, account, ctx),
        "promise-to-pay": lambda: promise_to_pay_status(account),
        "promise to pay": lambda: promise_to_pay_status(account),
        "customer response": lambda: customer_response(db, account, ctx),
        "outstanding balance": lambda: float(account.outstanding_balance or 0),
    }
    if lowered in catalog:
        return catalog[lowered]()

    # Legacy field names used by older steps / event hooks.
    if lowered in {"paid", "has_payment", "payment_received"}:
        if ctx.get("payment_received") or ctx.get("has_payment"):
            return True
        return bool(account.last_payment_date) or float(
            account.outstanding_balance or 0
        ) < float(account.original_balance or 0)
    if lowered == "outstanding_balance":
        return float(account.outstanding_balance or 0)
    if lowered == "collection_status":
        return account.collection_status
    if lowered == "provincial_hold":
        return bool(account.provincial_hold)
    if lowered in {"comm_status", "communication_status", "status"}:
        return ctx.get("comm_status") or ctx.get("status")

    return getattr(account, key, None)


def payment_status(
    db: Session, account: PayflowAccountModel, *, run: PayflowWorkflowRunModel | None = None
) -> str:
    """The catalog's Payment Status: Paid In Full / Payment Plan Active / Partial Payment / Unpaid.

    - On a plan: "Payment Plan Active" while it is on track, "Unpaid" while an instalment is missed
      (so a Payment Plan workflow's "Payment Status Equals Unpaid" finds missed instalments).
    - "Partial Payment" counts payments made since `run` started (its recovered_at_start): a part
      payment from before the workflow is not the customer acting on it. Without that baseline
      (no run, or a run from before it was recorded) any part payment counts, as before.
    """
    outstanding = float(account.outstanding_balance or 0)
    original = float(account.original_balance or 0)
    recovered = float(account.recovered_balance or 0)
    status = (account.collection_status or "").strip().lower()

    if outstanding <= _EPSILON or status in {"resolved", "closed", "paid"}:
        return "Paid In Full"

    plan = _current_plan(db, account.id)
    if status == PayflowCollectionStatus.PAYMENT_PLAN.value.lower() or plan is not None:
        return "Unpaid" if plan is not None and _plan_is_behind(db, plan) else "Payment Plan Active"

    baseline = run.recovered_at_start if run is not None else None
    if baseline is not None:
        return "Partial Payment" if recovered > float(baseline) + _EPSILON else "Unpaid"
    if recovered > _EPSILON or (original > 0 and outstanding + _EPSILON < original):
        return "Partial Payment"

    return "Unpaid"


def channel_delivery(
    db: Session,
    account: PayflowAccountModel,
    channel: str,
    ctx: dict[str, Any],
) -> str | None:
    """Map latest channel communication into catalog Email/SMS Delivery values."""
    ctx_channel = str(ctx.get("channel") or "").strip().lower()
    ctx_status = str(ctx.get("comm_status") or ctx.get("status") or "").strip()
    if ctx_status and (not ctx_channel or ctx_channel == channel.lower()):
        mapped = _map_comm_status(ctx_status, bounce_hint=_ctx_bounce_hint(ctx))
        if mapped:
            return mapped

    row = (
        db.query(PayflowCommunicationModel)
        .filter(
            PayflowCommunicationModel.account_id == account.id,
            PayflowCommunicationModel.channel == channel,
        )
        .order_by(PayflowCommunicationModel.id.desc())
        .first()
    )
    if not row:
        return None
    return _map_comm_status(row.status, bounce_hint=_row_bounce_hint(row))


def contact_quality(value: str | None, *, kind: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return "Missing"
    if kind == "email":
        return "Valid" if _EMAIL_RE.match(raw) else "Invalid"
    digits = re.sub(r"\D", "", raw)
    return "Valid" if len(digits) >= 7 else "Invalid"


def payment_link_status(
    db: Session, account: PayflowAccountModel, ctx: dict[str, Any]
) -> str:
    if str(ctx.get("comm_status") or ctx.get("status") or "") == (
        PayflowCommStatus.PAYMENT_LINK_CLICKED.value
    ):
        return "Clicked"
    if ctx.get("payment_link_clicked") or ctx.get("clicked"):
        return "Clicked"

    link = (
        db.query(PayflowPaymentLinkModel)
        .filter(PayflowPaymentLinkModel.account_id == account.id)
        .order_by(PayflowPaymentLinkModel.id.desc())
        .first()
    )
    if link and (link.last_clicked_at or int(link.clicks_count or 0) > 0):
        return "Clicked"

    comm = (
        db.query(PayflowCommunicationModel)
        .filter(
            PayflowCommunicationModel.account_id == account.id,
            PayflowCommunicationModel.payment_link.is_(True),
        )
        .order_by(PayflowCommunicationModel.id.desc())
        .first()
    )
    if comm and (
        comm.status == PayflowCommStatus.PAYMENT_LINK_CLICKED.value
        or comm.first_clicked_at
        or int(comm.clicks_count or 0) > 0
    ):
        return "Clicked"
    return "Not Clicked"


def promise_to_pay_status(account: PayflowAccountModel) -> str:
    if not account.ptp_due_date and not account.ptp_code and not account.ptp_amount:
        return "None"
    today = date.today()
    due = account.ptp_due_date
    paid = account.last_payment_date
    if paid and due and paid <= due:
        return "Kept"
    if paid and account.last_payment_is_ptp:
        return "Kept"
    if due and due < today and not (
        paid and paid >= due
    ):
        # Past due with no qualifying payment → Broken.
        if not paid or paid < due:
            return "Broken"
    return "Created"


def customer_response(
    db: Session, account: PayflowAccountModel, ctx: dict[str, Any]
) -> str:
    hint = str(ctx.get("customer_response") or ctx.get("response") or "").strip().lower()
    if "dispute" in hint:
        return "Dispute Raised"
    if hint in {"received", "reply", "responded"}:
        return "Received"

    for item in reversed(list(account.timeline or [])[-20:]):
        if not isinstance(item, dict):
            continue
        blob = f"{item.get('label') or ''} {item.get('detail') or ''}".lower()
        if "dispute" in blob:
            return "Dispute Raised"
        if any(w in blob for w in ("customer reply", "customer response", "inbound")):
            return "Received"

    review = (
        db.query(PayflowHumanReviewModel)
        .filter(PayflowHumanReviewModel.account_id == account.id)
        .order_by(PayflowHumanReviewModel.id.desc())
        .first()
    )
    if review:
        reason = f"{review.reason or ''} {review.guidance or ''} {review.final_action or ''}".lower()
        if "dispute" in reason:
            return "Dispute Raised"

    if account.last_contact_date:
        return "Received"
    return "None"


def _current_plan(db: Session, account_id: int | None) -> PayflowPaymentPlanModel | None:
    """The customer's latest plan that is not completed or cancelled."""
    if account_id is None:
        return None
    return (
        db.query(PayflowPaymentPlanModel)
        .filter(
            PayflowPaymentPlanModel.account_id == account_id,
            PayflowPaymentPlanModel.status.in_(_CURRENT_PLAN_STATUSES),
        )
        .order_by(PayflowPaymentPlanModel.id.desc())
        .first()
    )


def _plan_is_behind(db: Session, plan: PayflowPaymentPlanModel) -> bool:
    """A missed instalment: the plan is Overdue / Defaulted, or an instalment is Overdue / Failed.

    Instalments are checked too: paying one missed instalment sets the plan back to Active even
    when another one is still unpaid.
    """
    if plan.status in _BEHIND_PLAN_STATUSES:
        return True
    missed = (
        db.query(PayflowPaymentPlanInstallmentModel.id)
        .filter(
            PayflowPaymentPlanInstallmentModel.plan_id == plan.id,
            PayflowPaymentPlanInstallmentModel.status.in_(_MISSED_INSTALMENT_STATUSES),
        )
        .first()
    )
    return missed is not None


def _map_comm_status(status: str | None, *, bounce_hint: bool) -> str | None:
    s = (status or "").strip()
    if not s:
        return None
    if s == PayflowCommStatus.FAILED.value or s.lower() in {"bounced", "bounce", "failed"}:
        return "Bounced" if bounce_hint or s.lower() in {"bounced", "bounce"} else "Failed"
    if s in {
        PayflowCommStatus.DELIVERED.value,
        PayflowCommStatus.OPENED_READ.value,
        PayflowCommStatus.PAYMENT_LINK_CLICKED.value,
    } or s.lower() in {"delivered", "opened", "opened / read"}:
        return "Delivered"
    if s.lower() == "failed":
        return "Failed"
    return None


def _row_bounce_hint(row: PayflowCommunicationModel) -> bool:
    blob = " ".join(
        str(x or "")
        for x in (
            row.provider_error,
            row.restriction_reason,
            row.engagement,
        )
    ).lower()
    if any(h in blob for h in _BOUNCE_HINTS):
        return True
    for ev in row.events or []:
        if not isinstance(ev, dict):
            continue
        label = f"{ev.get('type') or ''} {ev.get('label') or ''} {ev.get('detail') or ''}".lower()
        if any(h in label for h in _BOUNCE_HINTS):
            return True
    return False


def _ctx_bounce_hint(ctx: dict[str, Any]) -> bool:
    blob = " ".join(
        str(ctx.get(k) or "")
        for k in ("provider_error", "restriction_reason", "event_type", "label", "detail")
    ).lower()
    return any(h in blob for h in _BOUNCE_HINTS)


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "")
    return float(text)


def _eq(actual: Any, expected: Any) -> bool:
    if actual is None and expected is None:
        return True
    if isinstance(actual, bool) or isinstance(expected, bool):
        return bool(actual) == bool(expected)
    # Numeric catalog thresholds ("1,000") vs outstanding balance.
    try:
        if actual is not None and expected is not None:
            a = _number(actual)
            b = _number(expected)
            return abs(a - b) < 0.0001
    except (TypeError, ValueError):
        pass
    return str(actual).strip().lower() == str(expected).strip().lower()


def apply_outcome_to_account(account: PayflowAccountModel, outcome: str | None) -> dict[str, Any]:
    """Side effects when an Outcome step completes the run."""
    label = (outcome or "").strip()
    before = account.collection_status
    lowered = label.lower()
    if lowered in {"paid in full", "case closed"}:
        account.collection_status = PayflowCollectionStatus.RESOLVED.value
        if lowered == "paid in full" and float(account.outstanding_balance or 0) > 0:
            # Record closure without inventing a payment txn — balance already trusted upstream.
            account.recovered_balance = float(account.recovered_balance or 0) + float(
                account.outstanding_balance or 0
            )
            account.outstanding_balance = 0.0
    elif lowered == "payment plan active":
        account.collection_status = PayflowCollectionStatus.PAYMENT_PLAN.value
    elif lowered in {"promise to pay recorded", "promise-to-pay recorded"}:
        account.collection_status = PayflowCollectionStatus.PROMISE_TO_PAY.value
    elif lowered == "no contactable channel":
        account.next_action = "No contactable channel"
        account.human_review = True
    account.last_action = f"Outcome: {label or 'done'}"
    return {
        "outcome": label or None,
        "collection_status_before": before,
        "collection_status": account.collection_status,
    }


def timedelta_for_unit(amount: int, unit: str):
    from datetime import timedelta

    u = (unit or "Days").strip().lower()
    if u.startswith("hour"):
        return timedelta(hours=amount)
    if u.startswith("minute"):
        return timedelta(minutes=amount)
    if u.startswith("week"):
        return timedelta(days=amount * 7)
    return timedelta(days=amount)


def resolve_reference_anchor(
    db: Session,
    account: PayflowAccountModel,
    reference_event: str | None,
    *,
    run_started_at: datetime | None = None,
) -> datetime | None:
    """Best-effort timestamp for Wait/Communication reference_event."""
    ref = (reference_event or "").strip().lower()
    if not ref:
        return None

    def _as_dt(value: date | datetime | None) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)

    if ref == "case received":
        return _as_dt(account.date_listed) or _as_dt(run_started_at) or _as_dt(
            account.created_at
        )
    if ref == "due date":
        return _as_dt(account.due_date)
    if ref == "promise-to-pay date":
        return _as_dt(account.ptp_due_date)
    if ref == "broken promise-to-pay":
        return _as_dt(account.ptp_due_date)
    if ref == "last customer response":
        return _as_dt(account.last_contact_date)
    if ref == "previous email":
        return _latest_comm_at(db, account.id, "Email") or _as_dt(
            account.last_email_sent_date
        )
    if ref == "previous sms":
        return _latest_comm_at(db, account.id, "SMS") or _as_dt(account.last_sms_sent_date)
    if ref == "payment link sent":
        at = _latest_payment_link_comm_at(db, account.id)
        return at or _latest_comm_at(db, account.id, None, payment_link_only=True)
    if ref == "previous action":
        return (
            _as_dt(account.last_contact_date)
            or _as_dt(account.last_email_sent_date)
            or _as_dt(account.last_sms_sent_date)
            or _as_dt(run_started_at)
        )
    return None


def _latest_comm_at(
    db: Session,
    account_id: int,
    channel: str | None,
    *,
    payment_link_only: bool = False,
) -> datetime | None:
    q = db.query(PayflowCommunicationModel).filter(
        PayflowCommunicationModel.account_id == account_id
    )
    if channel:
        q = q.filter(PayflowCommunicationModel.channel == channel)
    if payment_link_only:
        q = q.filter(PayflowCommunicationModel.payment_link.is_(True))
    row = q.order_by(PayflowCommunicationModel.id.desc()).first()
    if not row:
        return None
    value = row.sent_at or row.delivered_at or row.created_at
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _latest_payment_link_comm_at(db: Session, account_id: int) -> datetime | None:
    return _latest_comm_at(db, account_id, None, payment_link_only=True)
