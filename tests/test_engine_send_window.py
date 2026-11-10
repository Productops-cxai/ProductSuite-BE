"""A message due outside the client's send window is sent when the window opens."""

from datetime import datetime, timezone

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowStrategyModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
    PayflowWorkflowStepRunModel,
)
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.shared.enums import PayflowWorkflowRunStatus

STEPS = [
    {
        "id": "s1",
        "kind": "Communication",
        "title": "Email reminder",
        "config": {"channel": "Email", "purpose": "Payment Reminder", "body": "Hi {{first_name}}"},
        "next": "s2",
    },
    {"id": "s2", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
]


def _closed_window():
    """A one-hour window that starts two hours from now (UTC)."""
    hour = datetime.now(timezone.utc).hour
    return {
        "timezone": "UTC",
        "send_window_start_hour": (hour + 2) % 24,
        "send_window_end_hour": (hour + 3) % 24,
    }


def _run(db, *, communication_settings=None):
    client = PayflowClientModel(
        code="win-c", name="Window Client", communication_settings=communication_settings
    )
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id,
        customer_name="Test User",
        account_reference="WIN-1",
        case_reference="CASE-WIN-1",
        original_balance=500.0,
        outstanding_balance=500.0,
        collection_status="Active",
        email="a@example.com",
    )
    strategy = PayflowStrategyModel(
        code="win-wf", name="Window WF", client_id=client.id, status="Active",
        steps=STEPS, entry_node_id="s1",
    )
    db.add_all([account, strategy])
    db.flush()
    run = PayflowWorkflowRunModel(
        account_id=account.id,
        strategy_id=strategy.id,
        client_id=client.id,
        status=PayflowWorkflowRunStatus.ACTIVE.value,
        current_step_id="s1",
        strategy_version=1,
    )
    db.add(run)
    db.commit()
    return run


def _pending_jobs(db, run):
    return (
        db.query(PayflowWorkflowJobModel)
        .filter(PayflowWorkflowJobModel.run_id == run.id, PayflowWorkflowJobModel.status == "pending")
        .all()
    )


def test_message_outside_send_window_is_rescheduled_not_dropped(db_session):
    run = _run(db_session, communication_settings=_closed_window())

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")

    jobs = _pending_jobs(db_session, run)
    assert [j.step_id for j in jobs] == ["s1"]
    scheduled = jobs[0].scheduled_at
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    assert scheduled > datetime.now(timezone.utc)
    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.WAITING.value
    step_run = db_session.query(PayflowWorkflowStepRunModel).filter_by(run_id=run.id).one()
    assert step_run.status == "scheduled"


def test_message_inside_send_window_moves_to_next_step(db_session):
    run = _run(db_session)

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")

    assert [j.step_id for j in _pending_jobs(db_session, run)] == ["s2"]


def test_rescheduled_message_is_sent_once_the_window_opens(db_session):
    from unittest.mock import patch

    run = _run(db_session, communication_settings=_closed_window())
    svc = WorkflowExecutionService(db_session)
    svc.execute_step(run.id, "s1")
    for job in _pending_jobs(db_session, run):
        job.status = "done"
    db_session.commit()

    with patch.object(WorkflowExecutionService, "_send_window_opens_at", return_value=None), \
            patch(
                "app.modules.payflow.services.communication_service._evaluate_send_window",
                return_value={"ok": True},
            ):
        svc.execute_step(run.id, "s1", after_comm_delay=True)

    assert [j.step_id for j in _pending_jobs(db_session, run)] == ["s2"]
