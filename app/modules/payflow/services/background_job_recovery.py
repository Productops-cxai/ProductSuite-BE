"""Recover import / AI-suggest jobs that an API restart cut off.

Both job kinds run inside the API process (FastAPI BackgroundTasks). When that
process restarts mid-job, nothing finishes the row: an import stays "running" on
the progress screen forever, and a "pending" / "running" AI-suggest job blocks
every later suggest for its client or portfolio.

The workflow worker calls ``recover_interrupted_jobs`` on its sweep.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import (
    PayflowAiSuggestJobModel,
    PayflowImportJobModel,
    PayflowImportRunModel,
)

IMPORT_INTERRUPTED_MESSAGE = (
    "Import was interrupted (server restarted) before it finished. "
    "Rows already processed were saved. Please upload the file again."
)
AI_INTERRUPTED_MESSAGE = (
    "AI suggest was interrupted (server restarted) before it finished. Nothing was saved."
)

_IMPORT_OPEN = ("queued", "running")
_IMPORT_RUN_OPEN = ("Uploaded", "Validating", "Processing")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def recover_interrupted_jobs(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or _utcnow()
    result: dict[str, Any] = {
        "import_jobs_failed": 0,
        "ai_jobs_failed": 0,
        "ai_jobs_to_run": [],
    }

    # Imports report progress row by row; silence for this long means the process died.
    import_cutoff = now - timedelta(minutes=max(1, int(settings.IMPORT_JOB_STALE_MINUTES or 15)))
    stale_imports = (
        db.query(PayflowImportJobModel)
        .filter(
            PayflowImportJobModel.status.in_(_IMPORT_OPEN),
            func.coalesce(PayflowImportJobModel.heartbeat_at, PayflowImportJobModel.created_at)
            < import_cutoff,
        )
        .all()
    )
    for job in stale_imports:
        job.status = "failed"
        job.error_message = IMPORT_INTERRUPTED_MESSAGE
        job.completed_at = now
        if job.import_run_id:
            run = db.get(PayflowImportRunModel, job.import_run_id)
            if run is not None and run.status in _IMPORT_RUN_OPEN:
                run.status = "Failed"
                run.completed_at = now
        result["import_jobs_failed"] += 1

    # One AI suggest takes minutes; well past that, a "running" job is dead.
    ai_cutoff = now - timedelta(minutes=max(1, int(settings.AI_SUGGEST_JOB_STALE_MINUTES or 30)))
    stale_running = (
        db.query(PayflowAiSuggestJobModel)
        .filter(
            PayflowAiSuggestJobModel.status == "running",
            func.coalesce(PayflowAiSuggestJobModel.started_at, PayflowAiSuggestJobModel.created_at)
            < ai_cutoff,
        )
        .all()
    )
    for job in stale_running:
        job.status = "failed"
        job.error_message = AI_INTERRUPTED_MESSAGE
        job.finished_at = now
        result["ai_jobs_failed"] += 1

    # Never started: the process that queued it is gone. Hand it back to be run.
    stale_pending = (
        db.query(PayflowAiSuggestJobModel.id)
        .filter(
            PayflowAiSuggestJobModel.status == "pending",
            PayflowAiSuggestJobModel.created_at < ai_cutoff,
        )
        .order_by(PayflowAiSuggestJobModel.id.asc())
        .all()
    )
    result["ai_jobs_to_run"] = [row[0] for row in stale_pending]

    db.commit()
    return result
