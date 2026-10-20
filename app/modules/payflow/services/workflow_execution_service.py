"""Workflow enrollment, step advance, regenerate supersede, and worker log helpers."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowPortfolioModel,
    PayflowStrategyModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
    PayflowWorkflowStepRunModel,
    PayflowWorkflowWorkerLogModel,
)
from app.modules.payflow.services.communication_service import PayflowCommunicationService
from app.modules.payflow.services.rule_runtime_service import RuleRuntimeService
from app.modules.payflow.services.strategy_service import MESSAGE_TEMPLATES
from app.modules.payflow.services.workflow_queue import (
    cancel_pending_jobs_for_run,
    enqueue_enroll_strategy,
    enqueue_execute_step,
    enqueue_resume_run,
)
from app.modules.payflow.services.workflow_suggestion.segmenter import account_fits_segment
from app.shared.enums import (
    PayflowCollectionStatus,
    PayflowCommStatus,
    PayflowNotificationType,
    PayflowWorkflowJobStatus,
    PayflowWorkflowJobType,
    PayflowWorkflowRunStatus,
)

logger = logging.getLogger("app.workflow.execution")

# Explicit deactivate policy (story AC): cancel open runs + pending jobs; no silent drop.
DEACTIVATE_POLICY = "cancel_in_flight"

_TERMINAL = {
    PayflowWorkflowRunStatus.COMPLETED.value,
    PayflowWorkflowRunStatus.CANCELLED.value,
    PayflowWorkflowRunStatus.FAILED.value,
    PayflowWorkflowRunStatus.SUPERSEDED.value,
}

# Include terminal step outcomes so duplicate worker jobs cannot re-run the same
# step (and re-notify / re-send) after it already finished or paused.
_STEP_DONE_STATUSES = frozenset(
    {
        "done",
        "skipped",
        "blocked",
        "held",
        "cancelled",
        "paused_human_review",
        "reassessment_requested",
    }
)

_UI_STEP_STATUS = {
    "pending": "Pending",
    "scheduled": "Scheduled",
    "running": "In Progress",
    "done": "Completed",
    "skipped": "Completed",
    "blocked": "Blocked",
    "held": "Blocked",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "reassessment_requested": "Scheduled",
    "paused_human_review": "Awaiting Review",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cfg(step: dict) -> dict:
    cfg = step.get("config")
    return cfg if isinstance(cfg, dict) else {}


def _account_execution_block_reason(account: PayflowAccountModel) -> str | None:
    """Return why collection actions must not proceed, else None."""
    status = (account.collection_status or "").strip()
    if status == PayflowCollectionStatus.RESOLVED.value:
        return "Case is Resolved — further collection actions are blocked"
    if status.lower() in {"closed", "ineligible"}:
        return f"Case status '{status}' — further collection actions are blocked"
    if account.provincial_hold:
        return "Provincial / communication hold is active on this case"
    hold_days = account.hold_days
    if hold_days is not None:
        try:
            if int(hold_days) > 0 and status.lower() in {"on hold", "hold"}:
                return f"Case is on hold ({hold_days} days)"
        except (TypeError, ValueError):
            pass
    if status.lower() in {"on hold", "hold"}:
        return "Case is on hold"
    return None


# Headline ranking for grouped list rows (higher = more important to show).
_LEVEL_RANK = {"error": 3, "warn": 2, "info": 1}
_EVENT_RANK = {
    "run_failed": 100,
    "step_failed": 95,
    "human_review_paused": 90,
    "rule_matched": 80,
    "email_skipped": 70,
    "sms_skipped": 70,
    "run_superseded": 65,
    "run_cancelled": 60,
    "human_review_cancelled_run": 55,
    "account_enrolled": 50,
    "step_done": 45,
    "run_resumed": 40,
    "human_review_continue": 38,
    "enroll_complete": 30,
    "enroll_queued": 20,
    "step_skipped": 15,
}
# Strategy bookends (no account) — keep in DB/detail, hide from the main list.
_LIST_HIDE_NO_ACCOUNT_EVENTS = frozenset({"enroll_queued", "enroll_complete"})


class WorkflowExecutionService:
    def __init__(self, db: Session):
        self.db = db
        self.rules = RuleRuntimeService(db)

    # ------------------------------------------------------------------ logging
    def log_event(
        self,
        *,
        event_type: str,
        message: str,
        level: str = "info",
        detail: str | None = None,
        client_id: int | None = None,
        portfolio_id: int | None = None,
        account_id: int | None = None,
        strategy_id: int | None = None,
        run_id: int | None = None,
        job_id: int | None = None,
        step_id: str | None = None,
        commit: bool = False,
    ) -> PayflowWorkflowWorkerLogModel:
        # Always stamp client / portfolio / account from the run when possible
        # so the list is scannable (no "—" for account-level events).
        if run_id is not None and (
            client_id is None
            or portfolio_id is None
            or account_id is None
            or strategy_id is None
        ):
            run = self.db.get(PayflowWorkflowRunModel, run_id)
            if run:
                client_id = client_id if client_id is not None else run.client_id
                portfolio_id = portfolio_id if portfolio_id is not None else run.portfolio_id
                account_id = account_id if account_id is not None else run.account_id
                strategy_id = strategy_id if strategy_id is not None else run.strategy_id
        if account_id is not None and (client_id is None or portfolio_id is None):
            account = self.db.get(PayflowAccountModel, account_id)
            if account:
                client_id = client_id if client_id is not None else account.client_id
                portfolio_id = (
                    portfolio_id if portfolio_id is not None else account.portfolio_id
                )

        row = PayflowWorkflowWorkerLogModel(
            event_type=event_type,
            level=level,
            message=message[:512],
            detail=detail,
            client_id=client_id,
            portfolio_id=portfolio_id,
            account_id=account_id,
            strategy_id=strategy_id,
            run_id=run_id,
            job_id=job_id,
            step_id=step_id,
        )
        self.db.add(row)
        if commit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    # ------------------------------------------------------------------ activate hooks
    def on_strategy_activated(self, strategy_id: int) -> PayflowWorkflowJobModel:
        """Supersede prior active runs for same segment scope, then enqueue enroll."""
        strategy = self.db.get(PayflowStrategyModel, strategy_id)
        if not strategy:
            raise ValueError(f"strategy {strategy_id} not found")
        self._supersede_prior_for_strategy(strategy)
        job = enqueue_enroll_strategy(self.db, strategy_id, commit=False)
        self.log_event(
            event_type="enroll_queued",
            message=f"Enroll queued for strategy {strategy.code}",
            strategy_id=strategy_id,
            client_id=strategy.client_id,
            portfolio_id=strategy.portfolio_id,
            job_id=job.id,
        )
        self.db.commit()
        return job

    def on_strategy_deactivated(self, strategy_id: int) -> dict[str, Any]:
        """Deactivate policy: cancel_in_flight — stop open runs; block new enroll via Inactive status."""
        runs = (
            self.db.query(PayflowWorkflowRunModel)
            .filter(
                PayflowWorkflowRunModel.strategy_id == strategy_id,
                PayflowWorkflowRunModel.status.notin_(list(_TERMINAL)),
            )
            .all()
        )
        cancelled = 0
        for run in runs:
            run.status = PayflowWorkflowRunStatus.CANCELLED.value
            run.completed_at = _utcnow()
            cancel_pending_jobs_for_run(self.db, run.id)
            cancelled += 1
            self.log_event(
                event_type="run_cancelled",
                message=(
                    f"Run cancelled — strategy deactivated "
                    f"(policy={DEACTIVATE_POLICY})"
                ),
                run_id=run.id,
                account_id=run.account_id,
                strategy_id=strategy_id,
                client_id=run.client_id,
            )
        self.db.commit()
        return {
            "cancelled": cancelled,
            "policy": DEACTIVATE_POLICY,
            "message": (
                "Existing in-flight executions were cancelled; "
                "deactivated workflows cannot receive new assignments."
            ),
        }

    def _supersede_prior_for_strategy(self, strategy: PayflowStrategyModel) -> None:
        """When activating a regenerated (or replacement) workflow, supersede older runs."""
        q = self.db.query(PayflowStrategyModel).filter(
            PayflowStrategyModel.client_id == strategy.client_id,
            PayflowStrategyModel.id != strategy.id,
            PayflowStrategyModel.status == "Active",
        )
        if strategy.portfolio_id is not None:
            q = q.filter(PayflowStrategyModel.portfolio_id == strategy.portfolio_id)
        # Prefer strategies linked via revision chain or same segment label.
        priors = q.all()
        prior_ids: set[int] = set()
        if strategy.revision_of_id:
            prior_ids.add(strategy.revision_of_id)
            parent = self.db.get(PayflowStrategyModel, strategy.revision_of_id)
            while parent and parent.revision_of_id:
                prior_ids.add(parent.revision_of_id)
                parent = self.db.get(PayflowStrategyModel, parent.revision_of_id)
        for p in priors:
            if p.id in prior_ids or self._segment_compatible(p.segment, strategy.segment):
                prior_ids.add(p.id)

        if not prior_ids:
            return

        runs = (
            self.db.query(PayflowWorkflowRunModel)
            .filter(
                PayflowWorkflowRunModel.strategy_id.in_(list(prior_ids)),
                PayflowWorkflowRunModel.status.notin_(list(_TERMINAL)),
            )
            .all()
        )
        for run in runs:
            run.status = PayflowWorkflowRunStatus.SUPERSEDED.value
            run.completed_at = _utcnow()
            cancel_pending_jobs_for_run(self.db, run.id)
            if run.human_review_id:
                self._close_superseded_review(run.human_review_id)
            self.log_event(
                event_type="run_superseded",
                message=f"Run superseded by strategy {strategy.code}",
                run_id=run.id,
                account_id=run.account_id,
                strategy_id=run.strategy_id,
                client_id=run.client_id,
            )
            self._notify_superseded(run, strategy)

        # Deactivate prior Active strategies that we superseded.
        for sid in prior_ids:
            row = self.db.get(PayflowStrategyModel, sid)
            if row and row.status == "Active":
                row.status = "Inactive"

    @staticmethod
    def _segment_compatible(a: dict | None, b: dict | None) -> bool:
        if not a or not b:
            return False
        keys = ("situation", "delinquency", "contact_state", "balance_band")
        return all((a.get(k) or "") == (b.get(k) or "") for k in keys)

    def _close_superseded_review(self, review_id: int) -> None:
        from app.infrastructure.database.models import PayflowHumanReviewModel
        from app.shared.enums import PayflowReviewStatus

        review = self.db.get(PayflowHumanReviewModel, review_id)
        if not review:
            return
        if review.status in {
            PayflowReviewStatus.AWAITING_REVIEW.value,
            PayflowReviewStatus.ON_HOLD.value,
        }:
            review.status = PayflowReviewStatus.REJECTED.value
            history = list(review.history or [])
            history.append(
                {
                    "at": _utcnow().isoformat(),
                    "event": "Superseded by regenerated workflow",
                    "by": "Workflow worker",
                }
            )
            review.history = history

    def _notify_superseded(self, run: PayflowWorkflowRunModel, new_strategy: PayflowStrategyModel) -> None:
        try:
            from app.modules.payflow.services.notification_service import PayflowNotificationService

            svc = PayflowNotificationService(self.db)
            users = svc.recipients_for_client(run.client_id)
            svc.notify(
                users=users,
                notification_type=PayflowNotificationType.WORKFLOW_RUN_SUPERSEDED.value,
                title="Workflow run superseded",
                body=f"Account run replaced by {new_strategy.name}",
                link=f"/payflow/cases/{run.account_id}",
                client_id=run.client_id,
                entity_type="workflow_run",
                entity_id=str(run.id),
                dedupe=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("notify superseded failed run=%s", run.id)

    # ------------------------------------------------------------------ enroll
    def enroll_strategy(self, strategy_id: int, *, job_id: int | None = None) -> dict[str, Any]:
        strategy = self.db.get(PayflowStrategyModel, strategy_id)
        if not strategy:
            return {"enrolled": 0, "skipped": 0, "error": "strategy_not_found"}
        if strategy.status != "Active":
            return {"enrolled": 0, "skipped": 0, "error": "strategy_not_active"}

        q = self.db.query(PayflowAccountModel).filter(
            PayflowAccountModel.client_id == strategy.client_id
        )
        if strategy.portfolio_id is not None:
            q = q.filter(PayflowAccountModel.portfolio_id == strategy.portfolio_id)
        accounts = q.all()

        enrolled = 0
        skipped = 0
        today = datetime.now(timezone.utc).date()
        for account in accounts:
            block = _account_execution_block_reason(account)
            if block:
                skipped += 1
                self.log_event(
                    event_type="enroll_skipped",
                    message=block,
                    level="warn",
                    account_id=account.id,
                    strategy_id=strategy.id,
                    client_id=strategy.client_id,
                    portfolio_id=strategy.portfolio_id or account.portfolio_id,
                    job_id=job_id,
                )
                continue
            if strategy.segment and not account_fits_segment(account, strategy.segment, today):
                skipped += 1
                continue
            existing = (
                self.db.query(PayflowWorkflowRunModel)
                .filter(
                    PayflowWorkflowRunModel.account_id == account.id,
                    PayflowWorkflowRunModel.strategy_id == strategy.id,
                    PayflowWorkflowRunModel.status.notin_(list(_TERMINAL)),
                )
                .first()
            )
            if existing:
                skipped += 1
                continue
            # Cancel other active runs on this account for same portfolio scope.
            other = (
                self.db.query(PayflowWorkflowRunModel)
                .filter(
                    PayflowWorkflowRunModel.account_id == account.id,
                    PayflowWorkflowRunModel.status.notin_(list(_TERMINAL)),
                )
                .all()
            )
            for r in other:
                r.status = PayflowWorkflowRunStatus.SUPERSEDED.value
                r.completed_at = _utcnow()
                cancel_pending_jobs_for_run(self.db, r.id)

            entry = strategy.entry_node_id or (
                (strategy.steps or [{}])[0].get("id") if strategy.steps else None
            )
            if not entry:
                skipped += 1
                continue

            run = PayflowWorkflowRunModel(
                account_id=account.id,
                strategy_id=strategy.id,
                strategy_version=int(strategy.version or 1),
                client_id=strategy.client_id,
                portfolio_id=strategy.portfolio_id or account.portfolio_id,
                status=PayflowWorkflowRunStatus.ACTIVE.value,
                current_step_id=entry,
                segment=strategy.segment,
                started_at=_utcnow(),
            )
            self.db.add(run)
            self.db.flush()

            account.current_workflow = strategy.name
            account.last_action = "Workflow enrolled"
            account.next_action = f"Execute step {entry}"

            enqueue_execute_step(
                self.db,
                run_id=run.id,
                step_id=entry,
                account_id=account.id,
                strategy_id=strategy.id,
                commit=False,
            )
            self.log_event(
                event_type="account_enrolled",
                message=f"Enrolled account {account.account_reference} into {strategy.code}",
                account_id=account.id,
                strategy_id=strategy.id,
                run_id=run.id,
                client_id=strategy.client_id,
                portfolio_id=run.portfolio_id,
                job_id=job_id,
                step_id=entry,
            )
            enrolled += 1

        self.db.commit()
        self.log_event(
            event_type="enroll_complete",
            message=f"Enrolled {enrolled}, skipped {skipped} for {strategy.code}",
            strategy_id=strategy_id,
            client_id=strategy.client_id,
            portfolio_id=strategy.portfolio_id,
            job_id=job_id,
            commit=True,
        )
        return {"enrolled": enrolled, "skipped": skipped}

    def gap_fill_active_strategies(self) -> dict[str, Any]:
        """Enroll accounts that match Active strategies but have no open run."""
        strategies = (
            self.db.query(PayflowStrategyModel)
            .filter(PayflowStrategyModel.status == "Active")
            .all()
        )
        totals = {"enrolled": 0, "skipped": 0}
        for s in strategies:
            result = self.enroll_strategy(s.id)
            totals["enrolled"] += int(result.get("enrolled") or 0)
            totals["skipped"] += int(result.get("skipped") or 0)
        return totals

    # ------------------------------------------------------------------ execute / resume
    def resume_run(self, run_id: int, *, from_step_id: str | None = None, job_id: int | None = None) -> None:
        run = self.db.get(PayflowWorkflowRunModel, run_id)
        if not run or run.status in _TERMINAL:
            return
        strategy = self.db.get(PayflowStrategyModel, run.strategy_id)
        if not strategy:
            return
        step_id = from_step_id or run.current_step_id
        steps = {s.get("id"): s for s in (strategy.steps or []) if isinstance(s, dict) and s.get("id")}
        step = steps.get(step_id or "")
        next_id = None
        if step:
            next_id = step.get("next") or _cfg(step).get("next")
        if not next_id:
            # Resume from current step itself if it was Human Review pause before executing next.
            next_id = step_id
        run.status = PayflowWorkflowRunStatus.ACTIVE.value
        run.human_review_id = None
        self.db.flush()
        if next_id:
            enqueue_execute_step(
                self.db,
                run_id=run.id,
                step_id=next_id,
                account_id=run.account_id,
                strategy_id=run.strategy_id,
                commit=False,
            )
        self.log_event(
            event_type="run_resumed",
            message=f"Run {run_id} resumed after human review",
            run_id=run.id,
            account_id=run.account_id,
            strategy_id=run.strategy_id,
            job_id=job_id,
            step_id=next_id,
        )
        self.db.commit()

    def execute_step(self, run_id: int, step_id: str, *, job_id: int | None = None) -> None:
        run = self.db.get(PayflowWorkflowRunModel, run_id)
        if not run or run.status in _TERMINAL:
            return
        if run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value:
            self.log_event(
                event_type="step_skipped",
                message="Run paused for human review",
                run_id=run_id,
                step_id=step_id,
                job_id=job_id,
                level="warn",
            )
            self.db.commit()
            return

        if run.status == PayflowWorkflowRunStatus.WAITING.value:
            run.status = PayflowWorkflowRunStatus.ACTIVE.value

        strategy = self.db.get(PayflowStrategyModel, run.strategy_id)
        account = self.db.get(PayflowAccountModel, run.account_id)
        if not strategy or not account:
            return

        # Idempotency: do not re-execute a step that already completed successfully.
        prior_ok = (
            self.db.query(PayflowWorkflowStepRunModel)
            .filter(
                PayflowWorkflowStepRunModel.run_id == run.id,
                PayflowWorkflowStepRunModel.step_id == step_id,
                PayflowWorkflowStepRunModel.status.in_(list(_STEP_DONE_STATUSES)),
            )
            .order_by(PayflowWorkflowStepRunModel.id.desc())
            .first()
        )
        if prior_ok is not None:
            self.log_event(
                event_type="step_skipped_duplicate",
                message=f"Step {step_id} already {prior_ok.status} — skipping duplicate job",
                run_id=run_id,
                step_id=step_id,
                job_id=job_id,
                account_id=account.id,
                strategy_id=strategy.id,
                level="warn",
            )
            self.db.commit()
            return

        block = _account_execution_block_reason(account)
        if block:
            if account.collection_status == PayflowCollectionStatus.RESOLVED.value or (
                account.collection_status or ""
            ).lower() in {"closed", "ineligible"}:
                run.status = PayflowWorkflowRunStatus.CANCELLED.value
                run.completed_at = _utcnow()
                run.last_error = block
                cancel_pending_jobs_for_run(self.db, run.id)
                self.log_event(
                    event_type="run_cancelled",
                    message=block,
                    level="warn",
                    run_id=run.id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                    job_id=job_id,
                    step_id=step_id,
                )
            else:
                run.status = PayflowWorkflowRunStatus.WAITING.value
                run.last_error = block
                account.next_action = block
                self.log_event(
                    event_type="run_blocked",
                    message=block,
                    level="warn",
                    run_id=run.id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                    job_id=job_id,
                    step_id=step_id,
                )
                attempt = self._next_attempt(run.id, step_id)
                self.db.add(
                    PayflowWorkflowStepRunModel(
                        run_id=run.id,
                        job_id=job_id,
                        step_id=step_id,
                        kind=(
                            next(
                                (
                                    s.get("kind") or ""
                                    for s in (strategy.steps or [])
                                    if isinstance(s, dict) and s.get("id") == step_id
                                ),
                                "",
                            )
                        ),
                        attempt=attempt,
                        status="blocked",
                        started_at=_utcnow(),
                        finished_at=_utcnow(),
                        scheduled_at=_utcnow(),
                        error=block,
                        result_json={"reason": block},
                    )
                )
            self.db.commit()
            return

        steps = {s.get("id"): s for s in (strategy.steps or []) if isinstance(s, dict) and s.get("id")}
        step = steps.get(step_id)
        if not step:
            self._fail_run(run, f"Unknown step {step_id}", job_id=job_id)
            return

        kind = step.get("kind") or ""
        attempt = self._next_attempt(run.id, step_id)
        step_run = PayflowWorkflowStepRunModel(
            run_id=run.id,
            job_id=job_id,
            step_id=step_id,
            kind=kind,
            attempt=attempt,
            status="running",
            started_at=_utcnow(),
            scheduled_at=_utcnow(),
        )
        self.db.add(step_run)
        self.db.flush()

        run.current_step_id = step_id
        account.last_action = f"{kind}: {step.get('title') or step_id}"

        try:
            channel = _cfg(step).get("channel") or step.get("channel")
            blocking = self.rules.first_blocking_action(
                account,
                channel=channel if isinstance(channel, str) else None,
                for_communication=(kind == "Communication"),
            )
            if blocking:
                handled = self._apply_rule_action(
                    run, account, strategy, step, step_run, blocking, job_id=job_id
                )
                if handled:
                    self.db.commit()
                    return

            next_id = self._dispatch_kind(run, account, strategy, step, step_run, steps)
            if run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value:
                self.db.commit()
                return

            if step_run.status == "running":
                step_run.status = "done"
                step_run.finished_at = _utcnow()
            self.log_event(
                event_type="step_done",
                message=f"{kind} completed",
                run_id=run.id,
                account_id=account.id,
                strategy_id=strategy.id,
                client_id=run.client_id,
                portfolio_id=run.portfolio_id,
                job_id=job_id,
                step_id=step_id,
            )

            if next_id:
                account.next_action = f"Next: {next_id}"
                enqueue_execute_step(
                    self.db,
                    run_id=run.id,
                    step_id=next_id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                    commit=False,
                )

            self.db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("execute_step failed run=%s step=%s", run_id, step_id)
            step_run.status = "failed"
            step_run.error = str(exc)[:1000]
            step_run.finished_at = _utcnow()
            run.last_error = str(exc)[:1000]
            self.log_event(
                event_type="step_failed",
                message=str(exc)[:200],
                level="error",
                detail=str(exc)[:2000],
                run_id=run.id,
                account_id=account.id,
                strategy_id=strategy.id,
                job_id=job_id,
                step_id=step_id,
            )
            self._notify_step_failed(run, str(exc)[:200])
            self.db.commit()
            raise

    def _next_attempt(self, run_id: int, step_id: str) -> int:
        last = (
            self.db.query(PayflowWorkflowStepRunModel)
            .filter(
                PayflowWorkflowStepRunModel.run_id == run_id,
                PayflowWorkflowStepRunModel.step_id == step_id,
            )
            .count()
        )
        return last + 1

    def _apply_rule_action(
        self,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        step: dict,
        step_run: PayflowWorkflowStepRunModel,
        matched: dict[str, Any],
        *,
        job_id: int | None,
    ) -> bool:
        """Return True if step chain should stop (handled)."""
        action = matched.get("action")
        self.log_event(
            event_type="rule_matched",
            message=f"Rule '{matched.get('name')}' → {action}",
            run_id=run.id,
            account_id=account.id,
            strategy_id=strategy.id,
            job_id=job_id,
            step_id=step.get("id"),
            level="warn",
        )

        if action == "Prevent Communication" and (step.get("kind") == "Communication"):
            step_run.status = "blocked"
            step_run.error = "Prevent Communication (governance)"
            step_run.result_json = {
                "reason": "Prevent Communication",
                "rule_id": matched["rule_id"],
                "rule_name": matched.get("name"),
            }
            step_run.finished_at = _utcnow()
            cfg = _cfg(step)
            channel = (cfg.get("channel") or step.get("channel") or "Email").strip()
            purpose = cfg.get("purpose") or step.get("purpose") or "Payment Reminder"
            try:
                from app.modules.payflow.services.communication_service import (
                    PayflowCommunicationService,
                )

                PayflowCommunicationService(self.db).record_blocked_communication(
                    account=account,
                    strategy=strategy,
                    channel=channel,
                    purpose=purpose,
                    payment_link=bool(cfg.get("template_id") or cfg.get("payment_link")),
                    restriction_reason=f"Prevent Communication — {matched.get('name') or 'governance rule'}",
                    eligibility_status="Blocked",
                    rule_id=matched.get("rule_id"),
                    rule_name=matched.get("name"),
                    client_id=run.client_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to persist blocked communication")
            next_id = step.get("next") or _cfg(step).get("next")
            self._notify_rule_blocked(run, matched)
            if next_id:
                enqueue_execute_step(
                    self.db,
                    run_id=run.id,
                    step_id=next_id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                    commit=False,
                )
            return True

        if action == "Hold Action":
            run.status = PayflowWorkflowRunStatus.WAITING.value
            step_run.status = "blocked"
            step_run.error = f"Held by governance rule: {matched.get('name') or action}"
            step_run.result_json = {
                "reason": "Hold Action",
                "rule_id": matched.get("rule_id"),
                "rule_name": matched.get("name"),
            }
            step_run.finished_at = _utcnow()
            account.next_action = "Held by governance rule"
            self._notify_rule_blocked(run, matched)
            return True

        if action == "Require Human Review":
            self._pause_for_human_review(
                run,
                account,
                strategy,
                step,
                step_run,
                rule_id=matched["rule_id"],
                reason=matched.get("name") or "Governance rule",
            )
            return True

        if action == "Escalate Case":
            account.human_review = True
            account.collection_status = "Human Review"
            self._pause_for_human_review(
                run,
                account,
                strategy,
                step,
                step_run,
                rule_id=None,
                reason="Escalated by governance rule",
            )
            return True

        if action == "Request Reassessment":
            step_run.status = "reassessment_requested"
            step_run.finished_at = _utcnow()
            account.next_action = "AI reassessment requested"
            # Continue to next after logging — reassessment is async side-effect.
            return False

        if action == "Apply / Change Workflow":
            run.status = PayflowWorkflowRunStatus.CANCELLED.value
            run.completed_at = _utcnow()
            step_run.status = "cancelled"
            step_run.finished_at = _utcnow()
            cancel_pending_jobs_for_run(self.db, run.id)
            return True

        return False

    def _pause_for_human_review(
        self,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        step: dict,
        step_run: PayflowWorkflowStepRunModel,
        *,
        rule_id: int | None,
        reason: str,
    ) -> None:
        from app.modules.payflow.services.review_service import PayflowReviewService

        review_svc = PayflowReviewService(self.db)
        if rule_id:
            review = review_svc.enqueue_from_rule(
                account_id=account.id,
                rule_id=rule_id,
                strategy_id=strategy.id,
                workflow_version=run.strategy_version,
                exception_step_id=step.get("id"),
                actor_name="Workflow worker",
                commit=False,
                notify=True,
            )
        else:
            review = review_svc.create_review(
                account_id=account.id,
                reason=reason,
                proposed_action="Continue workflow after supervisor review",
                strategy_id=strategy.id,
                workflow_version=run.strategy_version,
                exception_step_id=step.get("id"),
                actor_name="Workflow worker",
                commit=False,
                notify=True,
            )
        run.status = PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value
        run.human_review_id = review.id
        account.human_review = True
        account.next_action = "Awaiting human review"
        step_run.status = "paused_human_review"
        step_run.finished_at = _utcnow()
        step_run.result_json = {"human_review_id": review.id}
        self.log_event(
            event_type="human_review_paused",
            message=f"Paused for human review #{review.id}",
            run_id=run.id,
            account_id=account.id,
            strategy_id=strategy.id,
            step_id=step.get("id"),
        )

    def _dispatch_kind(
        self,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        step: dict,
        step_run: PayflowWorkflowStepRunModel,
        steps: dict[str, dict],
        *,
        event_context: dict[str, Any] | None = None,
    ) -> str | None:
        kind = step.get("kind") or ""
        cfg = _cfg(step)
        ctx = event_context or {}

        if kind == "Trigger":
            return step.get("next") or cfg.get("next")

        if kind == "Wait":
            when = self._wait_until(cfg, step, strategy)
            next_id = step.get("next") or cfg.get("next")
            run.status = PayflowWorkflowRunStatus.WAITING.value
            step_run.result_json = {
                "wait_until": when.isoformat(),
                "next": next_id,
                "timezone": self._wait_timezone_name(cfg, step, strategy),
            }
            if next_id:
                enqueue_execute_step(
                    self.db,
                    run_id=run.id,
                    step_id=next_id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                    scheduled_at=when,
                    commit=False,
                )
                account.next_action = f"Scheduled {when.isoformat()} → {next_id}"
            return None

        if kind == "Communication":
            self._send_communication(run, account, strategy, step, step_run)
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            return step.get("next") or cfg.get("next")

        if kind == "Condition":
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            yes_id = step.get("yes") or cfg.get("yes")
            no_id = step.get("no") or cfg.get("no")
            matched = self._eval_condition(account, cfg, step, ctx)
            step_run.result_json = {"condition_matched": matched, "context_keys": list(ctx.keys())}
            return yes_id if matched else no_id

        if kind == "Human Review":
            self._pause_for_human_review(
                run,
                account,
                strategy,
                step,
                step_run,
                rule_id=None,
                reason=step.get("title") or cfg.get("note") or "Workflow human review step",
            )
            return None

        if kind == "Payment Action":
            self._apply_payment_action(account, cfg, step, step_run, ctx)
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            return step.get("next") or cfg.get("next")

        if kind == "Case Action":
            self._apply_case_action(account, cfg, step, step_run)
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            return step.get("next") or cfg.get("next")

        if kind == "AI Reassessment":
            self._queue_ai_reassessment(account, strategy, step_run)
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            return step.get("next") or cfg.get("next")

        if kind in {"Outcome", "End"}:
            run.status = PayflowWorkflowRunStatus.COMPLETED.value
            run.completed_at = _utcnow()
            account.next_action = None
            account.last_action = f"{kind}: {step.get('title') or 'done'}"
            step_run.result_json = {"outcome": step.get("title") or kind}
            return None

        run.status = PayflowWorkflowRunStatus.ACTIVE.value
        return step.get("next") or cfg.get("next")

    def _wait_timezone_name(
        self, cfg: dict, step: dict, strategy: PayflowStrategyModel
    ) -> str:
        tz_name = (
            cfg.get("timezone")
            or step.get("timezone")
            or None
        )
        if not tz_name:
            client = self.db.get(PayflowClientModel, strategy.client_id)
            # Prefer explicit client correspondence language region only as soft default UTC.
            tz_name = getattr(client, "timezone", None) if client else None
        return (str(tz_name).strip() if tz_name else "") or "UTC"

    def _wait_until(
        self, cfg: dict, step: dict, strategy: PayflowStrategyModel
    ) -> datetime:
        amount = cfg.get("amount")
        if amount is None:
            amount = step.get("amount", 3)
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            amount = 3
        amount = max(amount, 0)
        unit = (cfg.get("unit") or step.get("unit") or "Days").lower()
        tz_name = self._wait_timezone_name(cfg, step, strategy)
        try:
            tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001
            tz = timezone.utc
            tz_name = "UTC"
        now_local = datetime.now(tz)
        if unit.startswith("hour"):
            local_when = now_local + timedelta(hours=amount)
        elif unit.startswith("minute"):
            local_when = now_local + timedelta(minutes=amount)
        else:
            local_when = now_local + timedelta(days=amount)
        return local_when.astimezone(timezone.utc)

    def _eval_condition(
        self,
        account: PayflowAccountModel,
        cfg: dict,
        step: dict,
        ctx: dict[str, Any],
    ) -> bool:
        field = (cfg.get("field") or step.get("field") or "").strip()
        operator = (cfg.get("operator") or step.get("operator") or "eq").strip().lower()
        expected = cfg.get("value", step.get("value"))

        if not field:
            # Default: payment / arrangement progress.
            return float(account.outstanding_balance or 0) <= 0 or (
                account.collection_status or ""
            ).lower() in {
                "resolved",
                "promise to pay",
                "payment plan",
            }

        # Event context wins for payment/communication outcomes.
        if field in ctx:
            actual: Any = ctx.get(field)
        else:
            actual = getattr(account, field, None)
            if actual is None and field in {
                "paid",
                "has_payment",
                "payment_received",
            }:
                actual = bool(account.last_payment_date) or float(
                    account.outstanding_balance or 0
                ) < float(account.original_balance or 0)
            if field == "outstanding_balance":
                actual = float(account.outstanding_balance or 0)
            if field == "collection_status":
                actual = account.collection_status
            if field == "provincial_hold":
                actual = bool(account.provincial_hold)
            if field in {"comm_status", "communication_status"}:
                actual = ctx.get("comm_status") or ctx.get("status")

        if operator in {"eq", "=", "==", "equals"}:
            return str(actual).strip().lower() == str(expected).strip().lower()
        if operator in {"neq", "!=", "not_equals"}:
            return str(actual).strip().lower() != str(expected).strip().lower()
        if operator in {"gt", ">"}:
            try:
                return float(actual or 0) > float(expected or 0)
            except (TypeError, ValueError):
                return False
        if operator in {"gte", ">="}:
            try:
                return float(actual or 0) >= float(expected or 0)
            except (TypeError, ValueError):
                return False
        if operator in {"lt", "<"}:
            try:
                return float(actual or 0) < float(expected or 0)
            except (TypeError, ValueError):
                return False
        if operator in {"lte", "<="}:
            try:
                return float(actual or 0) <= float(expected or 0)
            except (TypeError, ValueError):
                return False
        if operator in {"truthy", "true", "is_true"}:
            return bool(actual)
        if operator in {"falsy", "false", "is_false"}:
            return not bool(actual)
        if operator in {"in", "one_of"}:
            options = expected if isinstance(expected, (list, tuple, set)) else str(expected).split(",")
            return str(actual).strip().lower() in {str(o).strip().lower() for o in options}
        return bool(actual)

    def _append_timeline(self, account: PayflowAccountModel, label: str, detail: str = "") -> None:
        timeline = list(account.timeline or [])
        timeline.append(
            {
                "at": _utcnow().isoformat(),
                "label": label,
                "detail": detail,
                "source": "workflow",
            }
        )
        account.timeline = timeline[-40:]

    def _apply_payment_action(
        self,
        account: PayflowAccountModel,
        cfg: dict,
        step: dict,
        step_run: PayflowWorkflowStepRunModel,
        ctx: dict[str, Any],
    ) -> None:
        action = cfg.get("action") or step.get("action") or "Record payment action"
        amount = cfg.get("amount", ctx.get("amount"))
        if amount is not None:
            try:
                account.last_payment_amount = float(amount)
            except (TypeError, ValueError):
                pass
        pay_date = cfg.get("payment_date") or ctx.get("payment_date")
        if pay_date:
            if isinstance(pay_date, date) and not isinstance(pay_date, datetime):
                account.last_payment_date = pay_date
            else:
                try:
                    account.last_payment_date = date.fromisoformat(str(pay_date)[:10])
                except ValueError:
                    account.last_payment_date = _utcnow().date()
        elif amount is not None:
            account.last_payment_date = _utcnow().date()
        if "is_ptp" in cfg or "is_ptp" in ctx:
            account.last_payment_is_ptp = bool(cfg.get("is_ptp", ctx.get("is_ptp")))
        status_hint = cfg.get("collection_status") or step.get("collection_status")
        if status_hint:
            account.collection_status = str(status_hint)
        elif account.last_payment_is_ptp:
            account.collection_status = PayflowCollectionStatus.PROMISE_TO_PAY.value
        detail = f"{action}"
        if account.last_payment_amount is not None:
            detail = f"{action} · {account.last_payment_amount}"
        self._append_timeline(account, action, detail)
        account.last_action = action
        step_run.result_json = {
            "action": action,
            "amount": account.last_payment_amount,
            "payment_date": account.last_payment_date.isoformat()
            if account.last_payment_date
            else None,
            "is_ptp": account.last_payment_is_ptp,
        }

    def _apply_case_action(
        self,
        account: PayflowAccountModel,
        cfg: dict,
        step: dict,
        step_run: PayflowWorkflowStepRunModel,
    ) -> None:
        action = cfg.get("action") or step.get("action") or "Case action"
        if cfg.get("collection_status") or step.get("collection_status"):
            account.collection_status = str(
                cfg.get("collection_status") or step.get("collection_status")
            )
        if "provincial_hold" in cfg:
            account.provincial_hold = bool(cfg.get("provincial_hold"))
        if "hold_days" in cfg:
            try:
                account.hold_days = int(cfg.get("hold_days"))
            except (TypeError, ValueError):
                pass
        if "human_review" in cfg:
            account.human_review = bool(cfg.get("human_review"))
        self._append_timeline(account, action, f"status={account.collection_status}")
        account.last_action = action
        step_run.result_json = {
            "action": action,
            "collection_status": account.collection_status,
            "provincial_hold": account.provincial_hold,
            "hold_days": account.hold_days,
        }

    def _queue_ai_reassessment(
        self,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        step_run: PayflowWorkflowStepRunModel,
    ) -> None:
        account.next_action = "AI reassessment queued"
        job_id = None
        portfolio_id = strategy.portfolio_id or account.portfolio_id
        if portfolio_id:
            try:
                from app.modules.payflow.services.ai_suggest_job_service import (
                    AiSuggestJobService,
                )

                job = AiSuggestJobService(self.db).enqueue(
                    strategy.client_id,
                    portfolio_id,
                    trigger="workflow_ai_reassessment",
                    requested_by_user_id=None,
                )
                if job:
                    job_id = job.id
            except Exception as exc:  # noqa: BLE001
                logger.exception("AI reassessment enqueue failed: %s", exc)
        step_run.result_json = {
            "queued": True,
            "ai_suggest_job_id": job_id,
        }
        self._append_timeline(
            account,
            "AI reassessment",
            f"job_id={job_id}" if job_id else "intent logged",
        )

    def _send_communication(
        self,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        step: dict,
        step_run: PayflowWorkflowStepRunModel,
    ) -> None:
        cfg = _cfg(step)
        channel = (cfg.get("channel") or step.get("channel") or "Email").strip()
        purpose = cfg.get("purpose") or step.get("purpose") or "Payment Reminder"
        template_id = cfg.get("template_id") or step.get("template_id")
        template = next((t for t in MESSAGE_TEMPLATES if t.get("id") == template_id), None)
        subject = cfg.get("subject") or (template or {}).get("subject") or f"{purpose}"
        body = cfg.get("body") or (template or {}).get("body") or ""
        subject = self._render(subject, account, strategy)
        body = self._render(body, account, strategy)

        result = PayflowCommunicationService(self.db).send_workflow_step(
            account=account,
            strategy=strategy,
            channel=channel,
            purpose=purpose,
            subject=subject,
            body=body,
            payment_link=self._payment_link(account),
            run_id=run.id,
            client_id=run.client_id,
        )
        step_run.result_json = result
        if result.get("skipped"):
            event = "sms_skipped" if channel == "SMS" else "email_skipped"
            self.log_event(
                event_type=event,
                message=f"Skipped communication ({result['skipped']})",
                level="warn",
                run_id=run.id,
                account_id=account.id,
                strategy_id=strategy.id,
            )
            return
        if result.get("provider_error"):
            self._notify_provider_failed(
                run, channel, str(result.get("provider_error"))
            )
        elif result.get("status") in {"sent", "logged"}:
            self._notify_comm_sent(
                run,
                account,
                channel=channel,
                recipient=str(result.get("recipient") or ""),
                comm_code=str(result.get("code") or ""),
            )

    def _render(self, text: str, account: PayflowAccountModel, strategy: PayflowStrategyModel) -> str:
        if not text:
            return ""
        first = account.customer_first_name or (account.customer_name or "").split(" ")[0]
        mapping = {
            "first_name": first or "Customer",
            "outstanding_balance": f"${float(account.outstanding_balance or 0):,.2f}",
            "account_reference": account.account_reference or "",
            "payment_link": self._payment_link(account),
            "brand_name": settings.SENDGRID_FROM_NAME or "PayFlow",
            "promise_amount": f"${float(account.ptp_amount or 0):,.2f}",
            "promise_date": str(account.ptp_due_date or ""),
        }

        def repl(match: re.Match) -> str:
            key = match.group(1).strip()
            return str(mapping.get(key, match.group(0)))

        return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", repl, text)

    def _payment_link(self, account: PayflowAccountModel) -> str:
        base = (settings.WORKFLOW_PAYMENT_LINK_BASE or "").rstrip("/")
        return f"{base}/{account.account_reference}"

    def _fail_run(self, run: PayflowWorkflowRunModel, error: str, *, job_id: int | None) -> None:
        run.status = PayflowWorkflowRunStatus.FAILED.value
        run.last_error = error[:1000]
        run.completed_at = _utcnow()
        self.log_event(
            event_type="run_failed",
            message=error[:200],
            level="error",
            run_id=run.id,
            account_id=run.account_id,
            strategy_id=run.strategy_id,
            job_id=job_id,
        )
        self.db.commit()

    def _notify_rule_blocked(self, run: PayflowWorkflowRunModel, matched: dict) -> None:
        try:
            from app.modules.payflow.services.notification_service import PayflowNotificationService

            svc = PayflowNotificationService(self.db)
            svc.notify(
                users=svc.recipients_for_client(run.client_id),
                notification_type=PayflowNotificationType.WORKFLOW_RULE_BLOCKED.value,
                title="Workflow blocked by rule",
                body=f"{matched.get('name')}: {matched.get('action')}",
                link=f"/payflow/cases/{run.account_id}",
                client_id=run.client_id,
                entity_type="workflow_run",
                entity_id=str(run.id),
                send_mail=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("rule block notify failed")

    def _notify_step_failed(self, run: PayflowWorkflowRunModel, message: str) -> None:
        try:
            from app.modules.payflow.services.notification_service import PayflowNotificationService

            svc = PayflowNotificationService(self.db)
            svc.notify(
                users=svc.recipients_for_client(run.client_id),
                notification_type=PayflowNotificationType.WORKFLOW_STEP_FAILED.value,
                title="Workflow step failed",
                body=message,
                link=f"/payflow/cases/{run.account_id}",
                client_id=run.client_id,
                entity_type="workflow_run",
                entity_id=str(run.id),
                send_mail=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("step failed notify failed")

    def _notify_provider_failed(self, run: PayflowWorkflowRunModel, channel: str, detail: str) -> None:
        try:
            from app.modules.payflow.services.notification_service import PayflowNotificationService

            svc = PayflowNotificationService(self.db)
            svc.notify(
                users=svc.recipients_for_client(run.client_id),
                notification_type=PayflowNotificationType.PROVIDER_SEND_FAILED.value,
                title=f"{channel} send failed",
                body=detail[:200],
                link=f"/payflow/cases/{run.account_id}",
                client_id=run.client_id,
                entity_type="workflow_run",
                entity_id=str(run.id),
                send_mail=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("provider failed notify failed")

    def _notify_comm_sent(
        self,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        *,
        channel: str,
        recipient: str,
        comm_code: str,
    ) -> None:
        """In-app only: supervisors see that collection email/SMS went to the account customer."""
        try:
            from app.modules.payflow.services.notification_service import PayflowNotificationService

            svc = PayflowNotificationService(self.db)
            svc.notify(
                users=svc.recipients_for_client(run.client_id),
                notification_type=PayflowNotificationType.COMM_SENT.value,
                title=f"{channel} sent · {account.customer_name}",
                body=f"To {recipient} · {account.account_reference} · {comm_code}",
                link=f"/payflow/cases/{account.id}",
                client_id=run.client_id,
                entity_type="communication",
                entity_id=comm_code,
                send_mail=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("comm sent notify failed run=%s", run.id)

    # ------------------------------------------------------------------ list APIs
    @staticmethod
    def _worker_log_group_key(item: dict[str, Any]) -> str:
        if item.get("run_id") is not None:
            return f"run:{item['run_id']}"
        if item.get("account_id") is not None and item.get("job_id") is not None:
            return f"acct:{item['account_id']}:job:{item['job_id']}"
        if item.get("job_id") is not None:
            return f"job:{item['job_id']}"
        return f"solo:{item['id']}"

    @staticmethod
    def _headline_score(item: dict[str, Any]) -> tuple[int, int, int]:
        return (
            _LEVEL_RANK.get(str(item.get("level") or "info"), 0),
            _EVENT_RANK.get(str(item.get("event_type") or ""), 0),
            int(item.get("id") or 0),
        )

    def _group_worker_logs(
        self, items: list[dict[str, Any]], *, limit: int
    ) -> list[dict[str, Any]]:
        """One list row per account run — enroll bookends without account stay off the list."""
        # Drop strategy-level enroll_queued / enroll_complete (Account shows "—").
        list_items = [
            item
            for item in items
            if not (
                item.get("account_id") is None
                and (item.get("event_type") or "") in _LIST_HIDE_NO_ACCOUNT_EVENTS
            )
        ]
        buckets: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for item in list_items:
            key = self._worker_log_group_key(item)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(item)

        grouped: list[dict[str, Any]] = []
        for key in order:
            events = buckets[key]
            headline = max(events, key=self._headline_score)
            latest = max(events, key=lambda e: int(e.get("id") or 0))
            # Prefer context fields from any event that has them filled.
            for src in (headline, latest, *events):
                for field in (
                    "client_id",
                    "client_name",
                    "client_code",
                    "portfolio_id",
                    "portfolio_name",
                    "portfolio_code",
                    "account_id",
                    "account_reference",
                    "customer_name",
                    "strategy_id",
                    "strategy_name",
                    "strategy_code",
                    "run_id",
                    "job_id",
                ):
                    if headline.get(field) in (None, "") and src.get(field) not in (None, ""):
                        headline[field] = src[field]
            count = len(events)
            types = []
            for e in sorted(events, key=lambda x: int(x.get("id") or 0)):
                t = e.get("event_type")
                if t and t not in types:
                    types.append(t)
            row = dict(headline)
            row["event_count"] = count
            row["group_key"] = key
            row["created_at"] = latest.get("created_at") or headline.get("created_at")
            if count > 1:
                row["message"] = f"{headline.get('message') or ''} · {count} steps".strip(" ·")
            row["detail"] = None
            row["step_summary"] = " → ".join(types[:8])
            grouped.append(row)
            if len(grouped) >= limit:
                break
        return grouped

    def _enrich_worker_logs(
        self, rows: list[PayflowWorkflowWorkerLogModel]
    ) -> list[dict[str, Any]]:
        if not rows:
            return []

        # Backfill missing client/portfolio/account from the run (older noisy rows).
        run_ids = {
            r.run_id
            for r in rows
            if r.run_id
            and (r.client_id is None or r.portfolio_id is None or r.account_id is None)
        }
        runs: dict[int, PayflowWorkflowRunModel] = {}
        if run_ids:
            runs = {
                run.id: run
                for run in self.db.query(PayflowWorkflowRunModel)
                .filter(PayflowWorkflowRunModel.id.in_(run_ids))
                .all()
            }
        account_ids_for_fill = {
            r.account_id
            for r in rows
            if r.account_id and (r.client_id is None or r.portfolio_id is None)
        }
        accounts_for_fill: dict[int, PayflowAccountModel] = {}
        if account_ids_for_fill:
            accounts_for_fill = {
                a.id: a
                for a in self.db.query(PayflowAccountModel)
                .filter(PayflowAccountModel.id.in_(account_ids_for_fill))
                .all()
            }

        resolved: list[tuple[PayflowWorkflowWorkerLogModel, int | None, int | None, int | None, int | None]] = []
        client_ids: set[int] = set()
        portfolio_ids: set[int] = set()
        account_ids: set[int] = set()
        strategy_ids: set[int] = set()
        for r in rows:
            cid, pid, aid, sid = r.client_id, r.portfolio_id, r.account_id, r.strategy_id
            run = runs.get(r.run_id) if r.run_id else None
            if run:
                cid = cid if cid is not None else run.client_id
                pid = pid if pid is not None else run.portfolio_id
                aid = aid if aid is not None else run.account_id
                sid = sid if sid is not None else run.strategy_id
            if aid is not None and (cid is None or pid is None):
                acct = accounts_for_fill.get(aid)
                if acct:
                    cid = cid if cid is not None else acct.client_id
                    pid = pid if pid is not None else acct.portfolio_id
            if cid is not None:
                client_ids.add(cid)
            if pid is not None:
                portfolio_ids.add(pid)
            if aid is not None:
                account_ids.add(aid)
            if sid is not None:
                strategy_ids.add(sid)
            resolved.append((r, cid, pid, aid, sid))

        clients = {
            c.id: c
            for c in (
                self.db.query(PayflowClientModel)
                .filter(PayflowClientModel.id.in_(client_ids))
                .all()
                if client_ids
                else []
            )
        }
        portfolios = {
            p.id: p
            for p in (
                self.db.query(PayflowPortfolioModel)
                .filter(PayflowPortfolioModel.id.in_(portfolio_ids))
                .all()
                if portfolio_ids
                else []
            )
        }
        accounts = {
            a.id: a
            for a in (
                self.db.query(PayflowAccountModel)
                .filter(PayflowAccountModel.id.in_(account_ids))
                .all()
                if account_ids
                else []
            )
        }
        strategies = {
            s.id: s
            for s in (
                self.db.query(PayflowStrategyModel)
                .filter(PayflowStrategyModel.id.in_(strategy_ids))
                .all()
                if strategy_ids
                else []
            )
        }

        out: list[dict[str, Any]] = []
        for r, cid, pid, aid, sid in resolved:
            client = clients.get(cid) if cid else None
            portfolio = portfolios.get(pid) if pid else None
            account = accounts.get(aid) if aid else None
            strategy = strategies.get(sid) if sid else None
            out.append(
                {
                    "id": r.id,
                    "event_type": r.event_type,
                    "level": r.level,
                    "message": r.message,
                    "detail": r.detail,
                    "client_id": cid,
                    "client_name": client.name if client else None,
                    "client_code": client.code if client else None,
                    "portfolio_id": pid,
                    "portfolio_name": portfolio.name if portfolio else None,
                    "portfolio_code": portfolio.code if portfolio else None,
                    "account_id": aid,
                    "account_reference": account.account_reference if account else None,
                    "customer_name": account.customer_name if account else None,
                    "strategy_id": sid,
                    "strategy_name": strategy.name if strategy else None,
                    "strategy_code": strategy.code if strategy else None,
                    "run_id": r.run_id,
                    "job_id": r.job_id,
                    "step_id": r.step_id,
                    "created_at": r.created_at,
                    "event_count": 1,
                    "group_key": None,
                    "step_summary": None,
                }
            )
        return out

    def list_worker_logs(
        self,
        *,
        client_id: int | None = None,
        portfolio_id: int | None = None,
        account_id: int | None = None,
        strategy_id: int | None = None,
        event_type: str | None = None,
        level: str | None = None,
        search: str | None = None,
        limit: int = 200,
        group: bool = True,
    ) -> list[dict[str, Any]]:
        q = self.db.query(PayflowWorkflowWorkerLogModel)
        if client_id is not None:
            q = q.filter(PayflowWorkflowWorkerLogModel.client_id == client_id)
        if portfolio_id is not None:
            q = q.filter(PayflowWorkflowWorkerLogModel.portfolio_id == portfolio_id)
        if account_id is not None:
            q = q.filter(PayflowWorkflowWorkerLogModel.account_id == account_id)
        if strategy_id is not None:
            q = q.filter(PayflowWorkflowWorkerLogModel.strategy_id == strategy_id)
        if event_type:
            q = q.filter(PayflowWorkflowWorkerLogModel.event_type == event_type)
        if level:
            q = q.filter(PayflowWorkflowWorkerLogModel.level == level)
        if search:
            like = f"%{search}%"
            q = (
                q.outerjoin(
                    PayflowClientModel,
                    PayflowWorkflowWorkerLogModel.client_id == PayflowClientModel.id,
                )
                .outerjoin(
                    PayflowPortfolioModel,
                    PayflowWorkflowWorkerLogModel.portfolio_id == PayflowPortfolioModel.id,
                )
                .outerjoin(
                    PayflowAccountModel,
                    PayflowWorkflowWorkerLogModel.account_id == PayflowAccountModel.id,
                )
                .outerjoin(
                    PayflowStrategyModel,
                    PayflowWorkflowWorkerLogModel.strategy_id == PayflowStrategyModel.id,
                )
                .filter(
                    or_(
                        PayflowWorkflowWorkerLogModel.message.ilike(like),
                        PayflowWorkflowWorkerLogModel.event_type.ilike(like),
                        PayflowClientModel.name.ilike(like),
                        PayflowClientModel.code.ilike(like),
                        PayflowPortfolioModel.name.ilike(like),
                        PayflowPortfolioModel.code.ilike(like),
                        PayflowAccountModel.account_reference.ilike(like),
                        PayflowAccountModel.customer_name.ilike(like),
                        PayflowStrategyModel.name.ilike(like),
                        PayflowStrategyModel.code.ilike(like),
                    )
                )
                .distinct()
            )
        fetch_limit = min(max(limit * 10, limit), 2000) if group else min(limit, 500)
        rows = (
            q.order_by(PayflowWorkflowWorkerLogModel.id.desc())
            .limit(fetch_limit)
            .all()
        )
        enriched = self._enrich_worker_logs(rows)
        if not group:
            return enriched[: min(limit, 500)]
        return self._group_worker_logs(enriched, limit=min(limit, 500))

    def get_worker_log(self, log_id: int) -> dict[str, Any] | None:
        row = self.db.get(PayflowWorkflowWorkerLogModel, log_id)
        if not row:
            return None
        item = self._enrich_worker_logs([row])[0]

        timeline_q = self.db.query(PayflowWorkflowWorkerLogModel)
        if row.run_id is not None:
            timeline_q = timeline_q.filter(PayflowWorkflowWorkerLogModel.run_id == row.run_id)
        elif row.account_id is not None and row.job_id is not None:
            timeline_q = timeline_q.filter(
                PayflowWorkflowWorkerLogModel.account_id == row.account_id,
                PayflowWorkflowWorkerLogModel.job_id == row.job_id,
            )
        elif row.account_id is not None:
            timeline_q = timeline_q.filter(
                PayflowWorkflowWorkerLogModel.account_id == row.account_id
            )
        elif row.job_id is not None:
            timeline_q = timeline_q.filter(PayflowWorkflowWorkerLogModel.job_id == row.job_id)
        elif row.strategy_id is not None:
            timeline_q = timeline_q.filter(
                PayflowWorkflowWorkerLogModel.strategy_id == row.strategy_id
            )
        else:
            timeline_q = timeline_q.filter(PayflowWorkflowWorkerLogModel.id == log_id)

        timeline_rows = (
            timeline_q.order_by(PayflowWorkflowWorkerLogModel.id.asc()).limit(80).all()
        )
        timeline = self._enrich_worker_logs(timeline_rows)
        # Prefer the richest context row for the header.
        for ev in reversed(timeline):
            for field in (
                "client_id",
                "client_name",
                "client_code",
                "portfolio_id",
                "portfolio_name",
                "portfolio_code",
                "account_id",
                "account_reference",
                "customer_name",
                "strategy_id",
                "strategy_name",
                "strategy_code",
                "run_id",
                "job_id",
            ):
                if item.get(field) in (None, "") and ev.get(field) not in (None, ""):
                    item[field] = ev[field]
        item["event_count"] = len(timeline)
        item["group_key"] = self._worker_log_group_key(item)
        item["related_events"] = timeline
        return item

    def list_runs(
        self,
        *,
        client_id: int | None = None,
        strategy_id: int | None = None,
        account_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        q = self.db.query(PayflowWorkflowRunModel)
        if client_id is not None:
            q = q.filter(PayflowWorkflowRunModel.client_id == client_id)
        if strategy_id is not None:
            q = q.filter(PayflowWorkflowRunModel.strategy_id == strategy_id)
        if account_id is not None:
            q = q.filter(PayflowWorkflowRunModel.account_id == account_id)
        if status:
            q = q.filter(PayflowWorkflowRunModel.status == status)
        rows = q.order_by(PayflowWorkflowRunModel.id.desc()).limit(min(limit, 500)).all()
        out: list[dict[str, Any]] = []
        for r in rows:
            strategy = self.db.get(PayflowStrategyModel, r.strategy_id)
            out.append(
                {
                    "id": r.id,
                    "account_id": r.account_id,
                    "strategy_id": r.strategy_id,
                    "strategy_version": r.strategy_version,
                    "strategy_name": strategy.name if strategy else None,
                    "strategy_code": strategy.code if strategy else None,
                    "workflow_definition_path": f"/payflow/workflows/{r.strategy_id}",
                    "client_id": r.client_id,
                    "portfolio_id": r.portfolio_id,
                    "status": r.status,
                    "current_step_id": r.current_step_id,
                    "human_review_id": r.human_review_id,
                    "started_at": r.started_at,
                    "completed_at": r.completed_at,
                    "last_error": r.last_error,
                }
            )
        return out

    def _serialize_step_run(
        self, s: PayflowWorkflowStepRunModel, *, steps_by_id: dict[str, dict] | None = None
    ) -> dict[str, Any]:
        step_def = (steps_by_id or {}).get(s.step_id) or {}
        ui_status = _UI_STEP_STATUS.get((s.status or "").lower(), s.status)
        block_reason = None
        if ui_status == "Blocked":
            block_reason = s.error or (s.result_json or {}).get("reason")
            if isinstance(s.result_json, dict) and s.result_json.get("rule_name"):
                block_reason = (
                    f"{block_reason} — {s.result_json.get('rule_name')}"
                    if block_reason
                    else str(s.result_json.get("rule_name"))
                )
        return {
            "id": s.id,
            "step_id": s.step_id,
            "kind": s.kind,
            "title": step_def.get("title") or s.kind,
            "attempt": s.attempt,
            "status": s.status,
            "ui_status": ui_status,
            "scheduled_at": s.scheduled_at,
            "started_at": s.started_at,
            "finished_at": s.finished_at,
            "error": s.error,
            "block_reason": block_reason,
            "result_json": s.result_json,
            "communication_id": (s.result_json or {}).get("communication_id")
            if isinstance(s.result_json, dict)
            else None,
        }

    def _enrich_run(self, run: PayflowWorkflowRunModel) -> dict[str, Any]:
        strategy = self.db.get(PayflowStrategyModel, run.strategy_id)
        steps_by_id = {
            s.get("id"): s
            for s in ((strategy.steps if strategy else None) or [])
            if isinstance(s, dict) and s.get("id")
        }
        step_rows = (
            self.db.query(PayflowWorkflowStepRunModel)
            .filter(PayflowWorkflowStepRunModel.run_id == run.id)
            .order_by(PayflowWorkflowStepRunModel.id.asc())
            .all()
        )
        pending_job = (
            self.db.query(PayflowWorkflowJobModel)
            .filter(
                PayflowWorkflowJobModel.run_id == run.id,
                PayflowWorkflowJobModel.job_type == PayflowWorkflowJobType.EXECUTE_STEP.value,
                PayflowWorkflowJobModel.status == PayflowWorkflowJobStatus.PENDING.value,
            )
            .order_by(PayflowWorkflowJobModel.scheduled_at.asc())
            .first()
        )
        current_def = steps_by_id.get(run.current_step_id or "") or {}
        next_step_id = pending_job.step_id if pending_job else None
        next_def = steps_by_id.get(next_step_id or "") or {}
        activity: list[dict[str, Any]] = []
        for s in step_rows:
            item = self._serialize_step_run(s, steps_by_id=steps_by_id)
            result = s.result_json if isinstance(s.result_json, dict) else {}
            activity.append(
                {
                    "at": (s.finished_at or s.started_at or s.scheduled_at or s.created_at),
                    "label": item.get("title") or s.kind,
                    "kind": s.kind,
                    "detail": item.get("block_reason") or s.error or "",
                    "ui_status": item["ui_status"],
                    "step_id": s.step_id,
                    "step_run_id": s.id,
                    "communication_id": item.get("communication_id"),
                    "human_review_id": result.get("human_review_id"),
                    "block_reason": item.get("block_reason"),
                    "error": s.error,
                }
            )
        if pending_job:
            activity.append(
                {
                    "at": pending_job.scheduled_at,
                    "label": next_def.get("title") or next_step_id or "Next step",
                    "kind": next_def.get("kind") or "Scheduled",
                    "detail": f"Scheduled for {pending_job.scheduled_at.isoformat() if pending_job.scheduled_at else '—'}",
                    "ui_status": "Scheduled",
                    "step_id": next_step_id,
                    "step_run_id": None,
                    "communication_id": None,
                    "block_reason": None,
                    "error": None,
                }
            )
        activity.sort(key=lambda a: a["at"] or datetime.min.replace(tzinfo=timezone.utc))
        block_reason = run.last_error
        if not block_reason:
            for s in reversed(step_rows):
                if (s.status or "").lower() in {"blocked", "held", "failed"}:
                    block_reason = s.error or (s.result_json or {}).get("reason")
                    break
        return {
            "id": run.id,
            "account_id": run.account_id,
            "strategy_id": run.strategy_id,
            "strategy_version": run.strategy_version,
            "strategy_name": strategy.name if strategy else None,
            "strategy_code": strategy.code if strategy else None,
            "workflow_definition_path": f"/payflow/workflows/{run.strategy_id}",
            "client_id": run.client_id,
            "portfolio_id": run.portfolio_id,
            "status": run.status,
            "current_step_id": run.current_step_id,
            "current_step": {
                "id": run.current_step_id,
                "title": current_def.get("title"),
                "kind": current_def.get("kind"),
            }
            if run.current_step_id
            else None,
            "next_step_id": next_step_id,
            "next_step_title": next_def.get("title"),
            "next_scheduled_at": pending_job.scheduled_at if pending_job else None,
            "human_review_id": run.human_review_id,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "last_error": run.last_error,
            "block_reason": block_reason,
            "deactivate_policy": DEACTIVATE_POLICY,
            "step_runs": [
                self._serialize_step_run(s, steps_by_id=steps_by_id) for s in step_rows
            ],
            "activity": activity,
        }

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        run = self.db.get(PayflowWorkflowRunModel, run_id)
        if not run:
            return None
        return self._enrich_run(run)

    def get_account_workflow_execution(self, account_id: int) -> dict[str, Any] | None:
        run = (
            self.db.query(PayflowWorkflowRunModel)
            .filter(
                PayflowWorkflowRunModel.account_id == account_id,
                PayflowWorkflowRunModel.status.notin_(list(_TERMINAL)),
            )
            .order_by(PayflowWorkflowRunModel.id.desc())
            .first()
        )
        if not run:
            run = (
                self.db.query(PayflowWorkflowRunModel)
                .filter(PayflowWorkflowRunModel.account_id == account_id)
                .order_by(PayflowWorkflowRunModel.id.desc())
                .first()
            )
        if not run:
            return None
        return self._enrich_run(run)

    def _open_runs_for_account(self, account_id: int) -> list[PayflowWorkflowRunModel]:
        return (
            self.db.query(PayflowWorkflowRunModel)
            .filter(
                PayflowWorkflowRunModel.account_id == account_id,
                PayflowWorkflowRunModel.status.notin_(list(_TERMINAL)),
            )
            .all()
        )

    def on_payment_event(
        self,
        account_id: int,
        payload: dict[str, Any] | None = None,
        *,
        commit: bool = True,
    ) -> dict[str, Any]:
        account = self.db.get(PayflowAccountModel, account_id)
        if not account:
            return {"advanced": 0}
        payload = payload or {}
        if payload.get("amount") is not None:
            try:
                account.last_payment_amount = float(payload["amount"])
            except (TypeError, ValueError):
                pass
        if payload.get("payment_date"):
            try:
                account.last_payment_date = date.fromisoformat(str(payload["payment_date"])[:10])
            except ValueError:
                account.last_payment_date = _utcnow().date()
        elif payload.get("amount") is not None:
            account.last_payment_date = _utcnow().date()
        if "is_ptp" in payload:
            account.last_payment_is_ptp = bool(payload.get("is_ptp"))
        ctx = {
            "has_payment": True,
            "payment_received": True,
            "amount": account.last_payment_amount,
            "payment_date": account.last_payment_date.isoformat()
            if account.last_payment_date
            else None,
            **payload,
        }
        advanced = 0
        for run in self._open_runs_for_account(account_id):
            if self._advance_run_from_event(run, event="payment", context=ctx):
                advanced += 1
            self.log_event(
                event_type="payment_event",
                message="Payment event received — workflow reassessed",
                run_id=run.id,
                account_id=account_id,
                strategy_id=run.strategy_id,
            )
        try:
            from app.modules.payflow.services.communication_service import (
                PayflowCommunicationService,
            )

            # Payment events are verified by PayFlow — never from email click alone.
            PayflowCommunicationService(self.db).mark_payment_event(
                account_id,
                event="completed" if payload.get("completed") else "initiated",
                commit=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to attribute payment to communication")
        if commit:
            self.db.commit()
        return {"advanced": advanced}

    def on_communication_event(
        self,
        communication_id: int,
        status: str,
        *,
        commit: bool = True,
    ) -> dict[str, Any]:
        comm = self.db.get(PayflowCommunicationModel, communication_id)
        if not comm or not comm.account_id:
            return {"advanced": 0}
        ctx = {
            "comm_status": status,
            "status": status,
            "communication_id": communication_id,
            "channel": comm.channel,
        }
        advanced = 0
        for run in self._open_runs_for_account(comm.account_id):
            if self._advance_run_from_event(run, event="communication", context=ctx):
                advanced += 1
            self.log_event(
                event_type="communication_event",
                message=f"Communication #{communication_id} → {status}",
                run_id=run.id,
                account_id=comm.account_id,
                strategy_id=run.strategy_id,
            )
        if commit:
            self.db.commit()
        return {"advanced": advanced}

    def on_case_status_changed(
        self,
        account_id: int,
        *,
        previous: str | None,
        new: str | None,
        provincial_hold: bool | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        account = self.db.get(PayflowAccountModel, account_id)
        if not account:
            return {"affected": 0}
        prior_hold = bool(account.provincial_hold)
        if provincial_hold is not None:
            account.provincial_hold = provincial_hold
        hold_released = prior_hold and not bool(account.provincial_hold)
        if (previous or "").lower() in {"on hold", "hold"} and (new or "").lower() not in {
            "on hold",
            "hold",
        }:
            hold_released = True
        previous_hold_was_true = hold_released
        affected = 0
        for run in self._open_runs_for_account(account_id):
            block = _account_execution_block_reason(account)
            if block and (
                (new or "") == PayflowCollectionStatus.RESOLVED.value
                or (new or "").lower() in {"closed", "ineligible"}
            ):
                run.status = PayflowWorkflowRunStatus.CANCELLED.value
                run.completed_at = _utcnow()
                run.last_error = block
                cancel_pending_jobs_for_run(self.db, run.id)
                self.log_event(
                    event_type="run_cancelled",
                    message=block,
                    level="warn",
                    run_id=run.id,
                    account_id=account_id,
                    strategy_id=run.strategy_id,
                )
                affected += 1
            elif block:
                run.status = PayflowWorkflowRunStatus.WAITING.value
                run.last_error = block
                account.next_action = block
                cancel_pending_jobs_for_run(self.db, run.id)
                self.log_event(
                    event_type="run_blocked",
                    message=block,
                    level="warn",
                    run_id=run.id,
                    account_id=account_id,
                    strategy_id=run.strategy_id,
                )
                affected += 1
            elif not block and (
                (previous or "").lower() in {"on hold", "hold", "resolved", "closed", "ineligible"}
                or previous_hold_was_true
            ):
                # Hold / block released — resume
                enqueue_resume_run(
                    self.db,
                    run_id=run.id,
                    step_id=run.current_step_id,
                    account_id=account_id,
                    commit=False,
                )
                self.log_event(
                    event_type="run_resumed",
                    message=f"Case status {previous} → {new}; resume queued",
                    run_id=run.id,
                    account_id=account_id,
                    strategy_id=run.strategy_id,
                )
                affected += 1
            else:
                self._advance_run_from_event(
                    run,
                    event="case_status",
                    context={"collection_status": new, "previous_status": previous},
                )
        if commit:
            self.db.commit()
        return {"affected": affected}

    def on_account_import_changed(
        self,
        account: PayflowAccountModel,
        *,
        previous_status: str | None,
        previous_payment_date: date | None,
        previous_hold: bool | None,
        commit: bool = False,
    ) -> None:
        """Hook from file upload when payment/status/hold fields change."""
        if account.last_payment_date and account.last_payment_date != previous_payment_date:
            self.on_payment_event(
                account.id,
                {
                    "amount": account.last_payment_amount,
                    "payment_date": account.last_payment_date.isoformat()
                    if account.last_payment_date
                    else None,
                    "is_ptp": account.last_payment_is_ptp,
                    "source": "crm_import",
                },
                commit=commit,
            )
        if (account.collection_status or "") != (previous_status or "") or (
            bool(account.provincial_hold) != bool(previous_hold)
        ):
            self.on_case_status_changed(
                account.id,
                previous=previous_status,
                new=account.collection_status,
                provincial_hold=account.provincial_hold,
                commit=commit,
            )

    def _advance_run_from_event(
        self,
        run: PayflowWorkflowRunModel,
        *,
        event: str,
        context: dict[str, Any],
    ) -> bool:
        if run.status in _TERMINAL or run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value:
            return False
        strategy = self.db.get(PayflowStrategyModel, run.strategy_id)
        if not strategy or not run.current_step_id:
            return False
        steps = {
            s.get("id"): s
            for s in (strategy.steps or [])
            if isinstance(s, dict) and s.get("id")
        }
        step = steps.get(run.current_step_id)
        if not step:
            return False
        kind = step.get("kind") or ""
        cfg = _cfg(step)

        # Condition steps: re-evaluate and enqueue the matching branch.
        if kind == "Condition":
            account = self.db.get(PayflowAccountModel, run.account_id)
            if not account:
                return False
            matched = self._eval_condition(
                account,
                cfg,
                step,
                context,
            )
            next_id = (step.get("yes") or cfg.get("yes")) if matched else (
                step.get("no") or cfg.get("no")
            )
            if next_id:
                run.status = PayflowWorkflowRunStatus.ACTIVE.value
                enqueue_execute_step(
                    self.db,
                    run_id=run.id,
                    step_id=next_id,
                    account_id=run.account_id,
                    strategy_id=run.strategy_id,
                    commit=False,
                )
                return True
            return False

        # Optional wait_for on Communication / Payment Action configs.
        wait_for = cfg.get("wait_for") or step.get("wait_for")
        if wait_for:
            if event == "communication" and str(wait_for).lower() in {
                str(context.get("comm_status") or "").lower(),
                str(context.get("status") or "").lower(),
            }:
                next_id = step.get("next") or cfg.get("next")
                if next_id:
                    run.status = PayflowWorkflowRunStatus.ACTIVE.value
                    enqueue_execute_step(
                        self.db,
                        run_id=run.id,
                        step_id=next_id,
                        account_id=run.account_id,
                        strategy_id=run.strategy_id,
                        commit=False,
                    )
                    return True
            if event == "payment" and str(wait_for).lower() in {
                "payment",
                "paid",
                "payment_received",
            }:
                next_id = step.get("next") or cfg.get("next")
                if next_id:
                    run.status = PayflowWorkflowRunStatus.ACTIVE.value
                    enqueue_execute_step(
                        self.db,
                        run_id=run.id,
                        step_id=next_id,
                        account_id=run.account_id,
                        strategy_id=run.strategy_id,
                        commit=False,
                    )
                    return True

        # Waiting runs: payment/status can resume current step.
        if run.status == PayflowWorkflowRunStatus.WAITING.value and event in {
            "payment",
            "case_status",
            "communication",
        }:
            enqueue_resume_run(
                self.db,
                run_id=run.id,
                step_id=run.current_step_id,
                account_id=run.account_id,
                commit=False,
            )
            return True
        return False

    def on_human_review_decided(self, review_id: int, *, continue_workflow: bool) -> None:
        run = (
            self.db.query(PayflowWorkflowRunModel)
            .filter(
                PayflowWorkflowRunModel.human_review_id == review_id,
                PayflowWorkflowRunModel.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value,
            )
            .first()
        )
        if not run:
            return
        if continue_workflow:
            enqueue_resume_run(
                self.db,
                run_id=run.id,
                step_id=run.current_step_id,
                account_id=run.account_id,
                commit=False,
            )
            self.log_event(
                event_type="human_review_continue",
                message=f"Resume queued after review #{review_id}",
                run_id=run.id,
                account_id=run.account_id,
                strategy_id=run.strategy_id,
            )
        else:
            run.status = PayflowWorkflowRunStatus.CANCELLED.value
            run.completed_at = _utcnow()
            cancel_pending_jobs_for_run(self.db, run.id)
            self.log_event(
                event_type="human_review_cancelled_run",
                message=f"Run cancelled after review #{review_id}",
                run_id=run.id,
                account_id=run.account_id,
                strategy_id=run.strategy_id,
            )
        self.db.commit()
