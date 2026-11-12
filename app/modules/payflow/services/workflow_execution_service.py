"""Workflow enrollment, step advance, regenerate supersede, and worker log helpers."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowHumanReviewModel,
    PayflowPortfolioModel,
    PayflowStrategyModel,
    PayflowStrategyVersionModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
    PayflowWorkflowStepRunModel,
    PayflowWorkflowWorkerLogModel,
)
from app.modules.payflow.services.communication_service import PayflowCommunicationService
from app.modules.payflow.services.rule_runtime_service import RuleRuntimeService
from app.modules.payflow.services.strategy_service import MESSAGE_TEMPLATES
from app.modules.payflow.services.workflow_condition_runtime import (
    FIXED_DATE_REFERENCE_EVENTS,
    apply_outcome_to_account,
    channel_delivery,
    compare_values,
    resolve_condition_actual,
    resolve_reference_anchor,
    timedelta_for_unit,
)
from app.modules.payflow.services.workflow_contact_runtime import (
    audience_message_config,
    flag_unreachable,
    is_contact_step,
    localized_message_config,
    localized_template,
    message_for_channel,
    preferred_contact_channels,
    send_succeeded,
    should_flag_unreachable,
)
from app.modules.payflow.services.workflow_queue import (
    cancel_pending_jobs_for_run,
    enqueue_enroll_strategy,
    enqueue_execute_step,
    enqueue_resume_run,
)
from app.modules.payflow.services.workflow_suggestion.segmenter import (
    match_account_to_segment,
)
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
    "waiting": "Waiting",
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


# Case Actions that hand the case to a person: the run pauses on a human review, which also
# keeps other workflows from picking the account up (collection status "Human Review").
# The supervisor's decision releases it: continue -> the workflow goes on, reject -> the run ends.
CASE_ACTION_HOLD = "hold automated contact"
CASE_ACTION_ESCALATE = "move case to escalated treatment"
CASE_ACTION_CONTACT_UPDATE = "request contact details update"
CASE_ACTION_CLOSE = "close case"
_REVIEW_CASE_ACTIONS = {
    CASE_ACTION_HOLD: "Automated contact is on hold - release it to continue the workflow",
    CASE_ACTION_ESCALATE: "Case moved to escalated treatment - decide the next step",
}
PAYMENT_ACTION_RECORD_PTP = "record promise to pay"
PAYMENT_ACTION_SEND_LINK = "send secure payment link"
PAYMENT_ACTION_OFFER_PLAN = "offer payment plan"
# What "Offer Payment Plan" found (step result "plan_offer"):
PLAN_OFFER_AVAILABLE = "available"  # the customer can set up a plan on their payment page
PLAN_OFFER_ALREADY_ON_PLAN = "already_on_plan"
PLAN_OFFER_NOT_ALLOWED = "not_allowed"  # the client's payment page does not offer plans
PLAN_OFFER_NO_PAGE = "no_payment_page"  # the customer's payment page could not be prepared
PLAN_OFFER_NOT_POSSIBLE = "Payment plan offer not possible - needs review"
# A Condition on "Email Delivery" / "SMS Delivery" right after a send: the provider's delivery
# report arrives seconds to minutes later. Until then the message is neither delivered nor failed,
# so the Condition waits for the report - at most this long, then it decides on what is known.
DELIVERY_REPORT_WAIT = timedelta(minutes=10)
_DELIVERY_ATTRIBUTES = {"email delivery": "Email", "sms delivery": "SMS"}
_IN_TRANSIT_COMM_STATUSES = (PayflowCommStatus.QUEUED.value, PayflowCommStatus.SENT.value)
# Outcomes that only confirm a case that is already Resolved: they may still run for it.
_CLOSING_OUTCOMES = {"case closed", "paid in full"}


def _aware(value: datetime | None) -> datetime | None:
    """Datetimes read back from the database may have lost their timezone (they are UTC)."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


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


# Enroll: commit progress and refresh the job lock every N accounts.
ENROLL_COMMIT_EVERY = 100

# Run.last_error marker: a supervisor stopped the run from a review (rejected it, or
# chose a different treatment), so the same workflow version must not pick the
# account up again on the next import.
REVIEW_STOPPED_PREFIX = "Human review"
REVIEW_REJECTED_REASON = f"{REVIEW_STOPPED_PREFIX} rejected"


def _account_enroll_block_reason(account: PayflowAccountModel) -> str | None:
    """Accounts that must not start a workflow (same exclusions as AI segmenting)."""
    if (account.account_status or "").strip().upper() == "CLOSED":
        return "Account is closed — not enrolled"
    if float(account.outstanding_balance or 0) <= 0:
        return "Nothing owed — not enrolled"
    if (account.collection_status or "") == PayflowCollectionStatus.HUMAN_REVIEW.value:
        return "Case is in Human Review — not enrolled"
    return None


def _is_resumable_block(step_run: PayflowWorkflowStepRunModel) -> bool:
    """A step stopped by a hold has not run yet, so it may run once the hold lifts.

    "Prevent Communication" is different: that message is dropped for good and the
    workflow already moved on.
    """
    if step_run.status != "blocked":
        return False
    result = step_run.result_json if isinstance(step_run.result_json, dict) else {}
    return result.get("reason") != "Prevent Communication"


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
            if run.human_review_id:
                # Nothing is left to approve: do not leave it in the supervisors' queue.
                self._close_superseded_review(
                    run.human_review_id, event="Workflow deactivated — review closed"
                )
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
        else:
            q = q.filter(PayflowStrategyModel.portfolio_id.is_(None))
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

    def _close_superseded_review(
        self, review_id: int, *, event: str = "Superseded by regenerated workflow"
    ) -> None:
        from app.infrastructure.database.models import PayflowHumanReviewModel
        from app.shared.enums import PayflowReviewStatus

        review = self.db.get(PayflowHumanReviewModel, review_id)
        if not review:
            return
        open_statuses = {
            PayflowReviewStatus.AWAITING_REVIEW.value,
            PayflowReviewStatus.ON_HOLD.value,
        }
        if review.status not in open_statuses:
            return
        review.status = PayflowReviewStatus.REJECTED.value
        history = list(review.history or [])
        history.append({"at": _utcnow().isoformat(), "event": event, "by": "Workflow worker"})
        review.history = history
        self.db.flush()
        # The review blocked the case; with it closed the case must be workable again,
        # unless another review on the same account is still open.
        if not review.account_id:
            return
        still_open = (
            self.db.query(PayflowHumanReviewModel.id)
            .filter(
                PayflowHumanReviewModel.account_id == review.account_id,
                PayflowHumanReviewModel.id != review.id,
                PayflowHumanReviewModel.status.in_(list(open_statuses)),
            )
            .first()
        )
        if still_open:
            return
        account = self.db.get(PayflowAccountModel, review.account_id)
        if account is None:
            return
        account.human_review = False
        if account.collection_status == PayflowCollectionStatus.HUMAN_REVIEW.value:
            account.collection_status = PayflowCollectionStatus.ACTIVE.value
        account.next_action = None

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
        else:
            # Client-level strategy: only parent accounts (no sub-client portfolio).
            q = q.filter(PayflowAccountModel.portfolio_id.is_(None))
        accounts = q.all()

        # Every Active workflow of this scope: an account belongs to the most
        # specific one it fits, so overlapping workflows never fight over it.
        scope_q = self.db.query(PayflowStrategyModel).filter(
            PayflowStrategyModel.client_id == strategy.client_id,
            PayflowStrategyModel.status == "Active",
        )
        if strategy.portfolio_id is not None:
            scope_q = scope_q.filter(PayflowStrategyModel.portfolio_id == strategy.portfolio_id)
        else:
            scope_q = scope_q.filter(PayflowStrategyModel.portfolio_id.is_(None))
        active_in_scope = scope_q.order_by(PayflowStrategyModel.id.asc()).all()
        candidates = [{"id": s.id, "segment": s.segment or {}} for s in active_in_scope]
        active_ids = {s.id for s in active_in_scope}

        enrolled = 0
        skipped = 0
        unmatched = 0  # eligible accounts that fit no Active workflow of this scope
        today = datetime.now(timezone.utc).date()
        self._touch_job(job_id)
        for index, account in enumerate(accounts, start=1):
            # Save progress and keep the job lock fresh: a large portfolio can take
            # longer than the stale window, and a reclaimed copy of this job must
            # see the runs already created instead of creating them again.
            if index % ENROLL_COMMIT_EVERY == 0:
                self._touch_job(job_id)
                self.db.commit()
            block = _account_execution_block_reason(account) or _account_enroll_block_reason(
                account
            )
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
            best = match_account_to_segment(account, candidates, today)
            if not best or best["id"] != strategy.id:
                skipped += 1
                if not best:
                    unmatched += 1
                continue
            open_runs = (
                self.db.query(PayflowWorkflowRunModel)
                .filter(
                    PayflowWorkflowRunModel.account_id == account.id,
                    PayflowWorkflowRunModel.status.notin_(list(_TERMINAL)),
                )
                .all()
            )
            # A run in flight on an Active workflow is never restarted: neither by
            # this workflow again nor because the account drifted into another segment.
            if any(r.strategy_id in active_ids for r in open_runs):
                skipped += 1
                continue
            # This workflow version already ran to an end for the account: wait for
            # a new version instead of starting again from the first message.
            finished = (
                self.db.query(PayflowWorkflowRunModel)
                .filter(
                    PayflowWorkflowRunModel.account_id == account.id,
                    PayflowWorkflowRunModel.strategy_id == strategy.id,
                    PayflowWorkflowRunModel.strategy_version == int(strategy.version or 1),
                    or_(
                        PayflowWorkflowRunModel.status.in_(
                            [
                                PayflowWorkflowRunStatus.COMPLETED.value,
                                PayflowWorkflowRunStatus.FAILED.value,
                            ]
                        ),
                        and_(
                            PayflowWorkflowRunModel.status
                            == PayflowWorkflowRunStatus.CANCELLED.value,
                            PayflowWorkflowRunModel.last_error.like(f"{REVIEW_STOPPED_PREFIX}%"),
                        ),
                    ),
                )
                .first()
            )
            if finished:
                skipped += 1
                continue
            # Left-over runs on workflows that are no longer Active.
            for r in open_runs:
                r.status = PayflowWorkflowRunStatus.SUPERSEDED.value
                r.completed_at = _utcnow()
                cancel_pending_jobs_for_run(self.db, r.id)
                if r.human_review_id:
                    self._close_superseded_review(r.human_review_id)

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
                # "Payment Status" counts payments made from here on (workflow_condition_runtime).
                recovered_at_start=float(account.recovered_balance or 0),
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
        if unmatched:
            # Not silent any more: these accounts moved into (or arrived in) a segment that no
            # Active workflow covers, so nothing contacts them until a workflow is added.
            plural = "" if unmatched == 1 else "s"
            self.log_event(
                event_type="enroll_unmatched",
                message=(
                    f"{unmatched} account{plural} in this scope fit no Active workflow - "
                    "no workflow is running for them"
                ),
                level="warn",
                strategy_id=strategy_id,
                client_id=strategy.client_id,
                portfolio_id=strategy.portfolio_id,
                job_id=job_id,
                commit=True,
            )
        return {"enrolled": enrolled, "skipped": skipped, "unmatched": unmatched}

    def _touch_job(self, job_id: int | None) -> None:
        """Heartbeat: tell the worker this job is alive so it is not reclaimed."""
        if job_id is None:
            return
        job = self.db.get(PayflowWorkflowJobModel, job_id)
        if job is not None:
            job.locked_at = _utcnow()

    def on_job_failed_for_good(self, job_id: int, error: str) -> None:
        """A job used its last attempt: surface it and close the run it belonged to."""
        job = self.db.get(PayflowWorkflowJobModel, job_id)
        if not job:
            return
        self.log_event(
            event_type="job_failed",
            message=f"{job.job_type} job failed after {job.attempts} attempts: {error}"[:500],
            level="error",
            detail=error[:2000],
            run_id=job.run_id,
            account_id=job.account_id,
            strategy_id=job.strategy_id,
            job_id=job.id,
            step_id=job.step_id,
        )
        run = self.db.get(PayflowWorkflowRunModel, job.run_id) if job.run_id else None
        if run is not None and run.status not in _TERMINAL:
            run.status = PayflowWorkflowRunStatus.FAILED.value
            run.last_error = error[:1000]
            run.completed_at = _utcnow()
            cancel_pending_jobs_for_run(self.db, run.id)
            account = self.db.get(PayflowAccountModel, run.account_id)
            if account is not None:
                account.next_action = "Workflow failed - needs attention"
            self.log_event(
                event_type="run_failed",
                message=f"Run failed: step {job.step_id or 'unknown'} could not be executed",
                level="error",
                run_id=run.id,
                account_id=run.account_id,
                strategy_id=run.strategy_id,
                job_id=job.id,
                step_id=job.step_id,
            )
            self._notify_step_failed(run, error[:200])
        self.db.commit()

    def gap_fill_active_strategies(self, *, job_id: int | None = None) -> dict[str, Any]:
        """Enroll accounts that match Active strategies but have no open run."""
        strategies = (
            self.db.query(PayflowStrategyModel)
            .filter(PayflowStrategyModel.status == "Active")
            .all()
        )
        totals = {"enrolled": 0, "skipped": 0}
        for s in strategies:
            result = self.enroll_strategy(s.id, job_id=job_id)
            totals["enrolled"] += int(result.get("enrolled") or 0)
            totals["skipped"] += int(result.get("skipped") or 0)
        return totals

    def enqueue_enrolls_for_scopes(
        self,
        *,
        portfolio_pairs: set[tuple[int, int]] | list[tuple[int, int]] | None = None,
        parent_client_ids: set[int] | list[int] | None = None,
        commit: bool = False,
    ) -> list[PayflowWorkflowJobModel]:
        """Queue enroll jobs for Active strategies in the given import scopes.

        Activate-time enrollment is unchanged. This only schedules the same
        ``enroll_strategy`` job used on activate, so accounts already running
        are skipped inside ``enroll_strategy`` (open-run check).
        """
        strategy_ids: set[int] = set()
        pairs = list(portfolio_pairs or [])
        parents = list(parent_client_ids or [])

        if pairs:
            scope_filters = [
                and_(
                    PayflowStrategyModel.client_id == client_id,
                    PayflowStrategyModel.portfolio_id == portfolio_id,
                )
                for client_id, portfolio_id in pairs
            ]
            rows = (
                self.db.query(PayflowStrategyModel.id)
                .filter(
                    PayflowStrategyModel.status == "Active",
                    or_(*scope_filters),
                )
                .all()
            )
            strategy_ids.update(row[0] for row in rows)

        if parents:
            rows = (
                self.db.query(PayflowStrategyModel.id)
                .filter(
                    PayflowStrategyModel.status == "Active",
                    PayflowStrategyModel.client_id.in_(parents),
                    PayflowStrategyModel.portfolio_id.is_(None),
                )
                .all()
            )
            strategy_ids.update(row[0] for row in rows)

        jobs: list[PayflowWorkflowJobModel] = []
        for strategy_id in sorted(strategy_ids):
            jobs.append(enqueue_enroll_strategy(self.db, strategy_id, commit=False))
            self.log_event(
                event_type="enroll_queued",
                message=f"Enroll queued after import for strategy {strategy_id}",
                strategy_id=strategy_id,
                job_id=jobs[-1].id,
                commit=False,
            )
        if commit and jobs:
            self.db.commit()
        return jobs

    # ------------------------------------------------------------------ execute / resume
    def _run_steps(
        self, run: PayflowWorkflowRunModel, strategy: PayflowStrategyModel
    ) -> list[dict]:
        """Steps of the workflow version this run was enrolled on.

        Editing an Active workflow moves it to Under Review with a new version.
        Runs already in flight keep the approved steps that were archived at that
        moment, so unapproved edits (or AI rewrites) never reach a customer.
        """
        live = list(strategy.steps or [])
        run_version = int(run.strategy_version or 1)
        if strategy.status == "Active" and int(strategy.version or 1) == run_version:
            return live
        cached = getattr(self, "_steps_cache", None)
        if cached is None:
            cached = self._steps_cache = {}
        key = (strategy.id, run_version)
        if key not in cached:
            snapshot = (
                self.db.query(PayflowStrategyVersionModel)
                .filter(
                    PayflowStrategyVersionModel.strategy_id == strategy.id,
                    PayflowStrategyVersionModel.version == run_version,
                    PayflowStrategyVersionModel.status.in_(["Active", "Approved"]),
                    PayflowStrategyVersionModel.steps.isnot(None),
                )
                .order_by(PayflowStrategyVersionModel.id.desc())
                .first()
            )
            cached[key] = list(snapshot.steps or []) if snapshot else None
        return cached[key] if cached[key] is not None else live

    def resume_released_holds(self, *, limit: int = 200) -> dict[str, Any]:
        """Wake runs whose hold has lifted (worker sweep).

        A run parked by a case hold or a "Hold Action" rule has no scheduled job.
        Rule edits, rule deactivation and balance changes raise no event, so the
        worker re-checks parked runs and resumes those that are free to continue.
        """
        busy = (
            self.db.query(PayflowWorkflowJobModel.id)
            .filter(
                PayflowWorkflowJobModel.run_id == PayflowWorkflowRunModel.id,
                PayflowWorkflowJobModel.status.in_(
                    [
                        PayflowWorkflowJobStatus.PENDING.value,
                        PayflowWorkflowJobStatus.RUNNING.value,
                    ]
                ),
            )
            .exists()
        )
        runs = (
            self.db.query(PayflowWorkflowRunModel)
            .filter(
                PayflowWorkflowRunModel.status == PayflowWorkflowRunStatus.WAITING.value,
                ~busy,
            )
            .order_by(PayflowWorkflowRunModel.updated_at.asc())
            .limit(limit)
            .all()
        )
        resumed = 0
        for run in runs:
            last_sr = (
                self.db.query(PayflowWorkflowStepRunModel)
                .filter(PayflowWorkflowStepRunModel.run_id == run.id)
                .order_by(PayflowWorkflowStepRunModel.id.desc())
                .first()
            )
            if last_sr is None or not _is_resumable_block(last_sr):
                continue
            account = self.db.get(PayflowAccountModel, run.account_id)
            strategy = self.db.get(PayflowStrategyModel, run.strategy_id)
            if not account or not strategy:
                continue
            if _account_execution_block_reason(account):
                continue
            step = next(
                (
                    s
                    for s in self._run_steps(run, strategy)
                    if isinstance(s, dict) and s.get("id") == last_sr.step_id
                ),
                None,
            )
            channel = (_cfg(step).get("channel") or step.get("channel")) if step else None
            blocking = self.rules.first_blocking_action(
                account,
                channel=channel if isinstance(channel, str) else None,
                for_communication=(last_sr.kind == "Communication"),
            )
            if blocking and blocking.get("action") == "Hold Action":
                continue
            run.last_error = None
            enqueue_resume_run(
                self.db,
                run_id=run.id,
                step_id=run.current_step_id,
                account_id=run.account_id,
                commit=False,
            )
            self.log_event(
                event_type="run_resumed",
                message="Hold released — resume queued",
                run_id=run.id,
                account_id=run.account_id,
                strategy_id=run.strategy_id,
                step_id=last_sr.step_id,
            )
            resumed += 1
        self.db.commit()
        return {"examined": len(runs), "resumed": resumed}

    def resume_run(
        self,
        run_id: int,
        *,
        from_step_id: str | None = None,
        job_id: int | None = None,
        after_human_review: bool = False,
    ) -> None:
        run = self.db.get(PayflowWorkflowRunModel, run_id)
        if not run or run.status in _TERMINAL:
            return
        strategy = self.db.get(PayflowStrategyModel, run.strategy_id)
        if not strategy:
            return
        account = self.db.get(PayflowAccountModel, run.account_id)
        step_id = from_step_id or run.current_step_id
        steps = {s.get("id"): s for s in self._run_steps(run, strategy) if isinstance(s, dict) and s.get("id")}
        step = steps.get(step_id or "")
        step_kind = (step.get("kind") or "") if step else ""
        # After human review:
        # - Human Review *step*: supervisor already decided → advance to .next
        #   (re-running the step would create another Awaiting review).
        # - Terminal Human Review (no next): complete the run.
        # - Communication (or other) paused by a governance rule: re-execute that
        #   step so the email/SMS actually sends — do not jump to Wait.
        reexecute_paused = False
        complete_run = False
        target_id: str | None = None
        if after_human_review:
            if step_kind == "Human Review":
                target_id = (step.get("next") or _cfg(step).get("next")) if step else None
                # Close the paused HR step so activity no longer shows Awaiting Review.
                paused_sr = (
                    self.db.query(PayflowWorkflowStepRunModel)
                    .filter(
                        PayflowWorkflowStepRunModel.run_id == run.id,
                        PayflowWorkflowStepRunModel.step_id == step_id,
                        PayflowWorkflowStepRunModel.status == "paused_human_review",
                    )
                    .order_by(PayflowWorkflowStepRunModel.id.desc())
                    .first()
                )
                if paused_sr:
                    paused_sr.status = "done"
                    paused_sr.finished_at = _utcnow()
                if not target_id:
                    complete_run = True
            else:
                target_id = step_id
                reexecute_paused = True
        else:
            last_sr = (
                self.db.query(PayflowWorkflowStepRunModel)
                .filter(
                    PayflowWorkflowStepRunModel.run_id == run.id,
                    PayflowWorkflowStepRunModel.step_id == step_id,
                )
                .order_by(PayflowWorkflowStepRunModel.id.desc())
                .first()
            )
            if last_sr is not None and _is_resumable_block(last_sr):
                # The step never ran (hold / governance): run it now.
                target_id = step_id
            elif step:
                target_id = step.get("next") or _cfg(step).get("next")
            if not target_id:
                target_id = step_id

        run.human_review_id = None
        if complete_run:
            run.status = PayflowWorkflowRunStatus.COMPLETED.value
            run.completed_at = _utcnow()
            if account:
                account.next_action = None
                account.last_action = f"Human Review completed: {step.get('title') or step_id}"
            self.log_event(
                event_type="run_completed",
                message=f"Run {run_id} completed after terminal Human Review",
                run_id=run.id,
                account_id=run.account_id,
                strategy_id=run.strategy_id,
                job_id=job_id,
                step_id=step_id,
            )
            self.db.commit()
            return

        run.status = PayflowWorkflowRunStatus.ACTIVE.value
        self.db.flush()
        if target_id:
            enqueue_execute_step(
                self.db,
                run_id=run.id,
                step_id=target_id,
                account_id=run.account_id,
                strategy_id=run.strategy_id,
                payload={"after_human_review": True} if reexecute_paused else None,
                commit=False,
            )
        self.log_event(
            event_type="run_resumed",
            message=(
                f"Run {run_id} resumed after human review — "
                + (
                    "re-executing paused step"
                    if reexecute_paused
                    else "advancing past Human Review step"
                    if after_human_review
                    else "continuing"
                )
            ),
            run_id=run.id,
            account_id=run.account_id,
            strategy_id=run.strategy_id,
            job_id=job_id,
            step_id=target_id,
        )
        self.db.commit()

    def execute_step(
        self,
        run_id: int,
        step_id: str,
        *,
        job_id: int | None = None,
        after_human_review: bool = False,
        after_comm_delay: bool = False,
    ) -> None:
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

        # Scheduled post-Wait job: close the Wait row so the timeline no longer
        # shows "Waiting" after the wait window has elapsed.
        self._close_waits_targeting_step(run.id, step_id)
        if after_comm_delay:
            self._close_deferred_communication(run.id, step_id)
            self._close_awaiting_delivery(run.id, step_id, reason="delivery_report_wait_elapsed")

        # Idempotency: do not re-execute a step that already completed successfully.
        # Exception: after supervisor approve, re-run the step that was only paused
        # (communication never sent) — paused_human_review is not a successful send.
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
            allow_resume = _is_resumable_block(prior_ok) or (
                after_human_review and prior_ok.status == "paused_human_review"
            ) or (
                after_comm_delay
                and prior_ok.status in {"scheduled", "waiting", "done"}
                and isinstance(prior_ok.result_json, dict)
                and (
                    prior_ok.result_json.get("deferred_send")
                    or prior_ok.result_json.get("awaiting_delivery_report")
                )
            )
            if not allow_resume:
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
        if block and self._is_closing_outcome(run, strategy, step_id, account):
            # "Close Case" -> Outcome "Case Closed": the Outcome only confirms the closed case.
            block = None
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
                                    for s in self._run_steps(run, strategy)
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

        steps = {s.get("id"): s for s in self._run_steps(run, strategy) if isinstance(s, dict) and s.get("id")}
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
            # Supervisor already approved continue — do not re-pause this step on
            # Require Human Review / Escalate (email would never send otherwise).
            exclude_actions = (
                frozenset({"Require Human Review", "Escalate Case"})
                if after_human_review
                else None
            )
            if after_human_review:
                self.log_event(
                    event_type="rule_bypassed_after_review",
                    message="Skipping Require Human Review / Escalate after approval",
                    run_id=run.id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                    job_id=job_id,
                    step_id=step_id,
                    level="info",
                )
            # RHR / Escalate / Prevent Comm only on Communication (see rule_runtime).
            # Hold / Reassessment / Change Workflow may still apply on other steps.
            blocking = self.rules.first_blocking_action(
                account,
                channel=channel if isinstance(channel, str) else None,
                for_communication=(kind == "Communication"),
                exclude_actions=exclude_actions,
            )
            if blocking:
                handled = self._apply_rule_action(
                    run, account, strategy, step, step_run, blocking, job_id=job_id
                )
                if handled:
                    self.db.commit()
                    return

            next_id = self._dispatch_kind(
                run,
                account,
                strategy,
                step,
                step_run,
                steps,
                after_comm_delay=after_comm_delay,
                after_human_review=after_human_review,
            )
            if run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value:
                self.db.commit()
                return
            # Deferred Communication send: wait window in progress.
            if step_run.status in {"scheduled", "waiting"} and run.status == (
                PayflowWorkflowRunStatus.WAITING.value
            ):
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
            elif (
                run.status == PayflowWorkflowRunStatus.ACTIVE.value
                and step_run.status == "done"
            ):
                # Path ended on a non-Outcome leaf (Case Action / Communication / …).
                run.status = PayflowWorkflowRunStatus.COMPLETED.value
                run.completed_at = _utcnow()
                account.next_action = None
                self.log_event(
                    event_type="run_completed",
                    message=f"Run completed — path ended at {kind} ({step_id})",
                    run_id=run.id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                    job_id=job_id,
                    step_id=step_id,
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

    def _is_closing_outcome(
        self,
        run: PayflowWorkflowRunModel,
        strategy: PayflowStrategyModel,
        step_id: str,
        account: PayflowAccountModel,
    ) -> bool:
        """True for an Outcome "Case Closed" / "Paid In Full" step on a case that is already Resolved.

        Such an Outcome sends nothing and changes nothing; letting it run ends the run as
        Completed instead of Cancelled ("Case is Resolved").
        """
        if account.collection_status != PayflowCollectionStatus.RESOLVED.value:
            return False
        step = next(
            (s for s in self._run_steps(run, strategy) if isinstance(s, dict) and s.get("id") == step_id),
            None,
        )
        if not step or step.get("kind") not in {"Outcome", "End"}:
            return False
        label = _cfg(step).get("outcome") or step.get("outcome") or ""
        return str(label).strip().lower() in _CLOSING_OUTCOMES

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
            else:
                run.status = PayflowWorkflowRunStatus.COMPLETED.value
                run.completed_at = _utcnow()
                account.next_action = None
                self.log_event(
                    event_type="run_completed",
                    message="Run completed — last message was prevented by a governance rule",
                    run_id=run.id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                    job_id=job_id,
                    step_id=step.get("id"),
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
                reason="Payment arrangement exception",
            )
            return True

        if action == "Request Reassessment":
            step_run.status = "reassessment_requested"
            step_run.finished_at = _utcnow()
            try:
                from app.modules.payflow.services.case_reassessment_service import (
                    CaseReassessmentService,
                )

                CaseReassessmentService(self.db).run_reassessment(
                    account,
                    strategy,
                    trigger="rule_request_reassessment",
                )
            except Exception:  # noqa: BLE001
                logger.exception("rule Request Reassessment failed")
                account.next_action = "AI reassessment requested"
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
        proposed_action: str = "Continue workflow after supervisor review",
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
                proposed_action=proposed_action,
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
        after_comm_delay: bool = False,
        after_human_review: bool = False,
    ) -> str | None:
        kind = step.get("kind") or ""
        cfg = _cfg(step)
        ctx = event_context or {}

        if kind == "Trigger":
            return step.get("next") or cfg.get("next")

        if kind == "Wait":
            when = self._wait_until(cfg, step, strategy, account=account, run=run)
            next_id = step.get("next") or cfg.get("next")
            run.status = PayflowWorkflowRunStatus.WAITING.value
            # Not "done" — wait is in progress until scheduled_at elapses.
            step_run.status = "waiting"
            step_run.result_json = {
                "wait_until": when.isoformat(),
                "next": next_id,
                "timezone": self._wait_timezone_name(cfg, step, strategy),
                "reference_event": cfg.get("reference_event") or step.get("reference_event"),
                "direction": cfg.get("direction") or step.get("direction"),
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
            deferred_until = None
            if not after_comm_delay:
                deferred_until = self._communication_defer_until(
                    cfg, step, strategy, account=account, run=run
                )
            # Outside the client's send window: hold the message until the window
            # opens instead of logging it as skipped and moving on without it.
            window_opens = self._send_window_opens_at(account)
            if window_opens is not None and (
                deferred_until is None or window_opens > deferred_until
            ):
                deferred_until = window_opens
            # Jurisdiction compliance rules, asked before the message is prepared.
            verdict, permitted_at = self._compliance_gate(
                account, cfg, step, review_approved=after_human_review
            )
            if verdict == "review":
                from app.modules.payflow.services.review_service import (
                    COMPLIANCE_REVIEW_REASON,
                )

                self._pause_for_human_review(
                    run,
                    account,
                    strategy,
                    step,
                    step_run,
                    rule_id=None,
                    reason=COMPLIANCE_REVIEW_REASON,
                )
                return None
            if verdict == "defer" and permitted_at is not None and (
                deferred_until is None or permitted_at > deferred_until
            ):
                deferred_until = permitted_at
            if deferred_until is not None:
                run.status = PayflowWorkflowRunStatus.WAITING.value
                step_run.status = "scheduled"
                step_run.result_json = {
                    "wait_until": deferred_until.isoformat(),
                    "deferred_send": True,
                    "next": step.get("id"),
                    "timezone": self._wait_timezone_name(cfg, step, strategy),
                }
                enqueue_execute_step(
                    self.db,
                    run_id=run.id,
                    step_id=step.get("id") or "",
                    account_id=account.id,
                    strategy_id=strategy.id,
                    scheduled_at=deferred_until,
                    payload={"after_comm_delay": True},
                    commit=False,
                )
                account.next_action = (
                    f"Scheduled communication {deferred_until.isoformat()}"
                )
                return None
            self._send_communication(
                run, account, strategy, step, step_run, review_approved=after_human_review
            )
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            return step.get("next") or cfg.get("next")

        if kind == "Condition":
            report_due = (
                None if ctx or after_comm_delay else self._delivery_report_due_at(account, cfg, step)
            )
            if report_due is not None:
                # Checked again when the report arrives (on_communication_event) or, without
                # one, when the wait is over.
                run.status = PayflowWorkflowRunStatus.WAITING.value
                step_run.status = "scheduled"
                step_run.result_json = {
                    "wait_until": report_due.isoformat(),
                    "awaiting_delivery_report": True,
                    "next": step.get("id"),
                }
                enqueue_execute_step(
                    self.db,
                    run_id=run.id,
                    step_id=step.get("id") or "",
                    account_id=account.id,
                    strategy_id=strategy.id,
                    scheduled_at=report_due,
                    payload={"after_comm_delay": True},
                    commit=False,
                )
                account.next_action = "Waiting for the delivery report"
                return None
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            yes_id = step.get("yes") or cfg.get("yes")
            no_id = step.get("no") or cfg.get("no")
            matched = self._eval_condition(account, cfg, step, ctx, run=run)
            attr = (
                cfg.get("attribute")
                or step.get("attribute")
                or cfg.get("field")
                or step.get("field")
            )
            step_run.result_json = {
                "condition_matched": matched,
                "attribute": attr,
                "operator": cfg.get("operator") or step.get("operator"),
                "value": cfg.get("value", step.get("value")),
                "actual": resolve_condition_actual(
                    self.db, account, str(attr or ""), ctx=ctx
                )
                if attr
                else None,
                "context_keys": list(ctx.keys()),
            }
            return yes_id if matched else no_id

        if kind == "Human Review":
            # Lovable: Review Reason comes from the catalog, not the step title ("Human review").
            from app.modules.payflow.services.review_service import (
                REVIEW_REASONS,
                normalize_review_reason,
            )

            configured = (cfg.get("review_reason") or "").strip()
            reason = (
                configured
                if configured in REVIEW_REASONS
                else normalize_review_reason(configured or cfg.get("note"))
            )
            self._pause_for_human_review(
                run,
                account,
                strategy,
                step,
                step_run,
                rule_id=None,
                reason=reason,
            )
            return None

        if kind == "Payment Action":
            self._apply_payment_action(account, cfg, step, step_run, ctx)
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            return step.get("next") or cfg.get("next")

        if kind == "Case Action":
            next_id = step.get("next") or cfg.get("next")
            action_key = str(cfg.get("action") or step.get("action") or "").strip().lower()
            if action_key in _REVIEW_CASE_ACTIONS:
                if after_human_review:
                    # The supervisor released the hold / decided on the escalation: go on.
                    self._append_timeline(account, "Released after review", str(cfg.get("action") or ""))
                    step_run.result_json = {"action": cfg.get("action"), "released_after_review": True}
                    run.status = PayflowWorkflowRunStatus.ACTIVE.value
                    return next_id
                self._apply_case_action(account, cfg, step, step_run)
                if (steps.get(next_id) or {}).get("kind") == "Human Review":
                    # The next step opens the review itself: one review, not two in a row.
                    run.status = PayflowWorkflowRunStatus.ACTIVE.value
                    return next_id
                from app.modules.payflow.services.review_service import normalize_review_reason

                applied = dict(step_run.result_json or {})
                self._pause_for_human_review(
                    run,
                    account,
                    strategy,
                    step,
                    step_run,
                    rule_id=None,
                    reason=normalize_review_reason(
                        cfg.get("review_reason") or cfg.get("note") or step.get("title")
                    ),
                    proposed_action=_REVIEW_CASE_ACTIONS[action_key],
                )
                step_run.result_json = {**applied, **(step_run.result_json or {})}
                return None
            self._apply_case_action(account, cfg, step, step_run)
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            return next_id

        if kind == "AI Reassessment":
            self._queue_ai_reassessment(account, strategy, step_run)
            run.status = PayflowWorkflowRunStatus.ACTIVE.value
            return step.get("next") or cfg.get("next")

        if kind in {"Outcome", "End"}:
            run.status = PayflowWorkflowRunStatus.COMPLETED.value
            run.completed_at = _utcnow()
            account.next_action = None
            outcome_label = (
                cfg.get("outcome") or step.get("outcome") or step.get("title") or kind
            )
            applied = apply_outcome_to_account(account, str(outcome_label))
            step_run.result_json = {"outcome": outcome_label, **applied}
            if applied.get("applied") is False:
                self.log_event(
                    event_type="outcome_not_applied",
                    message=str(applied.get("reason") or f"Outcome {outcome_label} not applied"),
                    level="warn",
                    run_id=run.id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                    step_id=step.get("id"),
                )
            return None

        run.status = PayflowWorkflowRunStatus.ACTIVE.value
        return step.get("next") or cfg.get("next")

    def _compliance_gate(
        self,
        account: PayflowAccountModel,
        cfg: dict,
        step: dict,
        *,
        review_approved: bool,
    ) -> tuple[str, datetime | None]:
        """Ask the compliance engine before a message is prepared.

        Returns ("ok", None), ("defer", when it is next permitted) or ("review", None).
        A block is "ok" here on purpose: the send itself refuses it and records the
        suppressed message, so the audit trail shows what was not sent and why.
        """
        from app.modules.payflow.services.compliance.evaluation_service import (
            ComplianceEvaluationService,
        )

        service = ComplianceEvaluationService(self.db)
        if not service.has_active_rules(account.client_id):
            return "ok", None
        if is_contact_step(cfg, step):
            channels = ["Email", "SMS"]
        else:
            channels = [(cfg.get("channel") or step.get("channel") or "Email").strip()]
        results = [
            service.evaluate_account(account, channel=channel, record=False) for channel in channels
        ]
        if any(
            r.decision == "ALLOW" or (r.decision == "REVIEW_REQUIRED" and review_approved)
            for r in results
        ):
            return "ok", None
        times = [
            r.next_permitted_at
            for r in results
            if r.decision == "RESCHEDULE" and r.next_permitted_at is not None
        ]
        if times:
            return "defer", min(times)
        # Needs a person: a review was asked for, or no permitted time could be found.
        if any(r.decision in {"REVIEW_REQUIRED", "RESCHEDULE"} for r in results):
            return ("ok", None) if review_approved else ("review", None)
        return "ok", None

    def _send_window_opens_at(self, account: PayflowAccountModel) -> datetime | None:
        """When the client's send window next opens, or None while it is open."""
        from app.modules.payflow.services.communication_service import _evaluate_send_window

        client = account.client
        if client is None and account.client_id:
            client = self.db.get(PayflowClientModel, account.client_id)
        window = _evaluate_send_window(client)
        if window.get("ok"):
            return None
        return window.get("next_eligible_at")

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

    def _close_waits_targeting_step(self, run_id: int, next_step_id: str) -> int:
        """Mark open Wait step-runs done when their scheduled next step starts."""
        rows = (
            self.db.query(PayflowWorkflowStepRunModel)
            .filter(
                PayflowWorkflowStepRunModel.run_id == run_id,
                PayflowWorkflowStepRunModel.kind == "Wait",
                PayflowWorkflowStepRunModel.status == "waiting",
            )
            .all()
        )
        closed = 0
        now = _utcnow()
        for row in rows:
            result = row.result_json if isinstance(row.result_json, dict) else {}
            if result.get("next") != next_step_id:
                continue
            row.status = "done"
            row.finished_at = now
            merged = dict(result)
            merged["closed_reason"] = "wait_elapsed"
            row.result_json = merged
            closed += 1
        return closed

    def _delivery_report_due_at(
        self, account: PayflowAccountModel, cfg: dict, step: dict
    ) -> datetime | None:
        """Until when a delivery Condition waits for the provider's report; None = decide now.

        Only while the channel's latest message is still in transit (Queued / Sent) and was
        sent less than DELIVERY_REPORT_WAIT ago.
        """
        attribute = cfg.get("attribute") or step.get("attribute") or cfg.get("field") or step.get("field")
        channel = _DELIVERY_ATTRIBUTES.get(str(attribute or "").strip().lower())
        if not channel:
            return None
        row = (
            self.db.query(PayflowCommunicationModel)
            .filter(
                PayflowCommunicationModel.account_id == account.id,
                PayflowCommunicationModel.channel == channel,
            )
            .order_by(PayflowCommunicationModel.id.desc())
            .first()
        )
        if row is None or row.status not in _IN_TRANSIT_COMM_STATUSES:
            return None
        sent_at = _aware(row.sent_at or row.created_at)
        if sent_at is None:
            return None
        due = sent_at + DELIVERY_REPORT_WAIT
        return due if due > _utcnow() else None

    def _awaiting_delivery_row(
        self, run_id: int, step_id: str
    ) -> PayflowWorkflowStepRunModel | None:
        """The Condition step-run that is waiting for a delivery report, if there is one."""
        rows = (
            self.db.query(PayflowWorkflowStepRunModel)
            .filter(
                PayflowWorkflowStepRunModel.run_id == run_id,
                PayflowWorkflowStepRunModel.step_id == step_id,
                PayflowWorkflowStepRunModel.kind == "Condition",
                PayflowWorkflowStepRunModel.status == "scheduled",
            )
            .order_by(PayflowWorkflowStepRunModel.id.desc())
            .all()
        )
        return next(
            (
                row
                for row in rows
                if isinstance(row.result_json, dict) and row.result_json.get("awaiting_delivery_report")
            ),
            None,
        )

    def _close_awaiting_delivery(self, run_id: int, step_id: str, *, reason: str) -> bool:
        row = self._awaiting_delivery_row(run_id, step_id)
        if row is None:
            return False
        row.status = "done"
        row.finished_at = _utcnow()
        row.result_json = {**row.result_json, "closed_reason": reason}
        return True

    def _close_deferred_communication(self, run_id: int, step_id: str) -> int:
        """Close Communication rows that were waiting for a timed send."""
        rows = (
            self.db.query(PayflowWorkflowStepRunModel)
            .filter(
                PayflowWorkflowStepRunModel.run_id == run_id,
                PayflowWorkflowStepRunModel.step_id == step_id,
                PayflowWorkflowStepRunModel.kind == "Communication",
                PayflowWorkflowStepRunModel.status.in_(["scheduled", "waiting"]),
            )
            .all()
        )
        closed = 0
        now = _utcnow()
        for row in rows:
            result = row.result_json if isinstance(row.result_json, dict) else {}
            if not result.get("deferred_send"):
                continue
            row.status = "done"
            row.finished_at = now
            merged = dict(result)
            merged["closed_reason"] = "comm_delay_elapsed"
            row.result_json = merged
            closed += 1
        return closed

    def _wait_until(
        self,
        cfg: dict,
        step: dict,
        strategy: PayflowStrategyModel,
        *,
        account: PayflowAccountModel | None = None,
        run: PayflowWorkflowRunModel | None = None,
    ) -> datetime:
        amount = cfg.get("amount")
        if amount is None:
            amount = step.get("amount", 3)
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            amount = 3
        amount = max(amount, 0)
        unit = cfg.get("unit") or step.get("unit") or "Days"
        direction = (cfg.get("direction") or step.get("direction") or "After").strip().lower()
        reference_event = cfg.get("reference_event") or step.get("reference_event")
        tz_name = self._wait_timezone_name(cfg, step, strategy)
        try:
            tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001
            tz = timezone.utc
        now_utc = _utcnow()
        delta = timedelta_for_unit(amount, str(unit))

        reference = str(reference_event or "").strip().lower()
        started_at = _aware(getattr(run, "started_at", None)) if run else None
        anchor = None
        if account is not None:
            anchor = resolve_reference_anchor(
                self.db,
                account,
                str(reference_event) if reference_event else None,
                run_started_at=started_at,
                previous_action_at=(
                    self._previous_action_at(run) if run and reference == "previous action" else None
                ),
            )
        if anchor is None:
            anchor = now_utc
        elif (
            reference in FIXED_DATE_REFERENCE_EVENTS
            and not direction.startswith("before")
            and started_at is not None
            and anchor < started_at
            and self._run_has_sent_message(run)
        ):
            # "7 days after Case Received" on an account received months ago is already over, so
            # every later message would follow the first one within seconds. Once the run has
            # sent a message, such a date counts from the run's start: the workflow keeps its
            # spacing. The first message is not affected (an overdue account gets it at once).
            anchor = started_at

        if direction.startswith("before"):
            when = anchor - delta
        else:
            when = anchor + delta

        # Never schedule in the past — fire as soon as the worker can.
        if when < now_utc:
            when = now_utc
        # Convert through client timezone for day-boundary consistency, then UTC.
        return when.astimezone(tz).astimezone(timezone.utc)

    def _previous_action_at(self, run: PayflowWorkflowRunModel) -> datetime | None:
        """When the run's previous step finished - the anchor of "Previous Action"."""
        if run is None or run.id is None:
            return None
        row = (
            self.db.query(PayflowWorkflowStepRunModel.finished_at)
            .filter(
                PayflowWorkflowStepRunModel.run_id == run.id,
                PayflowWorkflowStepRunModel.status == "done",
                PayflowWorkflowStepRunModel.finished_at.isnot(None),
            )
            .order_by(PayflowWorkflowStepRunModel.finished_at.desc())
            .first()
        )
        return _aware(row[0]) if row else None

    def _run_has_sent_message(self, run: PayflowWorkflowRunModel | None) -> bool:
        if run is None or run.id is None:
            return False
        return (
            self.db.query(PayflowWorkflowStepRunModel.id)
            .filter(
                PayflowWorkflowStepRunModel.run_id == run.id,
                PayflowWorkflowStepRunModel.kind == "Communication",
                PayflowWorkflowStepRunModel.status == "done",
            )
            .first()
            is not None
        )

    def _communication_defer_until(
        self,
        cfg: dict,
        step: dict,
        strategy: PayflowStrategyModel,
        *,
        account: PayflowAccountModel,
        run: PayflowWorkflowRunModel,
    ) -> datetime | None:
        """If Communication has a meaningful delay, return when to send; else None."""
        amount = cfg.get("amount")
        if amount is None:
            amount = step.get("amount", 0)
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            return None
        when = self._wait_until(cfg, step, strategy, account=account, run=run)
        # Ignore tiny "now" schedules (amount resolved against a past/now anchor).
        if when <= _utcnow() + timedelta(seconds=2):
            return None
        return when

    def _eval_condition(
        self,
        account: PayflowAccountModel,
        cfg: dict,
        step: dict,
        ctx: dict[str, Any],
        run: PayflowWorkflowRunModel | None = None,
    ) -> bool:
        """`run`: the run checking it — "Payment Status" counts payments made since it started."""
        # AI / FE catalog uses `attribute`; older steps may still use `field`.
        attribute = (
            cfg.get("attribute")
            or step.get("attribute")
            or cfg.get("field")
            or step.get("field")
            or ""
        )
        attribute = str(attribute).strip()
        operator = cfg.get("operator") or step.get("operator") or "Equals"
        expected = cfg.get("value", step.get("value"))

        if not attribute:
            # Default stop-when-paid/plan heuristic aligned with Payment Status catalog.
            status = resolve_condition_actual(
                self.db, account, "Payment Status", ctx=ctx, run=run
            )
            return status in {"Paid In Full", "Payment Plan Active", "Partial Payment"}

        actual = resolve_condition_actual(self.db, account, attribute, ctx=ctx, run=run)
        return compare_values(actual, str(operator), expected)

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
        action_key = str(action).strip().lower()
        status_hint = cfg.get("collection_status") or step.get("collection_status")
        if status_hint:
            account.collection_status = str(status_hint)
        elif account.last_payment_is_ptp or action_key == PAYMENT_ACTION_RECORD_PTP:
            # "Record Promise To Pay": the case is now a promise (same as Outcome "Promise To Pay Recorded").
            account.collection_status = PayflowCollectionStatus.PROMISE_TO_PAY.value
        # "Send Secure Payment Link": make sure the customer's payment page exists. The message that
        # carries the link is a Communication step - it alone passes the compliance checks.
        link_ready = bool(self._payment_link(account)) if action_key == PAYMENT_ACTION_SEND_LINK else None
        plan_offer = self._offer_payment_plan(account) if action_key == PAYMENT_ACTION_OFFER_PLAN else None
        detail = f"{action}"
        if account.last_payment_amount is not None:
            detail = f"{action} · {account.last_payment_amount}"
        if plan_offer is not None:
            detail = f"{action} · {plan_offer.replace('_', ' ')}"
        self._append_timeline(account, action, detail)
        account.last_action = action
        step_run.result_json = {
            "action": action,
            **({"plan_offer": plan_offer} if plan_offer is not None else {}),
            "amount": account.last_payment_amount,
            "payment_date": account.last_payment_date.isoformat()
            if account.last_payment_date
            else None,
            "is_ptp": account.last_payment_is_ptp,
            "collection_status": account.collection_status,
            **({"payment_link_ready": link_ready} if link_ready is not None else {}),
        }

    def _offer_payment_plan(self, account: PayflowAccountModel) -> str:
        """ "Offer Payment Plan": make the offer real, or say why it cannot be made.

        A plan is set up by the customer on their payment page, so the offer exists when the
        client's page allows plans and the customer's page is ready. The message that tells the
        customer about it is a Communication step ("Payment Plan Reminder") - it alone passes
        the compliance checks. When no plan can be offered a person looks at the case, instead
        of the timeline claiming an offer that was never possible.
        """
        from app.modules.payflow.services.workflow_condition_runtime import _current_plan
        from app.modules.payflow.services.workflow_suggestion.context import load_payment_options

        if _current_plan(self.db, account.id) is not None:
            return PLAN_OFFER_ALREADY_ON_PLAN
        try:
            allowed = "set up a payment plan" in load_payment_options(self.db, account.client_id)
        except Exception:  # noqa: BLE001
            logger.exception("could not read payment options for client_id=%s", account.client_id)
            allowed = False
        if not allowed:
            outcome = PLAN_OFFER_NOT_ALLOWED
        elif not self._payment_link(account):
            outcome = PLAN_OFFER_NO_PAGE
        else:
            return PLAN_OFFER_AVAILABLE
        account.human_review = True
        account.next_action = PLAN_OFFER_NOT_POSSIBLE
        self.log_event(
            event_type="plan_offer_not_possible",
            message=f"Offer Payment Plan: {outcome.replace('_', ' ')}",
            level="warn",
            account_id=account.id,
            client_id=account.client_id,
        )
        return outcome

    def _apply_case_action(
        self,
        account: PayflowAccountModel,
        cfg: dict,
        step: dict,
        step_run: PayflowWorkflowStepRunModel,
    ) -> None:
        action = cfg.get("action") or step.get("action") or "Case action"
        action_key = str(action).strip().lower()
        if cfg.get("collection_status") or step.get("collection_status"):
            account.collection_status = str(
                cfg.get("collection_status") or step.get("collection_status")
            )
        elif action_key == CASE_ACTION_CLOSE:
            # Same as Outcome "Case Closed": no further collection action on this case.
            account.collection_status = PayflowCollectionStatus.RESOLVED.value
            account.next_action = None
        elif action_key in _REVIEW_CASE_ACTIONS:
            # Held / escalated: a person decides. Other workflows do not enrol the account meanwhile.
            account.human_review = True
            account.collection_status = PayflowCollectionStatus.HUMAN_REVIEW.value
        elif action_key == CASE_ACTION_CONTACT_UPDATE:
            # Flagged for staff (the case's review flag + its timeline); the workflow itself goes on.
            account.human_review = True
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
        """Case-level AI recommendation (not portfolio Suggest job)."""
        result: dict[str, Any] = {}
        try:
            from app.modules.payflow.services.case_reassessment_service import (
                CaseReassessmentService,
            )

            result = CaseReassessmentService(self.db).run_reassessment(
                account,
                strategy,
                trigger="workflow_ai_reassessment",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI case reassessment failed: %s", exc)
            account.next_action = "AI reassessment failed"
            result = {"error": str(exc)}
        step_run.result_json = {
            "queued": False,
            "case_reassessment": True,
            **result,
        }
        rec = (result.get("recommendation") or {}) if isinstance(result, dict) else {}
        self._append_timeline(
            account,
            "AI reassessment",
            str(rec.get("action") or result.get("error") or "completed"),
        )

    def _send_communication(
        self,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        step: dict,
        step_run: PayflowWorkflowStepRunModel,
        *,
        review_approved: bool = False,
    ) -> None:
        cfg = _cfg(step)
        purpose = cfg.get("purpose") or step.get("purpose") or "Payment Reminder"
        payment_link = self._payment_link(account)

        # AI contact steps: no fixed channel — pick Email/SMS, failover, flag unreachable.
        if is_contact_step(cfg, step):
            self._send_contact_communication(
                run,
                account,
                strategy,
                step,
                step_run,
                purpose=str(purpose),
                payment_link=payment_link,
                review_approved=review_approved,
            )
            return

        channel = (cfg.get("channel") or step.get("channel") or "Email").strip()
        # The text written for the customer's audience when the step has one (else its main text).
        cfg, audience = audience_message_config(cfg, account)
        subject, body = self._render_channel_message(
            cfg, step, channel=channel, purpose=str(purpose), account=account, strategy=strategy
        )
        result = PayflowCommunicationService(self.db).send_workflow_step(
            account=account,
            strategy=strategy,
            channel=channel,
            purpose=purpose,
            subject=subject,
            body=body,
            payment_link=payment_link,
            run_id=run.id,
            client_id=run.client_id,
            compliance_review_approved=review_approved,
            tracking=self._message_tracking(
                run, account, strategy, step, channel=channel, purpose=str(purpose)
            ),
            message_audience=audience,
        )
        self._record_send_result(run, account, strategy, step_run, channel=channel, result=result)

    def _send_contact_communication(
        self,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        step: dict,
        step_run: PayflowWorkflowStepRunModel,
        *,
        purpose: str,
        payment_link: str,
        review_approved: bool = False,
    ) -> None:
        # The text written for the customer's audience when the step has one (else its main text).
        cfg, audience = audience_message_config(_cfg(step), account)
        channels = preferred_contact_channels(self.db, account)
        attempts: list[dict[str, Any]] = []
        chosen: str | None = None
        final: dict[str, Any] = {}

        for channel in channels:
            subject, body = self._render_channel_message(
                cfg,
                step,
                channel=channel,
                purpose=purpose,
                account=account,
                strategy=strategy,
            )
            result = PayflowCommunicationService(self.db).send_workflow_step(
                account=account,
                strategy=strategy,
                channel=channel,
                purpose=purpose,
                subject=subject,
                body=body,
                payment_link=payment_link,
                run_id=run.id,
                client_id=run.client_id,
                compliance_review_approved=review_approved,
                tracking=self._message_tracking(
                    run, account, strategy, step, channel=channel, purpose=purpose
                ),
                message_audience=audience,
            )
            attempt = {**result, "attempt_channel": channel}
            attempts.append(attempt)
            if send_succeeded(result):
                chosen = channel
                final = result
                break
            # Log the failed/skipped attempt, then failover to the next channel.
            self._record_send_result(
                run,
                account,
                strategy,
                step_run,
                channel=channel,
                result=result,
                persist_result=False,
            )
            if len(channels) > 1 and channel != channels[-1]:
                self.log_event(
                    event_type="contact_failover",
                    message=(
                        f"Contact step failover: {channel} did not send "
                        f"({result.get('skipped') or result.get('provider_error') or result.get('status')})"
                        f" — trying next channel"
                    ),
                    level="warn",
                    run_id=run.id,
                    account_id=account.id,
                    strategy_id=strategy.id,
                    step_id=step.get("id"),
                )

        if chosen:
            merged = {
                **final,
                "chosen_channel": chosen,
                "contact_step": True,
                "contact_attempts": [
                    {
                        "channel": a.get("attempt_channel"),
                        "status": a.get("status"),
                        "skipped": a.get("skipped"),
                        "provider_error": a.get("provider_error"),
                        "restriction_reason": a.get("restriction_reason"),
                        "code": a.get("code"),
                    }
                    for a in attempts
                ],
            }
            self._record_send_result(
                run, account, strategy, step_run, channel=chosen, result=merged
            )
            return

        # Nothing reached the customer.
        unreachable = should_flag_unreachable(attempts)
        flagged = flag_unreachable(account) if unreachable else {}
        step_run.result_json = {
            "skipped": "unreachable" if unreachable else "all_channels_blocked",
            "contact_step": True,
            "contact_attempts": attempts,
            "flagged_unreachable": unreachable,
            **flagged,
        }
        self.log_event(
            event_type="contact_unreachable" if unreachable else "contact_blocked",
            message=(
                "Contact step could not reach customer on Email or SMS"
                if unreachable
                else "Contact step blocked on all channels (temporary / governance)"
            ),
            level="warn",
            run_id=run.id,
            account_id=account.id,
            strategy_id=strategy.id,
            step_id=step.get("id"),
        )
        if unreachable:
            self._append_timeline(
                account,
                "No contactable channel",
                "Email and SMS both unavailable for this customer",
            )

    def _render_channel_message(
        self,
        cfg: dict[str, Any],
        step: dict,
        *,
        channel: str,
        purpose: str,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
    ) -> tuple[str, str]:
        """Build rendered subject/body for a fixed channel or contact-step channel pick."""
        subject, body = self._channel_message_source(
            cfg, step, channel=channel, purpose=purpose, account=account
        )
        return (
            self._render(subject, account, strategy),
            self._render(body, account, strategy),
        )

    def _channel_message_source(
        self,
        cfg: dict[str, Any],
        step: dict,
        *,
        channel: str,
        purpose: str,
        account: PayflowAccountModel,
    ) -> tuple[str, str]:
        """The subject/body to send, before the customer's values are filled in ({{...}} kept)."""
        # The step's text (or its template) in the customer's language when there is a version for it.
        language = getattr(account, "language", None)
        cfg = localized_message_config(cfg, language)
        if is_contact_step(cfg, step):
            subject, body = message_for_channel(cfg, channel=channel, purpose=purpose, language=language)
        else:
            template_id = cfg.get("template_id") or step.get("template_id")
            template = next((t for t in MESSAGE_TEMPLATES if t.get("id") == template_id), None)
            if template is None:
                # Purpose-matched fallback, then generic reminder.
                from app.modules.payflow.services.workflow_contact_runtime import (
                    template_for,
                )

                template = template_for(purpose, channel)
            template = localized_template(template, language)
            if channel.strip().lower() == "sms":
                subject, body = "", (cfg.get("body") or (template or {}).get("body") or "")
            else:
                subject = cfg.get("subject") or (template or {}).get("subject") or purpose
                body = cfg.get("body") or (template or {}).get("body") or ""
        return str(subject or ""), str(body or "")

    def _message_tracking(
        self,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        step: dict,
        *,
        channel: str,
        purpose: str,
    ) -> dict[str, Any] | None:
        """What is recorded on the communication for subject performance (see email_tracking)."""
        try:
            from app.modules.payflow.services.email_tracking import (
                ORIGIN_TEMPLATE,
                reminder_stage,
            )

            cfg = _cfg(step)
            subject, body = self._channel_message_source(
                cfg, step, channel=channel, purpose=purpose, account=account
            )
            own_text = bool(cfg.get("body") or cfg.get("sms_body") or cfg.get("subject"))
            return {
                "subject_template": subject,
                "body_template": body,
                "step_id": step.get("id"),
                "strategy_version": int(getattr(strategy, "version", None) or 1),
                "run_id": run.id,
                "portfolio_id": run.portfolio_id or getattr(account, "portfolio_id", None),
                "reminder_stage": reminder_stage(list(strategy.steps or []), step, purpose),
                "template_id": None if own_text else (cfg.get("template_id") or step.get("template_id")),
                "origin": (cfg.get("content_origin") or "Human Created") if own_text else ORIGIN_TEMPLATE,
            }
        except Exception:  # noqa: BLE001 - tracking never blocks a send
            logger.exception("failed to build message tracking for run_id=%s", run.id)
            return None

    def _record_send_result(
        self,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        step_run: PayflowWorkflowStepRunModel,
        *,
        channel: str,
        result: dict[str, Any],
        persist_result: bool = True,
    ) -> None:
        if persist_result:
            step_run.result_json = result
        if result.get("skipped") and not result.get("chosen_channel"):
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
        if result.get("provider_error") and not send_succeeded(result):
            self._notify_provider_failed(
                run, channel, str(result.get("provider_error"))
            )
        elif send_succeeded(result):
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
        client = account.client
        from app.modules.payflow.services.communication_service import (
            _is_third_party_client,
            _resolve_collection_from,
        )

        if _is_third_party_client(client):
            brand_name = (
                (settings.PAYFLOW_OPERATOR_BRAND_NAME or "").strip()
                or (settings.SENDGRID_FROM_NAME or "").strip()
                or "PayFlow Collections"
            )
        else:
            _, rendered_name = _resolve_collection_from(client)
            brand_name = rendered_name or (settings.SENDGRID_FROM_NAME or "PayFlow")
        mapping = {
            "first_name": first or "Customer",
            "outstanding_balance": f"${float(account.outstanding_balance or 0):,.2f}",
            "account_reference": account.account_reference or "",
            "payment_link": self._payment_link(account),
            "brand_name": brand_name,
            "promise_amount": f"${float(account.ptp_amount or 0):,.2f}",
            "promise_date": str(account.ptp_due_date or ""),
        }

        def repl(match: re.Match) -> str:
            key = match.group(1).strip()
            return str(mapping.get(key, match.group(0)))

        return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", repl, text)

    def _payment_link(self, account: PayflowAccountModel) -> str:
        """Opaque tokenized URL — never exposes CRM account_reference in the path."""
        try:
            from app.modules.payflow.services.payment_link_service import (
                PayflowPaymentLinkService,
            )

            svc = PayflowPaymentLinkService(self.db)
            link = svc.ensure_active_link(account, commit=False)
            return svc.public_url(link)
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to ensure payment link for account_id=%s", getattr(account, "id", None)
            )
            return ""

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
            from app.modules.payflow.services.operational_exception_service import (
                PayflowOperationalExceptionService,
            )
            from app.shared.enums import PayflowOperationalExceptionType

            PayflowOperationalExceptionService(self.db).raise_exception(
                exception_type=PayflowOperationalExceptionType.WORKFLOW_EXECUTION_FAILURE.value,
                reason=message or "Workflow step failed",
                client_id=run.client_id,
                account_id=run.account_id,
                source_entity_type="workflow_run",
                source_entity_id=run.id,
                severity="High",
                retriable=True,
                diagnostic_ref=f"run:{run.id}:step:{getattr(run, 'current_step_id', None) or run.status}",
                link=f"/payflow/cases/{run.account_id}",
                notify=False,
                commit=False,
            )
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
            from app.modules.payflow.services.operational_exception_service import (
                PayflowOperationalExceptionService,
            )
            from app.shared.enums import PayflowOperationalExceptionType

            PayflowOperationalExceptionService(self.db).raise_exception(
                exception_type=PayflowOperationalExceptionType.COMMUNICATION_FAILURE.value,
                reason=f"{channel} send failed: {(detail or '')[:300]}",
                client_id=run.client_id,
                account_id=run.account_id,
                source_entity_type="workflow_run",
                source_entity_id=run.id,
                severity="High",
                retriable=True,
                diagnostic_ref=f"run:{run.id}:{channel}",
                link=f"/payflow/cases/{run.account_id}",
                notify=False,
                commit=False,
            )
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

            PayflowNotificationService(self.db).notify_comm_sent(
                client_id=run.client_id,
                account_id=account.id,
                customer_name=account.customer_name or "Customer",
                account_reference=account.account_reference or str(account.id),
                channel=channel,
                recipient=recipient,
                communication_id=None,
                communication_code=comm_code,
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
        allowed_client_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        q = self.db.query(PayflowWorkflowWorkerLogModel)
        if allowed_client_ids is not None:
            if not allowed_client_ids:
                return []
            q = q.filter(PayflowWorkflowWorkerLogModel.client_id.in_(allowed_client_ids))
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
        allowed_client_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        q = self.db.query(PayflowWorkflowRunModel)
        if allowed_client_ids is not None:
            if not allowed_client_ids:
                return []
            q = q.filter(PayflowWorkflowRunModel.client_id.in_(allowed_client_ids))
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
        # Timed Wait: show Waiting until wait_until; once elapsed (or closed),
        # show Completed even if the row was left as status=waiting.
        kind = (s.kind or step_def.get("kind") or "").strip()
        if kind == "Wait" and isinstance(s.result_json, dict):
            raw_until = s.result_json.get("wait_until")
            if raw_until:
                try:
                    until = datetime.fromisoformat(str(raw_until).replace("Z", "+00:00"))
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                    status_l = (s.status or "").lower()
                    if until > _utcnow() and status_l in {"waiting", "running", "done", "skipped"}:
                        ui_status = "Waiting"
                    elif status_l == "waiting":
                        ui_status = "Completed"
                except ValueError:
                    pass
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
            for s in (self._run_steps(run, strategy) if strategy else [])
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
        # Latest activity first (case timeline / live execution).
        activity.sort(
            key=lambda a: a["at"] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
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
        status_raw = str(payload.get("status") or payload.get("result") or "").lower()
        if status_raw in {"failed", "failure", "error", "declined"}:
            try:
                from app.modules.payflow.services.operational_exception_service import (
                    PayflowOperationalExceptionService,
                )
                from app.shared.enums import PayflowOperationalExceptionType

                PayflowOperationalExceptionService(self.db).raise_exception(
                    exception_type=PayflowOperationalExceptionType.PAYMENT_PROCESSING_ISSUE.value,
                    reason=str(
                        payload.get("reason")
                        or payload.get("error")
                        or "Payment processing failed"
                    )[:500],
                    client_id=account.client_id,
                    account_id=account_id,
                    source_entity_type="payment",
                    source_entity_id=str(payload.get("payment_id") or account_id),
                    severity="High",
                    retriable=True,
                    link=f"/payflow/cases/{account_id}",
                    commit=commit,
                )
            except Exception:  # noqa: BLE001
                logger.exception("payment failure exception raise failed")
            return {"advanced": 0, "exception": "payment_processing_issue"}
        if status_raw in {"plan_missed", "auto_charge_paused"}:
            # A missed instalment is the opposite of a payment: let conditions and
            # held runs react, but record no money and credit no message with it.
            ctx = {**payload, "has_payment": False, "payment_received": False, "plan_missed": True}
            advanced = 0
            for run in self._open_runs_for_account(account_id):
                if self._advance_run_from_event(run, event="payment", context=ctx):
                    advanced += 1
                self.log_event(
                    event_type="payment_event",
                    message="Payment plan instalment missed — workflow reassessed",
                    level="warn",
                    run_id=run.id,
                    account_id=account_id,
                    strategy_id=run.strategy_id,
                )
            if commit:
                self.db.commit()
            return {"advanced": advanced, "plan_missed": True}
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
                # The message the payment is credited to, when the caller knows it.
                communication_id=payload.get("communication_id"),
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
            elif block and run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value:
                # Still the supervisor's call. Their decision resumes the run, and the
                # resumed step then stops on the hold until it is released.
                run.last_error = block
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
            for s in self._run_steps(run, strategy)
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
            if self._awaiting_delivery_row(run.id, run.current_step_id) is not None:
                # Waiting for a delivery report: only the report itself (Delivered / Failed /
                # Bounced for that channel) decides now. Anything else - "Sent", a payment, a
                # status change - leaves it to the scheduled check, so the branch is never
                # taken twice.
                attribute = cfg.get("attribute") or step.get("attribute") or ""
                channel = _DELIVERY_ATTRIBUTES.get(str(attribute).strip().lower())
                if (
                    event != "communication"
                    or not channel
                    or str(context.get("channel") or "").strip().lower() != channel.lower()
                    or channel_delivery(self.db, account, channel, context) is None
                ):
                    return False
                cancel_pending_jobs_for_run(self.db, run.id)  # the scheduled check
                self._close_awaiting_delivery(
                    run.id, run.current_step_id, reason="delivery_report_received"
                )
            matched = self._eval_condition(
                account,
                cfg,
                step,
                context,
                run=run,
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

        # Timed Wait steps must honor wait_until. Communication webhooks,
        # delivery events, and payment/status updates must NOT skip them —
        # otherwise "Wait 3 days" advances immediately after Email/SMS send.
        # Advancement happens only via the scheduled EXECUTE_STEP job.
        if kind == "Wait":
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

        # Non-Wait WAITING (e.g. account hold/blocked): payment or case-status
        # release may resume. Communication events must not auto-resume.
        if run.status == PayflowWorkflowRunStatus.WAITING.value and event in {
            "payment",
            "case_status",
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

    def on_human_review_decided(
        self,
        review_id: int,
        *,
        continue_workflow: bool,
        stop_reason: str | None = None,
    ) -> None:
        run = (
            self.db.query(PayflowWorkflowRunModel)
            .filter(
                PayflowWorkflowRunModel.human_review_id == review_id,
                PayflowWorkflowRunModel.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value,
            )
            .first()
        )
        if run:
            if continue_workflow:
                enqueue_resume_run(
                    self.db,
                    run_id=run.id,
                    step_id=run.current_step_id,
                    account_id=run.account_id,
                    payload={"after_human_review": True},
                    commit=False,
                )
                self.log_event(
                    event_type="human_review_continue",
                    message=f"Resume queued after review #{review_id} (re-execute paused step)",
                    run_id=run.id,
                    account_id=run.account_id,
                    strategy_id=run.strategy_id,
                )
            else:
                run.status = PayflowWorkflowRunStatus.CANCELLED.value
                run.completed_at = _utcnow()
                # Marker read by enroll: this workflow version is done for the account.
                run.last_error = (stop_reason or REVIEW_REJECTED_REASON)[:1000]
                run.human_review_id = None
                cancel_pending_jobs_for_run(self.db, run.id)
                self.log_event(
                    event_type="human_review_cancelled_run",
                    message=f"Run cancelled after review #{review_id}",
                    run_id=run.id,
                    account_id=run.account_id,
                    strategy_id=run.strategy_id,
                )
            self.db.commit()
            return

        # Governance-only HR (no paused run): after approve/modify, gap-fill enroll
        # into any Active strategies for this account's scope (same job as import).
        if not continue_workflow:
            return
        review = self.db.get(PayflowHumanReviewModel, review_id)
        if not review or not review.account_id:
            return
        account = self.db.get(PayflowAccountModel, review.account_id)
        if not account:
            return
        portfolio_pairs: set[tuple[int, int]] = set()
        parent_client_ids: set[int] = set()
        if account.portfolio_id is not None:
            portfolio_pairs.add((account.client_id, account.portfolio_id))
        else:
            parent_client_ids.add(account.client_id)
        jobs = self.enqueue_enrolls_for_scopes(
            portfolio_pairs=portfolio_pairs,
            parent_client_ids=parent_client_ids,
            commit=False,
        )
        if jobs:
            self.log_event(
                event_type="enroll_queued",
                message=(
                    f"Enroll queued after human review #{review_id} "
                    f"(no paused run; {len(jobs)} active strateg"
                    f"{'y' if len(jobs) == 1 else 'ies'})"
                ),
                account_id=account.id,
                client_id=account.client_id,
                portfolio_id=account.portfolio_id,
                commit=False,
            )
        self.db.commit()
