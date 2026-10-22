"""Claim due workflow jobs with SELECT … FOR UPDATE SKIP LOCKED."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.infrastructure.database.models import PayflowWorkflowJobModel
from app.shared.enums import PayflowWorkflowJobStatus

logger = logging.getLogger("app.worker.claim")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def reclaim_stale_running_jobs(
    db: Session,
    *,
    stale_seconds: int = 300,
    batch_size: int = 200,
) -> list[int]:
    """
    Reset RUNNING jobs whose lock is older than stale_seconds back to PENDING.

    Covers worker crashes / restarts that left jobs locked. scheduled_at is set
    to now so the next claim_due_jobs sweep can pick them up immediately.
    """
    if stale_seconds <= 0:
        return []

    now = _utcnow()
    cutoff = now - timedelta(seconds=stale_seconds)
    result = db.execute(
        text(
            """
            UPDATE payflow_workflow_jobs
            SET status = :pending,
                locked_by = NULL,
                locked_at = NULL,
                scheduled_at = :now,
                last_error = :err,
                updated_at = :now
            WHERE id IN (
                SELECT id FROM payflow_workflow_jobs
                WHERE status = :running
                  AND COALESCE(locked_at, updated_at, created_at) < :cutoff
                ORDER BY COALESCE(locked_at, updated_at, created_at) ASC
                FOR UPDATE SKIP LOCKED
                LIMIT :lim
            )
            RETURNING id
            """
        ),
        {
            "pending": PayflowWorkflowJobStatus.PENDING.value,
            "running": PayflowWorkflowJobStatus.RUNNING.value,
            "now": now,
            "cutoff": cutoff,
            "lim": batch_size,
            "err": "reclaimed: stale running lock",
        },
    )
    ids = [row[0] for row in result.fetchall()]
    if ids:
        db.commit()
        logger.warning(
            "reclaimed %s stale RUNNING job(s) older than %ss: %s",
            len(ids),
            stale_seconds,
            ids[:20],
        )
    else:
        db.rollback()
    return ids


def claim_due_jobs(
    db: Session,
    *,
    worker_id: str,
    batch_size: int = 50,
) -> list[int]:
    """
    Atomically claim up to batch_size pending jobs that are due.
    Uses SKIP LOCKED so multiple worker processes can run safely.
    Returns claimed job IDs (plain ints — safe after session close).
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
    for row in rows:
        row.status = PayflowWorkflowJobStatus.RUNNING.value
        row.locked_by = worker_id
        row.locked_at = now
        row.attempts = int(row.attempts or 0) + 1
    db.commit()
    # Return the pre-commit id list; ORM attrs expire on commit and detach on close.
    return ids


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
