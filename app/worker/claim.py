"""Claim due workflow jobs with SELECT … FOR UPDATE SKIP LOCKED."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.infrastructure.database.models import PayflowWorkflowJobModel
from app.shared.enums import PayflowWorkflowJobStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def claim_due_jobs(
    db: Session,
    *,
    worker_id: str,
    batch_size: int = 50,
) -> list[PayflowWorkflowJobModel]:
    """
    Atomically claim up to batch_size pending jobs that are due.
    Uses SKIP LOCKED so multiple worker processes can run safely.
    """
    now = _utcnow()
    # Raw SQL for reliable SKIP LOCKED across SQLAlchemy versions.
    result = db.execute(
        text(
            """
            SELECT id FROM payflow_workflow_jobs
            WHERE status = :pending
              AND scheduled_at <= :now
            ORDER BY priority ASC, scheduled_at ASC, id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT :lim
            """
        ),
        {
            "pending": PayflowWorkflowJobStatus.PENDING.value,
            "now": now,
            "lim": batch_size,
        },
    )
    ids = [row[0] for row in result.fetchall()]
    if not ids:
        return []

    rows = (
        db.query(PayflowWorkflowJobModel)
        .filter(PayflowWorkflowJobModel.id.in_(ids))
        .all()
    )
    claimed: list[PayflowWorkflowJobModel] = []
    for row in rows:
        row.status = PayflowWorkflowJobStatus.RUNNING.value
        row.locked_by = worker_id
        row.locked_at = now
        row.attempts = int(row.attempts or 0) + 1
        claimed.append(row)
    db.commit()
    return claimed


def mark_job_done(db: Session, job_id: int) -> None:
    row = db.get(PayflowWorkflowJobModel, job_id)
    if not row:
        return
    row.status = PayflowWorkflowJobStatus.DONE.value
    row.locked_by = None
    db.commit()


def mark_job_failed(db: Session, job_id: int, error: str) -> None:
    row = db.get(PayflowWorkflowJobModel, job_id)
    if not row:
        return
    row.last_error = error[:2000]
    max_attempts = int(row.max_attempts or 5)
    if int(row.attempts or 0) >= max_attempts:
        row.status = PayflowWorkflowJobStatus.FAILED.value
        row.locked_by = None
    else:
        # Retry with backoff: attempts * 60 seconds.
        from datetime import timedelta

        row.status = PayflowWorkflowJobStatus.PENDING.value
        row.locked_by = None
        row.scheduled_at = _utcnow() + timedelta(seconds=60 * int(row.attempts or 1))
    db.commit()
