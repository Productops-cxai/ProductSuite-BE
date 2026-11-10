"""Dispatch claimed workflow jobs to execution services."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.infrastructure.database.models import PayflowWorkflowJobModel
from app.infrastructure.database.session import SessionLocal
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.shared.enums import PayflowWorkflowJobStatus, PayflowWorkflowJobType
from app.worker.claim import mark_job_done, mark_job_failed

logger = logging.getLogger("app.worker.handlers")


def process_job(job_id: int, worker_id: str | None = None) -> None:
    db = SessionLocal()
    try:
        job = db.get(PayflowWorkflowJobModel, job_id)
        if not job:
            return
        # The job waited in this worker's pool after being claimed. If its lock went
        # stale meanwhile it was handed back (pending) or to another worker: running
        # it here too would execute the step twice.
        if job.status != PayflowWorkflowJobStatus.RUNNING.value or (
            worker_id is not None and job.locked_by != worker_id
        ):
            logger.warning(
                "job %s no longer owned by this worker (status=%s locked_by=%s) — skipped",
                job_id,
                job.status,
                job.locked_by,
            )
            return
        # The stale window counts from when work starts, not from when it was claimed.
        job.locked_at = datetime.now(timezone.utc)
        db.commit()
        svc = WorkflowExecutionService(db)
        job_type = job.job_type
        if job_type == PayflowWorkflowJobType.ENROLL_STRATEGY.value:
            if not job.strategy_id:
                raise ValueError("enroll_strategy missing strategy_id")
            svc.enroll_strategy(job.strategy_id, job_id=job.id)
        elif job_type == PayflowWorkflowJobType.EXECUTE_STEP.value:
            if not job.run_id or not job.step_id:
                raise ValueError("execute_step missing run_id/step_id")
            payload = job.payload or {}
            svc.execute_step(
                job.run_id,
                job.step_id,
                job_id=job.id,
                after_human_review=bool(payload.get("after_human_review")),
                after_comm_delay=bool(payload.get("after_comm_delay")),
            )
        elif job_type == PayflowWorkflowJobType.RESUME_RUN.value:
            if not job.run_id:
                raise ValueError("resume_run missing run_id")
            svc.resume_run(
                job.run_id,
                from_step_id=job.step_id,
                job_id=job.id,
                after_human_review=bool((job.payload or {}).get("after_human_review")),
            )
        elif job_type == PayflowWorkflowJobType.GAP_FILL.value:
            svc.gap_fill_active_strategies(job_id=job.id)
        else:
            raise ValueError(f"Unknown job_type={job_type}")
        mark_job_done(db, job_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("job %s failed", job_id)
        try:
            # Flush/UniqueViolation leaves the session needing rollback before
            # any further work (including mark_job_failed).
            db.rollback()
            if mark_job_failed(db, job_id, str(exc)):
                # No attempts left: without this the run would sit Active forever
                # with nothing scheduled and nobody told.
                WorkflowExecutionService(db).on_job_failed_for_good(job_id, str(exc))
        except Exception:  # noqa: BLE001
            logger.exception("mark_job_failed also failed for %s", job_id)
    finally:
        db.close()
