"""Import / AI-suggest jobs cut off by a restart must not stay 'running' forever."""

from datetime import datetime, timedelta, timezone

from app.infrastructure.database.models import (
    PayflowAiSuggestJobModel,
    PayflowClientModel,
    PayflowImportJobModel,
    PayflowImportRunModel,
)
from app.modules.payflow.services.ai_suggest_job_service import AiSuggestJobService
from app.modules.payflow.services.background_job_recovery import recover_interrupted_jobs
from app.modules.payflow.services.import_progress import report_import_progress

NOW = datetime.now(timezone.utc)
LONG_AGO = NOW - timedelta(hours=3)


def _import_job(db, *, status="running", heartbeat_at=None, created_at=None, with_run=False):
    run = None
    if with_run:
        run = PayflowImportRunModel(
            kind="accounts", file_name="daily.xlsx", uploaded_by_name="Tester", status="Processing"
        )
        db.add(run)
        db.flush()
    job = PayflowImportJobModel(
        job_type="account_upload",
        status=status,
        file_name="daily.xlsx",
        import_run_id=run.id if run else None,
        heartbeat_at=heartbeat_at,
        created_at=created_at or NOW,
    )
    db.add(job)
    db.commit()
    return job, run


def _ai_job(db, *, status, started_at=None, created_at=None):
    client = PayflowClientModel(code=f"rec-c-{status}", name="Recovery Client", status="active")
    db.add(client)
    db.flush()
    job = PayflowAiSuggestJobModel(
        client_id=client.id,
        portfolio_id=None,
        trigger="import",
        status=status,
        started_at=started_at,
        created_at=created_at or NOW,
    )
    db.add(job)
    db.commit()
    return client, job


def test_import_job_with_no_recent_progress_is_failed_with_its_run(db_session):
    job, run = _import_job(db_session, heartbeat_at=LONG_AGO, created_at=LONG_AGO, with_run=True)

    result = recover_interrupted_jobs(db_session)

    db_session.refresh(job)
    db_session.refresh(run)
    assert result["import_jobs_failed"] == 1
    assert job.status == "failed"
    assert "interrupted" in job.error_message.lower()
    assert job.completed_at is not None
    assert run.status == "Failed"


def test_import_job_still_reporting_progress_is_left_alone(db_session):
    job, _run = _import_job(db_session, heartbeat_at=NOW, created_at=LONG_AGO)

    result = recover_interrupted_jobs(db_session)

    db_session.refresh(job)
    assert result["import_jobs_failed"] == 0
    assert job.status == "running"


def test_queued_import_job_that_never_started_is_failed(db_session):
    job, _run = _import_job(db_session, status="queued", created_at=LONG_AGO)

    recover_interrupted_jobs(db_session)

    db_session.refresh(job)
    assert job.status == "failed"


def test_progress_report_refreshes_the_import_heartbeat(db_session):
    from unittest.mock import patch

    from sqlalchemy.orm import sessionmaker

    from app.modules.payflow.services import import_progress

    job, _run = _import_job(db_session, heartbeat_at=LONG_AGO, created_at=LONG_AGO)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=db_session.get_bind())
    with patch.object(import_progress, "SessionLocal", factory):
        report_import_progress(job_id=job.id, processed=5, total=10)

    db_session.expire_all()
    heartbeat = db_session.get(PayflowImportJobModel, job.id).heartbeat_at
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    assert heartbeat > LONG_AGO + timedelta(hours=2)


def test_ai_job_stuck_running_is_failed_so_the_scope_can_be_queued_again(db_session):
    client, job = _ai_job(db_session, status="running", started_at=LONG_AGO, created_at=LONG_AGO)

    result = recover_interrupted_jobs(db_session)

    db_session.refresh(job)
    assert result["ai_jobs_failed"] == 1
    assert job.status == "failed"
    assert "interrupted" in job.error_message.lower()
    # Before the fix this returned None forever: the dead job blocked its scope.
    again = AiSuggestJobService(db_session).enqueue(client.id, None, trigger="import")
    assert again is not None


def test_ai_job_pending_since_before_a_restart_is_handed_back_to_run(db_session):
    _client, job = _ai_job(db_session, status="pending", created_at=LONG_AGO)

    result = recover_interrupted_jobs(db_session)

    db_session.refresh(job)
    assert result["ai_jobs_to_run"] == [job.id]
    assert job.status == "pending"


def test_recent_ai_jobs_are_left_alone(db_session):
    _client, job = _ai_job(db_session, status="running", started_at=NOW)

    result = recover_interrupted_jobs(db_session)

    db_session.refresh(job)
    assert result["ai_jobs_failed"] == 0
    assert result["ai_jobs_to_run"] == []
    assert job.status == "running"


def test_import_job_that_crashes_marks_its_run_failed_too(db_session):
    from unittest.mock import patch

    from sqlalchemy.orm import sessionmaker

    from app.modules.payflow.services import import_job_service

    job, run = _import_job(db_session, status="queued", with_run=True)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=db_session.get_bind())
    with patch.object(import_job_service, "SessionLocal", factory), patch.object(
        import_job_service, "_resolve_stored_bytes", side_effect=RuntimeError("disk gone")
    ):
        import_job_service.run_import_job(job.id)

    db_session.expire_all()
    assert db_session.get(PayflowImportJobModel, job.id).status == "failed"
    assert db_session.get(PayflowImportRunModel, run.id).status == "Failed"
