"""Worker: no double runs, no silent dead jobs, no starving the pool."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import sessionmaker

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowStrategyModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
    PayflowWorkflowWorkerLogModel,
)
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.shared.enums import (
    PayflowWorkflowJobStatus,
    PayflowWorkflowJobType,
    PayflowWorkflowRunStatus,
)
from app.worker import handlers
from app.worker.loop import claim_budget

STEPS = [
    {"id": "s1", "kind": "Trigger", "title": "Start", "next": "s2"},
    {"id": "s2", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
]


@pytest.fixture()
def worker_db(db_session):
    """process_job opens its own session: point it at the test database."""
    factory = sessionmaker(autocommit=False, autoflush=False, bind=db_session.get_bind())
    with patch.object(handlers, "SessionLocal", factory):
        yield db_session


def _run(db, *, accounts=1):
    client = PayflowClientModel(code="wrk-c", name="Worker Client")
    db.add(client)
    db.flush()
    strategy = PayflowStrategyModel(
        code="wrk-wf", name="Worker WF", client_id=client.id, status="Active",
        steps=STEPS, entry_node_id="s1",
    )
    db.add(strategy)
    rows = []
    for index in range(accounts):
        account = PayflowAccountModel(
            client_id=client.id,
            customer_name="Test User",
            account_reference=f"WRK-{index}",
            case_reference=f"CASE-WRK-{index}",
            original_balance=500.0,
            outstanding_balance=500.0,
            collection_status="Active",
        )
        db.add(account)
        rows.append(account)
    db.flush()
    run = PayflowWorkflowRunModel(
        account_id=rows[0].id,
        strategy_id=strategy.id,
        client_id=client.id,
        status=PayflowWorkflowRunStatus.ACTIVE.value,
        current_step_id="s1",
        strategy_version=1,
    )
    db.add(run)
    db.commit()
    return strategy, run


def _job(db, *, run=None, strategy=None, step_id=None, job_type=None, status="running",
         locked_by="worker-a", attempts=1, max_attempts=5):
    job = PayflowWorkflowJobModel(
        job_type=job_type or PayflowWorkflowJobType.EXECUTE_STEP.value,
        run_id=run.id if run else None,
        strategy_id=strategy.id if strategy else None,
        step_id=step_id,
        status=status,
        locked_by=locked_by,
        locked_at=datetime.now(timezone.utc),
        attempts=attempts,
        max_attempts=max_attempts,
    )
    db.add(job)
    db.commit()
    return job


def test_job_claimed_by_another_worker_is_not_processed(worker_db):
    _strategy, run = _run(worker_db)
    job = _job(worker_db, run=run, step_id="s1", locked_by="worker-b")

    handlers.process_job(job.id, worker_id="worker-a")

    worker_db.expire_all()
    assert worker_db.get(PayflowWorkflowJobModel, job.id).status == "running"
    assert worker_db.query(PayflowWorkflowJobModel).count() == 1  # no next step queued


def test_job_reclaimed_back_to_pending_is_not_processed(worker_db):
    _strategy, run = _run(worker_db)
    job = _job(worker_db, run=run, step_id="s1", status="pending", locked_by=None)

    handlers.process_job(job.id, worker_id="worker-a")

    worker_db.expire_all()
    assert worker_db.get(PayflowWorkflowJobModel, job.id).status == "pending"


def test_owned_job_is_processed_and_marked_done(worker_db):
    _strategy, run = _run(worker_db)
    job = _job(worker_db, run=run, step_id="s1")

    handlers.process_job(job.id, worker_id="worker-a")

    worker_db.expire_all()
    assert worker_db.get(PayflowWorkflowJobModel, job.id).status == "done"


def test_job_that_fails_for_good_fails_the_run_and_is_logged(worker_db):
    _strategy, run = _run(worker_db)
    job = _job(worker_db, run=run, step_id="s1", attempts=5, max_attempts=5)

    with patch.object(
        WorkflowExecutionService, "execute_step", side_effect=RuntimeError("provider exploded")
    ):
        handlers.process_job(job.id, worker_id="worker-a")

    worker_db.expire_all()
    assert worker_db.get(PayflowWorkflowJobModel, job.id).status == PayflowWorkflowJobStatus.FAILED.value
    stored = worker_db.get(PayflowWorkflowRunModel, run.id)
    assert stored.status == PayflowWorkflowRunStatus.FAILED.value
    assert "provider exploded" in (stored.last_error or "")
    events = [
        row.event_type
        for row in worker_db.query(PayflowWorkflowWorkerLogModel).filter_by(level="error").all()
    ]
    assert "job_failed" in events


def test_job_with_retries_left_keeps_the_run_open(worker_db):
    _strategy, run = _run(worker_db)
    job = _job(worker_db, run=run, step_id="s1", attempts=1, max_attempts=5)

    with patch.object(
        WorkflowExecutionService, "execute_step", side_effect=RuntimeError("temporary")
    ):
        handlers.process_job(job.id, worker_id="worker-a")

    worker_db.expire_all()
    assert worker_db.get(PayflowWorkflowJobModel, job.id).status == "pending"
    assert worker_db.get(PayflowWorkflowRunModel, run.id).status == PayflowWorkflowRunStatus.ACTIVE.value


def test_long_enroll_keeps_its_job_lock_fresh(db_session):
    strategy, _run_row = _run(db_session, accounts=3)
    job = _job(db_session, strategy=strategy, job_type=PayflowWorkflowJobType.ENROLL_STRATEGY.value)
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    job.locked_at = stale
    db_session.commit()

    WorkflowExecutionService(db_session).enroll_strategy(strategy.id, job_id=job.id)

    db_session.refresh(job)
    locked_at = job.locked_at
    if locked_at.tzinfo is None:
        locked_at = locked_at.replace(tzinfo=timezone.utc)
    assert locked_at > stale + timedelta(minutes=5)


def test_claim_budget_leaves_room_for_jobs_already_queued():
    assert claim_budget(batch_size=50, concurrency=8, in_flight=0) == 16
    assert claim_budget(batch_size=50, concurrency=8, in_flight=10) == 6
    assert claim_budget(batch_size=50, concurrency=8, in_flight=16) == 0
    assert claim_budget(batch_size=4, concurrency=8, in_flight=0) == 4
