"""Which communication a completed payment is credited to.

Rule: the last payment-link click (email link click or portal visit, any channel) on the account
within PAYMENT_ATTRIBUTION_WINDOW_DAYS before the payment. Auto-charged plan instalments are
credited to the message the plan itself was credited to. No qualifying click → not attributed.

The credit is one column on the transaction, so a payment can never count for two subjects.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import (
    PayflowCommunicationEventModel,
    PayflowCommunicationModel,
    PayflowPaymentAttemptModel,
    PayflowPaymentPlanModel,
    PayflowPaymentTransactionModel,
)

logger = logging.getLogger("app.payments")

METHOD_LAST_CLICK = "last_click"
METHOD_PLAN = "plan"
METHOD_NONE = "none"

# Events that are a payment-link click: SendGrid's click on the email's link, and the customer
# opening the payment portal (see apply_provider_event / record_portal_visit).
CLICK_EVENT_TYPES = ("email_link_clicked", "click")


def attribution_window() -> timedelta:
    return timedelta(days=max(1, int(settings.PAYMENT_ATTRIBUTION_WINDOW_DAYS or 7)))


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def last_click_communication_id(db: Session, account_id: int, paid_at: datetime) -> int | None:
    """The account's payment-link message with the latest click in the window before `paid_at`."""
    row = (
        db.query(PayflowCommunicationEventModel.communication_id)
        .join(
            PayflowCommunicationModel,
            PayflowCommunicationModel.id == PayflowCommunicationEventModel.communication_id,
        )
        .filter(
            PayflowCommunicationModel.account_id == account_id,
            PayflowCommunicationModel.payment_link.is_(True),
            PayflowCommunicationEventModel.event_type.in_(CLICK_EVENT_TYPES),
            PayflowCommunicationEventModel.occurred_at >= paid_at - attribution_window(),
            PayflowCommunicationEventModel.occurred_at <= paid_at,
        )
        .order_by(
            PayflowCommunicationEventModel.occurred_at.desc(),
            PayflowCommunicationEventModel.id.desc(),
        )
        .first()
    )
    return row[0] if row else None


def _plan_communication_id(db: Session, attempt: PayflowPaymentAttemptModel) -> int | None:
    """The message the attempt's payment plan was credited to (its first credited payment)."""
    if not attempt.plan_id:
        return None
    plan = (
        db.query(PayflowPaymentPlanModel)
        .filter(
            PayflowPaymentPlanModel.account_id == attempt.account_id,
            PayflowPaymentPlanModel.public_id == attempt.plan_id,
        )
        .first()
    )
    if plan is None:
        return None
    row = (
        db.query(PayflowPaymentTransactionModel.attributed_communication_id)
        .filter(
            PayflowPaymentTransactionModel.payment_plan_id == plan.id,
            PayflowPaymentTransactionModel.attributed_communication_id.isnot(None),
        )
        .order_by(PayflowPaymentTransactionModel.id.asc())
        .first()
    )
    return row[0] if row else None


def attribute_payment(
    db: Session, txn: PayflowPaymentTransactionModel, attempt: PayflowPaymentAttemptModel
) -> None:
    """Set the transaction's credit once (a transaction already decided is left as it is)."""
    if txn.attribution_method:
        return
    paid_at = _aware(txn.completed_at) or datetime.now(timezone.utc)
    auto_charge = bool((attempt.provider_payload or {}).get("auto_charge"))

    communication_id: int | None = None
    method = METHOD_NONE
    if not auto_charge:
        # The message whose link started this checkout is the last click by definition, when its
        # click is still inside the window; else the latest click on any of the account's messages.
        source = db.get(PayflowCommunicationModel, attempt.communication_id) if attempt.communication_id else None
        clicked_at = _aware(source.last_clicked_at) if source is not None else None
        if source is not None and clicked_at and paid_at - attribution_window() <= clicked_at <= paid_at:
            communication_id = source.id
        else:
            communication_id = last_click_communication_id(db, attempt.account_id, paid_at)
        if communication_id:
            method = METHOD_LAST_CLICK
    if not communication_id:
        communication_id = _plan_communication_id(db, attempt)
        if communication_id:
            method = METHOD_PLAN

    txn.attributed_communication_id = communication_id
    txn.attribution_method = method
    txn.attributed_at = datetime.now(timezone.utc)
