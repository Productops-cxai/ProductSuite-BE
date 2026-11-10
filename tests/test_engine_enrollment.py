"""Enrollment: one workflow per account, no restart on every import."""

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowStrategyModel,
    PayflowWorkflowRunModel,
)
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.shared.enums import PayflowWorkflowRunStatus

_SEQ = 0

_STEPS = [
    {"id": "s1", "kind": "Trigger", "title": "Start", "next": "s2"},
    {"id": "s2", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
]


def _client(db):
    global _SEQ
    _SEQ += 1
    client = PayflowClientModel(code=f"enr-c-{_SEQ}", name=f"Enroll Client {_SEQ}")
    db.add(client)
    db.flush()
    return client


def _account(db, client, *, outstanding=500.0):
    global _SEQ
    _SEQ += 1
    account = PayflowAccountModel(
        client_id=client.id,
        customer_name="Test User",
        account_reference=f"ENR-{_SEQ}",
        case_reference=f"CASE-ENR-{_SEQ}",
        original_balance=outstanding,
        outstanding_balance=outstanding,
        collection_status="Active",
        email="a@example.com",
    )
    db.add(account)
    db.flush()
    return account


def _strategy(db, client, code, *, segment=None, status="Active"):
    strategy = PayflowStrategyModel(
        code=code,
        name=code,
        client_id=client.id,
        status=status,
        segment=segment,
        steps=_STEPS,
        entry_node_id="s1",
    )
    db.add(strategy)
    db.flush()
    return strategy


def _open_runs(db, account):
    return (
        db.query(PayflowWorkflowRunModel)
        .filter(
            PayflowWorkflowRunModel.account_id == account.id,
            PayflowWorkflowRunModel.status.in_(
                [
                    PayflowWorkflowRunStatus.ACTIVE.value,
                    PayflowWorkflowRunStatus.WAITING.value,
                    PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value,
                ]
            ),
        )
        .all()
    )


def test_account_enrolls_only_in_most_specific_active_workflow(db_session):
    client = _client(db_session)
    account = _account(db_session, client, outstanding=8000.0)
    general = _strategy(db_session, client, "enr-general")
    specific = _strategy(
        db_session, client, "enr-specific", segment={"balance_band": "$5,000 and over"}
    )
    db_session.commit()

    svc = WorkflowExecutionService(db_session)
    svc.enroll_strategy(general.id)
    svc.enroll_strategy(specific.id)

    runs = _open_runs(db_session, account)
    assert [r.strategy_id for r in runs] == [specific.id]


def test_repeated_enroll_does_not_restart_run_on_other_active_workflow(db_session):
    client = _client(db_session)
    account = _account(db_session, client, outstanding=8000.0)
    general = _strategy(db_session, client, "enr-general-2")
    specific = _strategy(
        db_session, client, "enr-specific-2", segment={"balance_band": "$5,000 and over"}
    )
    db_session.commit()

    svc = WorkflowExecutionService(db_session)
    svc.enroll_strategy(specific.id)
    first = _open_runs(db_session, account)[0].id

    # Every import queues enroll for all Active workflows in scope.
    svc.enroll_strategy(general.id)
    svc.enroll_strategy(specific.id)
    svc.enroll_strategy(general.id)

    runs = _open_runs(db_session, account)
    assert [r.id for r in runs] == [first]


def test_open_run_is_kept_when_account_drifts_into_another_segment(db_session):
    """A run in flight is not restarted because the account now fits a different workflow."""
    client = _client(db_session)
    account = _account(db_session, client, outstanding=400.0)
    small = _strategy(db_session, client, "enr-small", segment={"balance_band": "Under $500"})
    large = _strategy(
        db_session, client, "enr-large", segment={"balance_band": "$5,000 and over"}
    )
    db_session.commit()

    svc = WorkflowExecutionService(db_session)
    svc.enroll_strategy(small.id)
    first = _open_runs(db_session, account)[0].id

    account.outstanding_balance = 9000.0
    db_session.commit()
    svc.enroll_strategy(large.id)

    runs = _open_runs(db_session, account)
    assert [r.id for r in runs] == [first]


def test_completed_run_is_not_re_enrolled_on_same_workflow_version(db_session):
    client = _client(db_session)
    account = _account(db_session, client)
    strategy = _strategy(db_session, client, "enr-once")
    db_session.commit()

    svc = WorkflowExecutionService(db_session)
    svc.enroll_strategy(strategy.id)
    run = _open_runs(db_session, account)[0]
    run.status = PayflowWorkflowRunStatus.COMPLETED.value
    db_session.commit()

    result = svc.enroll_strategy(strategy.id)

    assert result["enrolled"] == 0
    assert _open_runs(db_session, account) == []


def test_run_on_inactive_workflow_is_replaced_by_active_one(db_session):
    client = _client(db_session)
    account = _account(db_session, client)
    old = _strategy(db_session, client, "enr-old")
    db_session.commit()
    svc = WorkflowExecutionService(db_session)
    svc.enroll_strategy(old.id)

    old.status = "Inactive"
    new = _strategy(db_session, client, "enr-new")
    db_session.commit()
    svc.enroll_strategy(new.id)

    runs = _open_runs(db_session, account)
    assert [r.strategy_id for r in runs] == [new.id]
