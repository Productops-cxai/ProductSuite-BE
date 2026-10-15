"""Workflow enrollment, step advance, regenerate supersede, and worker log helpers."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowCommunicationModel,
    PayflowStrategyModel,
    PayflowWorkflowRunModel,
    PayflowWorkflowStepRunModel,
    PayflowWorkflowWorkerLogModel,
)
from app.infrastructure.email.sendgrid_service import send_collection_email
from app.infrastructure.sms.twilio_service import send_collection_sms
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
    PayflowCommStatus,
    PayflowNotificationType,
    PayflowWorkflowRunStatus,
)

logger = logging.getLogger("app.workflow.execution")

_TERMINAL = {
    PayflowWorkflowRunStatus.COMPLETED.value,
    PayflowWorkflowRunStatus.CANCELLED.value,
    PayflowWorkflowRunStatus.FAILED.value,
    PayflowWorkflowRunStatus.SUPERSEDED.value,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cfg(step: dict) -> dict:
    cfg = step.get("config")
    return cfg if isinstance(cfg, dict) else {}


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

    def on_strategy_deactivated(self, strategy_id: int) -> int:
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
                message="Run cancelled — strategy deactivated",
                run_id=run.id,
                account_id=run.account_id,
                strategy_id=strategy_id,
                client_id=run.client_id,
            )
        self.db.commit()
        return cancelled

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
            step_run.status = "skipped"
            step_run.result_json = {"reason": "Prevent Communication", "rule_id": matched["rule_id"]}
            step_run.finished_at = _utcnow()
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
            step_run.status = "held"
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
    ) -> str | None:
        kind = step.get("kind") or ""
        cfg = _cfg(step)

        if kind == "Trigger":
            return step.get("next") or cfg.get("next")

        if kind == "Wait":
            delay = self._wait_delta(cfg, step)
            next_id = step.get("next") or cfg.get("next")
            run.status = PayflowWorkflowRunStatus.WAITING.value
            step_run.result_json = {"wait_until": (_utcnow() + delay).isoformat(), "next": next_id}
            if next_id:
                enqueue_execute_step(
                    self.db,
                    run_id=run.id,
                    step_id=next_id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                    scheduled_at=_utcnow() + delay,
                    commit=False,
                )
                account.next_action = f"Wait until next step {next_id}"
                # Keep Waiting until next job fires; execute_step will set Active again.
            return None  # next already scheduled with delay

        if kind == "Communication":
            self._send_communication(run, account, strategy, step, step_run)
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            return step.get("next") or cfg.get("next")

        if kind == "Condition":
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            yes_id = step.get("yes") or cfg.get("yes")
            no_id = step.get("no") or cfg.get("no")
            paid = float(account.outstanding_balance or 0) <= 0 or (
                account.collection_status or ""
            ).lower() in {"resolved", "promise to pay", "payment plan"}
            # Default condition: payment / arrangement progress.
            return yes_id if paid else no_id

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

        if kind in {"Payment Action", "Case Action"}:
            action = cfg.get("action") or step.get("action") or kind
            timeline = list(account.timeline or [])
            timeline.append(
                {"at": _utcnow().isoformat(), "label": action, "source": "workflow"}
            )
            account.timeline = timeline
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            return step.get("next") or cfg.get("next")

        if kind == "AI Reassessment":
            account.next_action = "AI reassessment queued"
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            return step.get("next") or cfg.get("next")

        if kind == "Outcome":
            run.status = PayflowWorkflowRunStatus.COMPLETED.value
            run.completed_at = _utcnow()
            account.next_action = None
            account.last_action = f"Outcome: {step.get('title') or 'done'}"
            return None

        # Unknown kinds: advance if next exists.
        run.status = PayflowWorkflowRunStatus.ACTIVE.value
        return step.get("next") or cfg.get("next")

    def _wait_delta(self, cfg: dict, step: dict) -> timedelta:
        amount = cfg.get("amount")
        if amount is None:
            amount = step.get("amount", 3)
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            amount = 3
        unit = (cfg.get("unit") or step.get("unit") or "Days").lower()
        if unit.startswith("hour"):
            return timedelta(hours=max(amount, 0))
        if unit.startswith("minute"):
            return timedelta(minutes=max(amount, 0))
        return timedelta(days=max(amount, 0))

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

        send_status = "logged"
        if channel == "SMS":
            phone = (account.phone_mobile or account.phone_work or "").strip()
            if not phone:
                step_run.result_json = {"skipped": "no_phone"}
                self.log_event(
                    event_type="sms_skipped",
                    message="No phone on account",
                    level="warn",
                    run_id=run.id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                )
                return
            sms = send_collection_sms(
                self.db,
                to_phone=phone,
                body=body,
                sms_type="collection_workflow",
                account_id=account.id,
                run_id=run.id,
                client_id=run.client_id,
                commit=False,
            )
            send_status = sms.status
            account.last_sms_sent_date = _utcnow().date()
            if sms.status == "failed":
                self._notify_provider_failed(run, "SMS", sms.error or "failed")
        else:
            email = (account.email or "").strip()
            if not email:
                step_run.result_json = {"skipped": "no_email"}
                self.log_event(
                    event_type="email_skipped",
                    message="No email on account",
                    level="warn",
                    run_id=run.id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                )
                return
            log = send_collection_email(
                self.db,
                to_email=email,
                subject=subject,
                body=body,
                email_type="collection_workflow",
                action_link=self._payment_link(account),
                commit=False,
            )
            send_status = log.status
            account.last_email_sent_date = _utcnow().date()
            if log.status == "failed":
                self._notify_provider_failed(run, "Email", "sendgrid_failed")

        account.last_contact_date = _utcnow().date()
        code = f"COM-{uuid4().hex[:10].upper()}"
        comm = PayflowCommunicationModel(
            code=code,
            client_id=run.client_id,
            account_id=account.id,
            channel=channel,
            purpose=purpose,
            status=PayflowCommStatus.SENT.value
            if send_status in {"sent", "logged"}
            else PayflowCommStatus.FAILED.value,
            workflow_name=strategy.name,
            subject=subject if channel != "SMS" else None,
            body_lines=body.splitlines()[:40],
            payment_link=True,
            why_message="Workflow communication step",
            why_channel=channel,
            why_timing="Scheduled by workflow worker",
            events=[{"at": _utcnow().isoformat(), "label": f"{channel} {send_status}"}],
            balance=float(account.outstanding_balance or 0),
        )
        self.db.add(comm)
        step_run.result_json = {"channel": channel, "status": send_status, "comm_code": code}

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
            )
        except Exception:  # noqa: BLE001
            logger.exception("provider failed notify failed")

    # ------------------------------------------------------------------ list APIs
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
            q = q.filter(PayflowWorkflowWorkerLogModel.message.ilike(like))
        rows = q.order_by(PayflowWorkflowWorkerLogModel.id.desc()).limit(min(limit, 500)).all()
        return [
            {
                "id": r.id,
                "event_type": r.event_type,
                "level": r.level,
                "message": r.message,
                "detail": r.detail,
                "client_id": r.client_id,
                "portfolio_id": r.portfolio_id,
                "account_id": r.account_id,
                "strategy_id": r.strategy_id,
                "run_id": r.run_id,
                "job_id": r.job_id,
                "step_id": r.step_id,
                "created_at": r.created_at,
            }
            for r in rows
        ]

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
        return [
            {
                "id": r.id,
                "account_id": r.account_id,
                "strategy_id": r.strategy_id,
                "strategy_version": r.strategy_version,
                "client_id": r.client_id,
                "portfolio_id": r.portfolio_id,
                "status": r.status,
                "current_step_id": r.current_step_id,
                "human_review_id": r.human_review_id,
                "started_at": r.started_at,
                "completed_at": r.completed_at,
                "last_error": r.last_error,
            }
            for r in rows
        ]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        run = self.db.get(PayflowWorkflowRunModel, run_id)
        if not run:
            return None
        steps = (
            self.db.query(PayflowWorkflowStepRunModel)
            .filter(PayflowWorkflowStepRunModel.run_id == run_id)
            .order_by(PayflowWorkflowStepRunModel.id.asc())
            .all()
        )
        return {
            "id": run.id,
            "account_id": run.account_id,
            "strategy_id": run.strategy_id,
            "strategy_version": run.strategy_version,
            "client_id": run.client_id,
            "portfolio_id": run.portfolio_id,
            "status": run.status,
            "current_step_id": run.current_step_id,
            "human_review_id": run.human_review_id,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "last_error": run.last_error,
            "step_runs": [
                {
                    "id": s.id,
                    "step_id": s.step_id,
                    "kind": s.kind,
                    "attempt": s.attempt,
                    "status": s.status,
                    "scheduled_at": s.scheduled_at,
                    "started_at": s.started_at,
                    "finished_at": s.finished_at,
                    "error": s.error,
                    "result_json": s.result_json,
                }
                for s in steps
            ],
        }

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
