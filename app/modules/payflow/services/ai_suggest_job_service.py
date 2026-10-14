"""Enqueue and run auto AI workflow suggestions; Platform lists the job history."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import AppError
from app.infrastructure.database.models import (
    PayflowAiSuggestJobModel,
    PayflowClientModel,
    PayflowPortfolioModel,
    UserModel,
)
from app.infrastructure.database.session import SessionLocal
from app.modules.payflow.services.workflow_suggestion.context import load_portfolio_accounts
from app.modules.payflow.services.workflow_suggestion.gates import (
    evaluate_suggest_gates,
    portfolio_has_ai_workflows,
)
from app.modules.payflow.services.workflow_suggestion.segmenter import build_segments
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService, _batches

logger = logging.getLogger("uvicorn.error")

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

# Do not enqueue another job while one is waiting or in flight.
_BLOCKING_STATUSES = {STATUS_PENDING, STATUS_RUNNING}
# Successful completion — idempotent until manually cleared (new AI rows would duplicate).
_DONE_STATUS = STATUS_DONE


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AiSuggestJobService:
    def __init__(self, db: Session):
        self.db = db

    def enqueue(
        self,
        client_id: int,
        portfolio_id: int,
        *,
        trigger: str,
        requested_by_user_id: uuid.UUID | None = None,
        import_run_id: int | None = None,
    ) -> PayflowAiSuggestJobModel | None:
        """Create a pending job unless one is already pending/running/done for this portfolio."""
        existing = (
            self.db.query(PayflowAiSuggestJobModel)
            .filter(
                PayflowAiSuggestJobModel.portfolio_id == portfolio_id,
                PayflowAiSuggestJobModel.status.in_([*_BLOCKING_STATUSES, _DONE_STATUS]),
            )
            .order_by(PayflowAiSuggestJobModel.id.desc())
            .first()
        )
        if existing:
            logger.info(
                "AI suggest skip enqueue portfolio=%s status=%s trigger=%s",
                portfolio_id,
                existing.status,
                trigger,
            )
            return None

        job = PayflowAiSuggestJobModel(
            client_id=client_id,
            portfolio_id=portfolio_id,
            trigger=trigger,
            status=STATUS_PENDING,
            requested_by_user_id=requested_by_user_id,
            import_run_id=import_run_id,
        )
        self.db.add(job)
        self.db.flush()
        logger.info(
            "AI suggest enqueued job=%s client=%s portfolio=%s trigger=%s",
            job.id,
            client_id,
            portfolio_id,
            trigger,
        )
        return job

    def enqueue_many(
        self,
        pairs: Iterable[tuple[int, int]],
        *,
        trigger: str,
        requested_by_user_id: uuid.UUID | None = None,
        import_run_id: int | None = None,
    ) -> list[int]:
        ids: list[int] = []
        seen: set[int] = set()
        for client_id, portfolio_id in pairs:
            if portfolio_id in seen:
                continue
            seen.add(portfolio_id)
            job = self.enqueue(
                client_id,
                portfolio_id,
                trigger=trigger,
                requested_by_user_id=requested_by_user_id,
                import_run_id=import_run_id,
            )
            if job:
                ids.append(job.id)
        return ids

    def enqueue_for_client(
        self,
        client_id: int,
        *,
        trigger: str,
        requested_by_user_id: uuid.UUID | None = None,
    ) -> list[int]:
        portfolios = (
            self.db.query(PayflowPortfolioModel)
            .filter(PayflowPortfolioModel.client_id == client_id)
            .all()
        )
        return self.enqueue_many(
            [(client_id, p.id) for p in portfolios],
            trigger=trigger,
            requested_by_user_id=requested_by_user_id,
        )

    def process(self, job_id: int) -> None:
        job = (
            self.db.query(PayflowAiSuggestJobModel)
            .filter(PayflowAiSuggestJobModel.id == job_id)
            .first()
        )
        if not job:
            logger.warning("AI suggest job=%s not found", job_id)
            return
        if job.status != STATUS_PENDING:
            logger.info("AI suggest job=%s not pending (%s)", job_id, job.status)
            return

        job.status = STATUS_RUNNING
        job.started_at = _utcnow()
        self.db.commit()

        t0 = time.perf_counter()
        try:
            gates = evaluate_suggest_gates(self.db, job.client_id, job.portfolio_id)
            if not gates.ok:
                self._finish(
                    job,
                    STATUS_SKIPPED,
                    skip_reason=gates.reason,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
                logger.info(
                    "AI suggest job=%s skipped: %s",
                    job.id,
                    gates.reason,
                )
                return

            if portfolio_has_ai_workflows(self.db, job.client_id, job.portfolio_id):
                self._finish(
                    job,
                    STATUS_SKIPPED,
                    skip_reason="Portfolio already has AI-generated workflows",
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
                logger.info("AI suggest job=%s skipped: already has AI workflows", job.id)
                return

            user = self._resolve_user(job)
            if not user:
                self._finish(
                    job,
                    STATUS_FAILED,
                    error_message="No user available to run AI suggest (missing requested_by_user_id)",
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
                return

            _, accounts = load_portfolio_accounts(self.db, job.client_id, job.portfolio_id)
            segments = build_segments(accounts)["segments"]
            batches = _batches(segments)
            job.segments_count = len(segments)
            job.batches_count = len(batches)
            self.db.commit()

            result = WorkflowSuggestionService(self.db).suggest_workflows(
                user, job.client_id, job.portfolio_id
            )
            workflows = result.get("workflows") or []
            ids = [w.get("id") for w in workflows if isinstance(w, dict) and w.get("id") is not None]
            # Approximate: one primary LLM call per batch (retries not counted precisely).
            job.groq_calls = len(batches)
            self._finish(
                job,
                STATUS_DONE,
                workflows_created=len(ids),
                workflow_ids=ids,
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
            logger.info(
                "AI suggest job=%s done workflows=%s ids=%s duration_ms=%s",
                job.id,
                len(ids),
                ids,
                job.duration_ms,
            )
        except AppError as exc:
            self.db.rollback()
            job = self.db.query(PayflowAiSuggestJobModel).filter_by(id=job_id).first()
            if job:
                self._finish(
                    job,
                    STATUS_FAILED,
                    error_message=str(exc.message),
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            logger.exception("AI suggest job=%s failed: %s", job_id, exc.message)
        except Exception as exc:
            self.db.rollback()
            job = self.db.query(PayflowAiSuggestJobModel).filter_by(id=job_id).first()
            if job:
                self._finish(
                    job,
                    STATUS_FAILED,
                    error_message=str(exc)[:2000],
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            logger.exception("AI suggest job=%s failed unexpectedly", job_id)

    def list_jobs(
        self,
        *,
        search: str | None = None,
        status: str | None = None,
        client_id: int | None = None,
        portfolio_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        q = (
            self.db.query(PayflowAiSuggestJobModel)
            .options(
                joinedload(PayflowAiSuggestJobModel.client),
                joinedload(PayflowAiSuggestJobModel.portfolio),
            )
            .order_by(PayflowAiSuggestJobModel.id.desc())
        )
        if status:
            q = q.filter(PayflowAiSuggestJobModel.status == status)
        if client_id is not None:
            q = q.filter(PayflowAiSuggestJobModel.client_id == client_id)
        if portfolio_id is not None:
            q = q.filter(PayflowAiSuggestJobModel.portfolio_id == portfolio_id)
        if search:
            like = f"%{search.strip()}%"
            q = (
                q.outerjoin(PayflowClientModel, PayflowAiSuggestJobModel.client_id == PayflowClientModel.id)
                .outerjoin(
                    PayflowPortfolioModel,
                    PayflowAiSuggestJobModel.portfolio_id == PayflowPortfolioModel.id,
                )
                .filter(
                    or_(
                        PayflowClientModel.name.ilike(like),
                        PayflowClientModel.code.ilike(like),
                        PayflowPortfolioModel.name.ilike(like),
                        PayflowPortfolioModel.code.ilike(like),
                        PayflowAiSuggestJobModel.trigger.ilike(like),
                        PayflowAiSuggestJobModel.skip_reason.ilike(like),
                        PayflowAiSuggestJobModel.error_message.ilike(like),
                        PayflowAiSuggestJobModel.status.ilike(like),
                    )
                )
                .distinct()
            )
        rows = q.limit(min(limit, 500)).all()
        return [self._serialize(r) for r in rows]

    def _resolve_user(self, job: PayflowAiSuggestJobModel) -> UserModel | None:
        if job.requested_by_user_id:
            return (
                self.db.query(UserModel)
                .filter(UserModel.id == job.requested_by_user_id)
                .first()
            )
        return None

    def _finish(
        self,
        job: PayflowAiSuggestJobModel,
        status: str,
        *,
        skip_reason: str | None = None,
        error_message: str | None = None,
        workflows_created: int = 0,
        workflow_ids: list | None = None,
        duration_ms: int | None = None,
    ) -> None:
        job.status = status
        job.skip_reason = skip_reason
        job.error_message = error_message
        job.workflows_created = workflows_created
        job.workflow_ids = workflow_ids
        job.finished_at = _utcnow()
        job.duration_ms = duration_ms
        self.db.commit()

    def _serialize(self, job: PayflowAiSuggestJobModel) -> dict[str, Any]:
        client = job.client
        portfolio = job.portfolio
        return {
            "id": job.id,
            "client_id": job.client_id,
            "client_name": client.name if client else None,
            "client_code": client.code if client else None,
            "portfolio_id": job.portfolio_id,
            "portfolio_name": portfolio.name if portfolio else None,
            "portfolio_code": portfolio.code if portfolio else None,
            "trigger": job.trigger,
            "status": job.status,
            "skip_reason": job.skip_reason,
            "error_message": job.error_message,
            "requested_by_user_id": str(job.requested_by_user_id) if job.requested_by_user_id else None,
            "import_run_id": job.import_run_id,
            "workflows_created": job.workflows_created,
            "workflow_ids": list(job.workflow_ids or []),
            "segments_count": job.segments_count,
            "batches_count": job.batches_count,
            "groq_calls": job.groq_calls,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "duration_ms": job.duration_ms,
            "created_at": job.created_at,
        }


def process_ai_suggest_jobs(job_ids: list[int]) -> None:
    """Background entry: each job gets its own DB session."""
    for job_id in job_ids:
        db = SessionLocal()
        try:
            AiSuggestJobService(db).process(job_id)
        finally:
            db.close()


def schedule_ai_suggest_jobs(background_tasks: Any, job_ids: list[int]) -> None:
    """Attach processing to FastAPI BackgroundTasks when there is work."""
    if not job_ids:
        return
    background_tasks.add_task(process_ai_suggest_jobs, list(job_ids))
