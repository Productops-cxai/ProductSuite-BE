"""Every field the rule builder offers must be enforced at run time."""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowPaymentAttemptModel,
    PayflowPaymentLinkModel,
    PayflowPaymentPlanInstallmentModel,
    PayflowPaymentPlanModel,
    PayflowRuleModel,
)
from app.modules.payflow.services.rule_runtime_service import FIELD_GETTERS, RuleRuntimeService
from app.modules.payflow.services.rule_service import RULE_FIELDS

_SEQ = 0


def _account(db):
    global _SEQ
    _SEQ += 1
    client = PayflowClientModel(code=f"rf-c-{_SEQ}", name=f"Rule Field Client {_SEQ}")
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id,
        customer_name="Test User",
        account_reference=f"RF-{_SEQ}",
        case_reference=f"CASE-RF-{_SEQ}",
        original_balance=600.0,
        outstanding_balance=600.0,
        collection_status="Active",
    )
    db.add(account)
    db.flush()
    return client, account


def _rule(db, client, field, operator, value):
    rule = PayflowRuleModel(
        code=f"rf-{field}-{client.id}",
        name=f"Rule on {field}",
        rule_type="Client Rule",
        client_id=client.id,
        status="Active",
        category="Test",
        action="Hold Action",
        conditions=[{"field": field, "operator": operator, "value": value}],
    )
    db.add(rule)
    db.commit()
    return rule


def _comm(db, account, status, *, days_ago=0):
    global _SEQ
    _SEQ += 1
    db.add(
        PayflowCommunicationModel(
            code=f"COM-RF-{_SEQ}",
            client_id=account.client_id,
            account_id=account.id,
            channel="Email",
            purpose="Payment Reminder",
            status=status,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
    )
    db.flush()


def _plan(db, account, *, status="Active", installments=("Pending", "Pending")):
    global _SEQ
    _SEQ += 1
    plan = PayflowPaymentPlanModel(
        public_id=f"plan-rf-{_SEQ}",
        client_id=account.client_id,
        account_id=account.id,
        status=status,
        installments_total=len(installments),
        installment_amount=100.0,
        total_payable=100.0 * len(installments),
        start_date=date.today(),
    )
    db.add(plan)
    db.flush()
    for index, inst_status in enumerate(installments, start=1):
        db.add(
            PayflowPaymentPlanInstallmentModel(
                plan_id=plan.id,
                sequence=index,
                amount=100.0,
                due_date=date.today(),
                status=inst_status,
            )
        )
    db.flush()
    return plan


def _matches(db, account, rule):
    return RuleRuntimeService(db).matches_rule(rule, account)


def test_every_offered_rule_field_has_a_runtime_getter():
    offered = {field["label"] for field in RULE_FIELDS}
    assert offered - set(FIELD_GETTERS) == set()


def test_unsuccessful_attempts_counts_failed_messages(db_session):
    client, account = _account(db_session)
    rule = _rule(db_session, client, "Unsuccessful Attempts", ">=", "2")
    _comm(db_session, account, "Failed")
    _comm(db_session, account, "Delivered")
    assert _matches(db_session, account, rule) is False

    _comm(db_session, account, "Failed")
    assert _matches(db_session, account, rule) is True


def test_failed_payment_attempts_counts_failed_attempts(db_session):
    client, account = _account(db_session)
    rule = _rule(db_session, client, "Failed Payment Attempts", ">=", "1")
    assert _matches(db_session, account, rule) is False

    link = PayflowPaymentLinkModel(
        client_id=account.client_id,
        account_id=account.id,
        token="rf-token",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db_session.add(link)
    db_session.flush()
    db_session.add(
        PayflowPaymentAttemptModel(
            public_id="att-rf-1",
            payment_link_id=link.id,
            client_id=account.client_id,
            account_id=account.id,
            option_type="pay_in_full",
            amount=600.0,
            status="Failed",
        )
    )
    db_session.flush()
    assert _matches(db_session, account, rule) is True


@pytest.mark.parametrize(
    ("plan_status", "installments", "expected"),
    [
        (None, (), "None"),
        ("Active", ("Paid", "Pending"), "On Track"),
        ("Active", ("Failed", "Pending"), "At Risk"),
        ("Overdue", ("Overdue", "Pending"), "At Risk"),
        ("Defaulted", ("Overdue", "Overdue"), "Defaulted"),
        ("Completed", ("Paid", "Paid"), "None"),
    ],
)
def test_payment_plan_status_values(db_session, plan_status, installments, expected):
    client, account = _account(db_session)
    rule = _rule(db_session, client, "Payment Plan Status", "equals", expected)
    if plan_status:
        _plan(db_session, account, status=plan_status, installments=installments)

    assert _matches(db_session, account, rule) is True


def test_missed_installments_counts_overdue_and_failed(db_session):
    client, account = _account(db_session)
    rule = _rule(db_session, client, "Missed Installments", ">=", "2")
    _plan(db_session, account, installments=("Paid", "Overdue", "Pending"))
    assert _matches(db_session, account, rule) is False

    _plan(db_session, account, installments=("Failed", "Overdue", "Pending"))
    assert _matches(db_session, account, rule) is True


def test_messages_sent_7_days_counts_only_messages_that_went_out(db_session):
    client, account = _account(db_session)
    rule = _rule(db_session, client, "Messages Sent (7 days)", ">=", "2")
    _comm(db_session, account, "Sent")
    _comm(db_session, account, "Suppressed")  # blocked by governance: never left
    _comm(db_session, account, "Delivered", days_ago=9)  # outside the window
    assert _matches(db_session, account, rule) is False

    _comm(db_session, account, "Delivered")
    assert _matches(db_session, account, rule) is True
