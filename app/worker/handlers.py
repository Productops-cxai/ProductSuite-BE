"""Dispatch claimed workflow jobs to execution services."""

from __future__ import annotations

import logging

from app.infrastructure.database.models import PayflowWorkflowJobModel
from app.infrastructure.database.session import SessionLocal
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.shared.enums import PayflowWorkflowJobType
from app.worker.claim import mark_job_done, mark_job_failed

logger = logging.getLogger("app.worker.handlers")


def process_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(PayflowWorkflowJobModel, job_id)
        if not job:
            return
        svc = WorkflowExecutionService(db)
        job_type = job.job_type
        if job_type == PayflowWorkflowJobType.ENROLL_STRATEGY.value:
            if not job.strategy_id:
                raise ValueError("enroll_strategy missing strategy_id")
            svc.enroll_strategy(job.strategy_id, job_id=job.id)
        elif job_type == PayflowWorkflowJobType.EXECUTE_STEP.value:
            if not job.run_id or not job.step_id:
                raise ValueError("execute_step missing run_id/step_id")
            svc.execute_step(job.run_id, job.step_id, job_id=job.id)
        elif job_type == PayflowWorkflowJobType.RESUME_RUN.value:
            if not job.run_id:
                raise ValueError("resume_run missing run_id")
            svc.resume_run(job.run_id, from_step_id=job.step_id, job_id=job.id)
        elif job_type == PayflowWorkflowJobType.GAP_FILL.value:
            svc.gap_fill_active_strategies()
        else:
            raise ValueError(f"Unknown job_type={job_type}")
        mark_job_done(db, job_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("job %s failed", job_id)
        try:
            mark_job_failed(db, job_id, str(exc))
        except Exception:  # noqa: BLE001
            logger.exception("mark_job_failed also failed for %s", job_id)
    finally:
        db.close()
