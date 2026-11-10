"""In-flight runs keep executing the approved version, never unapproved edits."""

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowStrategyModel,
    PayflowStrategyVersionModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
)
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.shared.enums import PayflowWorkflowRunStatus

APPROVED_STEPS = [
    {"id": "s1", "kind": "Trigger", "title": "Start", "next": "s2"},
    {"id": "s2", "kind": "Case Action", "title": "Approved action", "config": {"action": "Flag"}},
]
EDITED_STEPS = [
    {"id": "s1", "kind": "Trigger", "title": "Start", "next": "s9"},
    {"id": "s9", "kind": "Case Action", "title": "Unapproved action", "config": {"action": "Flag"}},
]


def _edited_active_strategy(db):
    """An Active v1 workflow that a reviewer edited: now Under Review v2, v1 archived."""
    client = PayflowClientModel(code="pin-c", name="Pin Client")
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id,
        customer_name="Test User",
        account_reference="PIN-1",
        case_reference="CASE-PIN-1",
        original_balance=500.0,
        outstanding_balance=500.0,
        collection_status="Active",
    )
    strategy = PayflowStrategyModel(
        code="pin-wf",
        name="Pin WF",
        client_id=client.id,
        status="Under Review",
        version=2,
        steps=EDITED_STEPS,
        entry_node_id="s1",
    )
    db.add_all([account, strategy])
    db.flush()
    db.add(
        PayflowStrategyVersionModel(
            strategy_id=strategy.id,
            version=1,
            event_date="2026-11-01",
            note="Archived approved/active configuration before edit",
            status="Active",
            steps=APPROVED_STEPS,
        )
    )
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
    return strategy, run


def _pending_step_ids(db, run):
    rows = (
        db.query(PayflowWorkflowJobModel)
        .filter(PayflowWorkflowJobModel.run_id == run.id, PayflowWorkflowJobModel.status == "pending")
        .all()
    )
    return [r.step_id for r in rows]


def test_run_follows_approved_version_after_workflow_is_edited(db_session):
    _strategy, run = _edited_active_strategy(db_session)

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")

    assert _pending_step_ids(db_session, run) == ["s2"]


def test_step_removed_by_an_edit_still_runs_for_in_flight_run(db_session):
    _strategy, run = _edited_active_strategy(db_session)

    WorkflowExecutionService(db_session).execute_step(run.id, "s2")

    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.COMPLETED.value


def test_run_on_current_version_uses_live_steps(db_session):
    strategy, run = _edited_active_strategy(db_session)
    strategy.status = "Active"
    run.strategy_version = 2
    db_session.commit()

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")

    assert _pending_step_ids(db_session, run) == ["s9"]
