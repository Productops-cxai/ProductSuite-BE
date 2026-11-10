"""Worker Condition / Wait / Outcome gaps — AI catalog attributes must evaluate at runtime."""

from datetime import datetime, timedelta, timezone

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowStrategyModel,
    PayflowWorkflowRunModel,
    PayflowWorkflowStepRunModel,
)
from app.modules.payflow.services.workflow_condition_runtime import (
    apply_outcome_to_account,
    compare_values,
    payment_status,
    resolve_condition_actual,
    timedelta_for_unit,
)
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.shared.enums import PayflowCollectionStatus, PayflowCommStatus, PayflowWorkflowRunStatus


_SEQ = 0


def _client_account(db, *, outstanding=500.0, original=500.0, recovered=0.0, status="Active"):
    global _SEQ
    _SEQ += 1
    n = _SEQ
    client = PayflowClientModel(code=f"cond-c-{n}", name=f"Cond Client {n}")
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id,
        customer_name="Test User",
        account_reference=f"ACC-{n}",
        case_reference=f"CASE-{n}",
        original_balance=original,
        outstanding_balance=outstanding,
        recovered_balance=recovered,
        collection_status=status,
        email="a@example.com",
        phone_mobile="+15551234567",
    )
    db.add(account)
    db.flush()
    return client, account


def test_compare_not_equals_with_spaces():
    assert compare_values("Unpaid", "Not Equals", "Paid In Full") is True
    assert compare_values("Unpaid", "Not Equals", "Unpaid") is False
    assert compare_values("Delivered", "Equals", "Delivered") is True


def test_payment_status_catalog_values(db_session):
    _, unpaid = _client_account(db_session)
    assert payment_status(db_session, unpaid) == "Unpaid"

    _, partial = _client_account(
        db_session, outstanding=200.0, original=500.0, recovered=300.0
    )
    assert payment_status(db_session, partial) == "Partial Payment"

    _, paid = _client_account(db_session, outstanding=0.0, original=500.0, recovered=500.0)
    assert payment_status(db_session, paid) == "Paid In Full"

    _, plan = _client_account(
        db_session, outstanding=400.0, original=500.0, status="Payment Plan"
    )
    assert payment_status(db_session, plan) == "Payment Plan Active"


def test_eval_condition_uses_attribute_not_field(db_session):
    client, account = _client_account(db_session)
    svc = WorkflowExecutionService(db_session)
    cfg = {
        "attribute": "Payment Status",
        "operator": "Not Equals",
        "value": "Unpaid",
    }
    # Still unpaid → Not Equals Unpaid is False → no branch
    assert svc._eval_condition(account, cfg, {"config": cfg}, {}) is False

    account.outstanding_balance = 0.0
    account.collection_status = PayflowCollectionStatus.RESOLVED.value
    db_session.flush()
    assert svc._eval_condition(account, cfg, {"config": cfg}, {}) is True


def test_email_delivery_bounced_from_failed_comm(db_session):
    client, account = _client_account(db_session)
    db_session.add(
        PayflowCommunicationModel(
            code="EM-1",
            client_id=client.id,
            account_id=account.id,
            channel="Email",
            purpose="Payment Reminder",
            status=PayflowCommStatus.FAILED.value,
            provider_error="bounce: mailbox full",
            balance=500.0,
        )
    )
    db_session.flush()
    actual = resolve_condition_actual(db_session, account, "Email Delivery")
    assert actual == "Bounced"

    svc = WorkflowExecutionService(db_session)
    cfg = {"attribute": "Email Delivery", "operator": "Equals", "value": "Bounced"}
    assert svc._eval_condition(account, cfg, {"config": cfg}, {}) is True


def test_wait_weeks_and_reference_after_previous_email(db_session):
    client, account = _client_account(db_session)
    sent_at = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.add(
        PayflowCommunicationModel(
            code="EM-WAIT",
            client_id=client.id,
            account_id=account.id,
            channel="Email",
            purpose="Payment Reminder",
            status=PayflowCommStatus.DELIVERED.value,
            sent_at=sent_at,
            balance=500.0,
        )
    )
    strategy = PayflowStrategyModel(
        code="wf-wait",
        name="Wait WF",
        client_id=client.id,
        status="Active",
        steps=[],
        entry_node_id="s1",
    )
    db_session.add(strategy)
    db_session.flush()
    run = PayflowWorkflowRunModel(
        account_id=account.id,
        strategy_id=strategy.id,
        client_id=client.id,
        status=PayflowWorkflowRunStatus.ACTIVE.value,
        strategy_version=1,
    )
    db_session.add(run)
    db_session.flush()

    assert timedelta_for_unit(1, "Weeks") == timedelta(days=7)

    svc = WorkflowExecutionService(db_session)
    cfg = {
        "amount": 3,
        "unit": "Days",
        "direction": "After",
        "reference_event": "Previous Email",
    }
    when = svc._wait_until(cfg, {"config": cfg}, strategy, account=account, run=run)
    expected = sent_at + timedelta(days=3)
    assert abs((when - expected).total_seconds()) < 2


def test_outcome_paid_in_full_updates_account(db_session):
    _, account = _client_account(db_session, outstanding=120.0, original=120.0)
    applied = apply_outcome_to_account(account, "Paid In Full")
    assert account.collection_status == PayflowCollectionStatus.RESOLVED.value
    assert float(account.outstanding_balance) == 0.0
    assert applied["outcome"] == "Paid In Full"


def test_terminal_human_review_completes_run(db_session):
    client, account = _client_account(db_session)
    strategy = PayflowStrategyModel(
        code="wf-hr",
        name="HR WF",
        client_id=client.id,
        status="Active",
        steps=[
            {
                "id": "s10",
                "kind": "Human Review",
                "title": "Supervisor review",
                "config": {"note": "decide"},
                "next": None,
            }
        ],
        entry_node_id="s10",
    )
    db_session.add(strategy)
    db_session.flush()
    run = PayflowWorkflowRunModel(
        account_id=account.id,
        strategy_id=strategy.id,
        client_id=client.id,
        status=PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value,
        current_step_id="s10",
        strategy_version=1,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        PayflowWorkflowStepRunModel(
            run_id=run.id,
            step_id="s10",
            kind="Human Review",
            attempt=1,
            status="paused_human_review",
        )
    )
    db_session.commit()

    WorkflowExecutionService(db_session).resume_run(
        run.id, from_step_id="s10", after_human_review=True
    )
    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.COMPLETED.value
    assert run.completed_at is not None
