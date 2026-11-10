"""A step blocked by a hold runs again once the hold is released."""

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowRuleModel,
    PayflowStrategyModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
    PayflowWorkflowStepRunModel,
)
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.shared.enums import PayflowWorkflowJobType, PayflowWorkflowRunStatus

STEPS = [
    {"id": "s1", "kind": "Trigger", "title": "Start", "next": "s2"},
    {
        "id": "s2",
        "kind": "Case Action",
        "title": "Flag account",
        "config": {"action": "Flag"},
        "next": "s3",
    },
    {"id": "s3", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
]


def _setup(db, *, steps=STEPS):
    client = PayflowClientModel(code="hold-c", name="Hold Client", governance_rules=[])
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id,
        customer_name="Test User",
        account_reference="HOLD-1",
        case_reference="CASE-HOLD-1",
        original_balance=900.0,
        outstanding_balance=900.0,
        collection_status="Active",
        email="a@example.com",
    )
    strategy = PayflowStrategyModel(
        code="hold-wf", name="Hold WF", client_id=client.id, status="Active",
        steps=steps, entry_node_id="s1",
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
    return client, account, run


def _hold_rule(db, client, *, action="Hold Action"):
    rule = PayflowRuleModel(
        code=f"hold-rule-{action}",
        name="Hold big balances",
        rule_type="Client Rule",
        client_id=client.id,
        status="Active",
        category="Amount",
        action=action,
        conditions=[{"field": "Outstanding Balance", "operator": ">", "value": "100"}],
    )
    db.add(rule)
    db.commit()
    return rule


def _pending(db, run):
    return (
        db.query(PayflowWorkflowJobModel)
        .filter(PayflowWorkflowJobModel.run_id == run.id, PayflowWorkflowJobModel.status == "pending")
        .order_by(PayflowWorkflowJobModel.id.asc())
        .all()
    )


def _finish_jobs(db, run):
    for job in _pending(db, run):
        job.status = "done"
    db.commit()


def _step_statuses(db, run, step_id):
    rows = (
        db.query(PayflowWorkflowStepRunModel)
        .filter_by(run_id=run.id, step_id=step_id)
        .order_by(PayflowWorkflowStepRunModel.id.asc())
        .all()
    )
    return [r.status for r in rows]


def test_step_blocked_by_account_hold_runs_after_hold_is_released(db_session):
    _client, account, run = _setup(db_session)
    svc = WorkflowExecutionService(db_session)
    svc.execute_step(run.id, "s1")
    _finish_jobs(db_session, run)

    account.provincial_hold = True
    db_session.commit()
    svc.execute_step(run.id, "s2")
    assert _step_statuses(db_session, run, "s2") == ["blocked"]

    account.provincial_hold = False
    db_session.commit()
    svc.resume_run(run.id, from_step_id=run.current_step_id)
    assert [j.step_id for j in _pending(db_session, run)] == ["s2"]
    _finish_jobs(db_session, run)
    svc.execute_step(run.id, "s2")

    assert _step_statuses(db_session, run, "s2") == ["blocked", "done"]
    assert [j.step_id for j in _pending(db_session, run)] == ["s3"]


def test_step_held_by_rule_is_re_run_not_skipped_on_resume(db_session):
    client, _account, run = _setup(db_session)
    rule = _hold_rule(db_session, client)
    svc = WorkflowExecutionService(db_session)
    svc.execute_step(run.id, "s2")
    assert _step_statuses(db_session, run, "s2") == ["blocked"]

    rule.status = "Inactive"
    db_session.commit()
    svc.resume_run(run.id, from_step_id=run.current_step_id)

    assert [j.step_id for j in _pending(db_session, run)] == ["s2"]


def test_sweep_resumes_run_when_hold_rule_no_longer_applies(db_session):
    client, _account, run = _setup(db_session)
    rule = _hold_rule(db_session, client)
    svc = WorkflowExecutionService(db_session)
    svc.execute_step(run.id, "s2")

    rule.status = "Inactive"
    db_session.commit()
    result = svc.resume_released_holds()

    assert result["resumed"] == 1
    jobs = _pending(db_session, run)
    assert [j.job_type for j in jobs] == [PayflowWorkflowJobType.RESUME_RUN.value]


def test_sweep_leaves_run_alone_while_hold_rule_still_applies(db_session):
    client, _account, run = _setup(db_session)
    _hold_rule(db_session, client)
    svc = WorkflowExecutionService(db_session)
    svc.execute_step(run.id, "s2")

    result = svc.resume_released_holds()

    assert result["resumed"] == 0
    assert _pending(db_session, run) == []


def test_prevented_message_on_last_step_completes_the_run(db_session):
    steps = [
        {
            "id": "s1",
            "kind": "Communication",
            "title": "Email",
            "config": {"channel": "Email", "purpose": "Payment Reminder", "body": "Hi"},
        }
    ]
    client, _account, run = _setup(db_session, steps=steps)
    _hold_rule(db_session, client, action="Prevent Communication")

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")

    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.COMPLETED.value
