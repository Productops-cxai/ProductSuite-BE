"""Enqueue helpers for the PostgreSQL workflow due-queue + LISTEN/NOTIFY."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import PayflowWorkflowJobModel
from app.shared.enums import PayflowWorkflowJobStatus, PayflowWorkflowJobType


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enqueue_job(
    db: Session,
    *,
    job_type: str,
    scheduled_at: datetime | None = None,
    run_id: int | None = None,
    strategy_id: int | None = None,
    step_id: str | None = None,
    account_id: int | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    notify: bool = True,
    commit: bool = False,
) -> PayflowWorkflowJobModel:
    row = PayflowWorkflowJobModel(
        job_type=job_type,
        run_id=run_id,
        strategy_id=strategy_id,
        step_id=step_id,
        account_id=account_id,
        payload=payload,
        status=PayflowWorkflowJobStatus.PENDING.value,
        priority=priority,
        scheduled_at=scheduled_at or _utcnow(),
    )
    db.add(row)
    db.flush()
    if notify:
        channel = settings.WORKFLOW_JOB_NOTIFY_CHANNEL or "workflow_jobs"
        db.execute(text(f"NOTIFY {channel}, :payload"), {"payload": str(row.id)})
    if commit:
        db.commit()
        db.refresh(row)
    return row


def enqueue_enroll_strategy(db: Session, strategy_id: int, *, commit: bool = True) -> PayflowWorkflowJobModel:
    return enqueue_job(
        db,
        job_type=PayflowWorkflowJobType.ENROLL_STRATEGY.value,
        strategy_id=strategy_id,
        priority=50,
        commit=commit,
    )


def enqueue_execute_step(
    db: Session,
    *,
    run_id: int,
    step_id: str,
    account_id: int | None = None,
    strategy_id: int | None = None,
    scheduled_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
    commit: bool = False,
) -> PayflowWorkflowJobModel:
    return enqueue_job(
        db,
        job_type=PayflowWorkflowJobType.EXECUTE_STEP.value,
        run_id=run_id,
        step_id=step_id,
        account_id=account_id,
        strategy_id=strategy_id,
        scheduled_at=scheduled_at,
        payload=payload,
        priority=100,
        commit=commit,
    )


def enqueue_resume_run(
    db: Session,
    *,
    run_id: int,
    step_id: str | None = None,
    account_id: int | None = None,
    payload: dict[str, Any] | None = None,
    commit: bool = True,
) -> PayflowWorkflowJobModel:
    return enqueue_job(
        db,
        job_type=PayflowWorkflowJobType.RESUME_RUN.value,
        run_id=run_id,
        step_id=step_id,
        account_id=account_id,
        payload=payload,
        priority=40,
        commit=commit,
    )


def cancel_pending_jobs_for_run(db: Session, run_id: int) -> int:
    rows = (
        db.query(PayflowWorkflowJobModel)
        .filter(
            PayflowWorkflowJobModel.run_id == run_id,
            PayflowWorkflowJobModel.status == PayflowWorkflowJobStatus.PENDING.value,
        )
        .all()
    )
    for row in rows:
        row.status = PayflowWorkflowJobStatus.CANCELLED.value
    return len(rows)
