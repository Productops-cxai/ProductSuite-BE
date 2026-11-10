"""Row-level import progress reporting (separate DB session so the UI can poll)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.infrastructure.database.models import PayflowImportJobModel, PayflowImportRunModel
from app.infrastructure.database.session import SessionLocal

ProgressCb = Callable[[int, int], None]


def make_progress_cb(
    *,
    job_id: int | None = None,
    run_id: int | None = None,
) -> ProgressCb | None:
    if job_id is None and run_id is None:
        return None

    def _cb(processed: int, total: int) -> None:
        report_import_progress(job_id=job_id, run_id=run_id, processed=processed, total=total)

    return _cb


def report_import_progress(
    *,
    job_id: int | None = None,
    run_id: int | None = None,
    processed: int,
    total: int | None = None,
) -> None:
    """Commit progress in a short-lived session (safe from long import transactions)."""
    if job_id is None and run_id is None:
        return
    db = SessionLocal()
    try:
        if job_id is not None:
            job = db.query(PayflowImportJobModel).filter(PayflowImportJobModel.id == job_id).first()
            if job:
                job.processed_count = max(0, int(processed))
                job.heartbeat_at = datetime.now(timezone.utc)
                if total is not None and int(total) >= 0:
                    job.total_count = int(total)
                if job.status == "queued":
                    job.status = "running"
        if run_id is not None:
            run = db.query(PayflowImportRunModel).filter(PayflowImportRunModel.id == run_id).first()
            if run:
                run.processed_count = max(0, int(processed))
                if total is not None and int(total) >= 0:
                    run.total_count = int(total)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def tick_progress(
    progress_cb: ProgressCb | None,
    done: int,
    total: int,
    *,
    force: bool = False,
) -> None:
    """Throttle progress writes (~1% steps) unless force/last row."""
    if not progress_cb or total <= 0:
        return
    step = max(1, total // 100)
    if force or done >= total or done % step == 0 or done == 1:
        progress_cb(min(done, total), total)

