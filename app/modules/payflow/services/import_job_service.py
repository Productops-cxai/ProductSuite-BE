"""Async import jobs (validate/upload) with pollable row progress."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.infrastructure.database.models import (
    PayflowImportJobModel,
    PayflowImportRunModel,
    UserModel,
)
from app.infrastructure.database.session import SessionLocal
from app.modules.payflow.services.ai_suggest_job_service import process_ai_suggest_jobs
from app.modules.payflow.services.import_progress import make_progress_cb

logger = logging.getLogger("app.import_jobs")

_BE_ROOT = Path(__file__).resolve().parents[4]
_JOB_UPLOADS = _BE_ROOT / "uploads" / "import_jobs"

_TERMINAL = frozenset({"completed", "failed"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ImportJobService:
    def __init__(self, db: Session):
        self.db = db

    def create_job(
        self,
        *,
        job_type: str,
        file_bytes: bytes,
        filename: str,
        user: UserModel | None,
    ) -> PayflowImportJobModel:
        _JOB_UPLOADS.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename or "upload.xlsx").name
        stored = _JOB_UPLOADS / f"{int(_utcnow().timestamp())}-{safe_name}"
        stored.write_bytes(file_bytes)
        try:
            stored_path = str(stored.relative_to(_BE_ROOT)).replace("\\", "/")
        except ValueError:
            stored_path = str(stored)

        job = PayflowImportJobModel(
            job_type=job_type,
            status="queued",
            file_name=safe_name,
            stored_path=stored_path,
            processed_count=0,
            total_count=0,
            created_by_user_id=user.id if user else None,
        )
        self.db.add(job)
        self.db.flush()
        return job

    def get_job(self, job_id: int, *, owner_id: Any = None) -> dict[str, Any]:
        """``owner_id`` set: only that user's job is found (others look missing)."""
        job = self.db.query(PayflowImportJobModel).filter(PayflowImportJobModel.id == job_id).first()
        if not job or (owner_id is not None and job.created_by_user_id != owner_id):
            raise NotFoundError("Import job not found")
        return self.serialize(job)

    @staticmethod
    def serialize(job: PayflowImportJobModel) -> dict[str, Any]:
        result = None
        if job.result_json:
            try:
                result = json.loads(job.result_json)
            except json.JSONDecodeError:
                result = None
        return {
            "id": job.id,
            "job_type": job.job_type,
            "status": job.status,
            "file_name": job.file_name,
            "processed_count": job.processed_count or 0,
            "total_count": job.total_count or 0,
            "import_run_id": job.import_run_id,
            "error_message": job.error_message,
            "result": result,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        }


def _resolve_stored_bytes(stored_path: str | None) -> bytes:
    if not stored_path:
        raise FileNotFoundError("Import job file path missing")
    path = Path(stored_path)
    if not path.is_absolute():
        path = _BE_ROOT / stored_path
    return path.read_bytes()


def run_import_job(job_id: int) -> None:
    """Background entry: own DB session; updates progress for FE polling."""
    db = SessionLocal()
    try:
        job = db.query(PayflowImportJobModel).filter(PayflowImportJobModel.id == job_id).first()
        if not job:
            return
        if job.status in _TERMINAL:
            return

        job.status = "running"
        job.heartbeat_at = _utcnow()
        db.commit()

        user = None
        if job.created_by_user_id:
            user = db.query(UserModel).filter(UserModel.id == job.created_by_user_id).first()

        data = _resolve_stored_bytes(job.stored_path)
        progress_cb = make_progress_cb(job_id=job.id)
        job_type = job.job_type
        result: dict[str, Any]
        ai_job_ids: list[int] = []

        if job_type == "account_validate":
            from app.modules.payflow.services.import_service import PayflowImportService

            result = PayflowImportService(db).validate_account_file(
                data, job.file_name, progress_cb=progress_cb
            )
        elif job_type == "account_upload":
            from app.modules.payflow.services.import_service import PayflowImportService

            if not user:
                raise RuntimeError("Import user is required for account upload")
            result, ai_job_ids = PayflowImportService(db).process_account_file(
                user, data, job.file_name, progress_cb=progress_cb, job_id=job.id
            )
        elif job_type == "client_validate":
            from app.modules.payflow.services.client_service import PayflowClientService

            result = PayflowClientService(db).bulk_validate(
                data, job.file_name, progress_cb=progress_cb
            )
        elif job_type == "client_upload":
            from app.modules.payflow.services.client_service import PayflowClientService

            result = PayflowClientService(db).bulk_upload(
                data, user=user, filename=job.file_name, progress_cb=progress_cb
            )
        else:
            raise RuntimeError(f"Unknown import job_type={job_type}")

        # Refresh job after long work (other session may have updated counters).
        db.expire_all()
        job = db.query(PayflowImportJobModel).filter(PayflowImportJobModel.id == job_id).first()
        if not job:
            return

        import_run_id = result.get("import_id") or result.get("id")
        if isinstance(import_run_id, int):
            job.import_run_id = import_run_id
        summary = result.get("summary") or result.get("counts") or {}
        total = summary.get("total")
        if isinstance(total, int) and total >= 0:
            job.total_count = total
            job.processed_count = total
        job.result_json = json.dumps(result, default=str)
        job.status = "completed"
        job.completed_at = _utcnow()
        job.error_message = None
        db.commit()

        if ai_job_ids:
            process_ai_suggest_jobs(list(ai_job_ids))
    except Exception as exc:
        logger.exception("import job %s failed", job_id)
        try:
            db.rollback()
        except Exception:
            pass
        try:
            job = db.query(PayflowImportJobModel).filter(PayflowImportJobModel.id == job_id).first()
            if job and job.status not in _TERMINAL:
                job.status = "failed"
                job.error_message = str(exc) or "Import job failed"
                job.completed_at = _utcnow()
                if job.import_run_id:
                    run = db.get(PayflowImportRunModel, job.import_run_id)
                    if run is not None and run.status in ("Uploaded", "Validating", "Processing"):
                        run.status = "Failed"
                        run.completed_at = _utcnow()
                db.commit()
        except Exception:
            logger.exception("import job %s failed to mark failed", job_id)
    finally:
        db.close()


def schedule_import_job(background_tasks: Any, job_id: int) -> None:
    background_tasks.add_task(run_import_job, int(job_id))
