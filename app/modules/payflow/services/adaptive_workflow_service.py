"""Adaptive workflow reassessment engine (B4–B6).

Queues ``reassess_run`` jobs, applies safety / NBA decisions, supersedes prior
plan changes, supports pre-send ``message_due`` reassessment, and Ops approval
APIs (approve / modify / reject).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowAdaptiveDecisionModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowEngagementEventModel,
    PayflowNotificationModel,
    PayflowStrategyModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
    PayflowWorkflowStepRunModel,
    UserModel,
)
from app.modules.payflow.services.strategy_service import (
    follow_up_for,
    follow_up_level,
    is_follow_up_step,
)
from app.modules.payflow.services.workflow_condition_runtime import payment_status
from app.modules.payflow.services.workflow_contact_runtime import audience_message_config, localized_message_config
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.modules.payflow.services.workflow_queue import (
    cancel_pending_jobs_for_run,
    enqueue_reassess_run,
)
from app.modules.payflow.services.workflow_suggestion.adaptive_follow_ups import has_text
from app.modules.payflow.services.workflow_suggestion.audiences import audience_label
from app.modules.payflow.services.workflow_suggestion.next_action import (
    NOT_SENT,
    decide_next_action,
    messages_after,
)
from app.shared.enums import PayflowWorkflowJobStatus, PayflowWorkflowJobType, PayflowWorkflowRunStatus

logger = logging.getLogger("app.payflow.adaptive")

_TERMINAL = {
    PayflowWorkflowRunStatus.COMPLETED.value,
    PayflowWorkflowRunStatus.CANCELLED.value,
    PayflowWorkflowRunStatus.FAILED.value,
    PayflowWorkflowRunStatus.SUPERSEDED.value,
}

_IMPORTANT_EVENT_TYPES = frozenset(
    {
        "payment_started",
        "payment_completed",
        "payment_partial",
        "payment_failed",
        "payment_abandoned",
        "plan_created",
        "plan_instalment_missed",
        "email_unsubscribed",
        "sms_opted_out",
        "email_bounced",
        "sms_failed",
    }
)

_ALLOWED_ACTIONS = frozenset(
    {
        "keep_plan",
        "use_follow_up",
        "drop_follow_up",  # rules only: the customer answered a lined-up follow-up's situation
        "move_next",
        "switch_channel",
        "recheck_at",
        "pause",
        "to_operations",
        "stop",
    }
)

_APPLIED_DECISION_STATUSES = frozenset({"executed", "approved", "modified"})
# Decisions that change the live plan (supersede earlier unfinished ones).
_PLAN_CHANGING_ACTIONS = frozenset(
    {
        "use_follow_up",
        "move_next",
        "switch_channel",
        "pause",
        "to_operations",
        "stop",
    }
)

_OPEN_DECISION_STATUSES = frozenset(
    {
        "awaiting_approval",
        "approved",
        "modified",
        "executed",
    }
)

_ENGAGEMENT_STATE_BY_EVENT = {
    "email_not_opened": "Not opened",
    "no_activity": "No activity",
    "email_opened": "Opened",
    "opened_not_clicked": "Opened",
    "email_clicked": "Opened",
    "payment_link_clicked": "Clicked",
    "payment_page_opened": "Clicked",
    "clicked_not_paid": "Clicked",
    "payment_started": "Payment started",
    "payment_partial": "Paid part",
    "payment_completed": "Paid",
    "email_unsubscribed": "Unsubscribed",
    "sms_opted_out": "Unsubscribed",
    "email_bounced": "Unsubscribed",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """A stored time with its zone (a database without time zones returns it without one)."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _cfg(step: dict[str, Any]) -> dict[str, Any]:
    cfg = step.get("config")
    return dict(cfg) if isinstance(cfg, dict) else {}


class AdaptiveWorkflowService:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------ queue

    def queue_reassess_for_event(self, event: PayflowEngagementEventModel) -> PayflowWorkflowJobModel | None:
        """Called from engagement recording. Delivered events never wake the run."""
        if not event or not event.run_id:
            return None
        if event.event_type in {"email_delivered", "sms_delivered"}:
            return None

        run = self.db.get(PayflowWorkflowRunModel, event.run_id)
        if not run or run.status in _TERMINAL:
            return None
        strategy = self.db.get(PayflowStrategyModel, run.strategy_id)
        if not strategy or not bool(getattr(strategy, "adaptive", True)):
            return None

        immediate = (
            event.event_type in _IMPORTANT_EVENT_TYPES
            or event.event_type == "reassess_requested"
        )
        when = _utcnow() if immediate else _utcnow() + timedelta(minutes=5)
        trigger = "manual" if event.event_type == "reassess_requested" else (
            "timer" if (event.source or "") == "timer" else "event"
        )
        return enqueue_reassess_run(
            self.db,
            run_id=run.id,
            account_id=run.account_id,
            strategy_id=run.strategy_id,
            scheduled_at=when,
            payload={
                "trigger": trigger,
                "trigger_event_id": event.id,
                "event_type": event.event_type,
            },
            commit=False,
        )

    def queue_manual_reassess(self, run_id: int) -> PayflowWorkflowJobModel | None:
        """Manual 'Reassess plan' (B6 API). Records reassess_requested (queues immediately)."""
        run = self.db.get(PayflowWorkflowRunModel, run_id)
        if not run or run.status in _TERMINAL:
            return None
        strategy = self.db.get(PayflowStrategyModel, run.strategy_id)
        if not strategy or not bool(getattr(strategy, "adaptive", True)):
            return None
        from app.modules.payflow.services.engagement_service import record_engagement_event

        # record_engagement_event → queue_reassess_for_event (immediate for this type).
        record_engagement_event(
            self.db,
            account_id=run.account_id,
            client_id=run.client_id,
            event_type="reassess_requested",
            source="manual",
            dedupe_key=f"manual:reassess_requested:run:{run.id}:{_utcnow().strftime('%Y%m%d%H%M%S')}",
            run_id=run.id,
            strategy_id=run.strategy_id,
        )
        return (
            self.db.query(PayflowWorkflowJobModel)
            .filter(
                PayflowWorkflowJobModel.run_id == run.id,
                PayflowWorkflowJobModel.job_type == PayflowWorkflowJobType.REASSESS_RUN.value,
                PayflowWorkflowJobModel.status == PayflowWorkflowJobStatus.PENDING.value,
            )
            .order_by(PayflowWorkflowJobModel.id.desc())
            .first()
        )

    # ------------------------------------------------------------------ reassess

    def reassess(
        self,
        run_id: int,
        *,
        job_id: int | None = None,
        trigger: str | None = None,
        due_step_id: str | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Lock the run, apply safety / NBA, mark events processed.

        ``trigger="message_due"`` is used from ``execute_step`` before a send (B5).
        """
        run = (
            self.db.query(PayflowWorkflowRunModel)
            .filter(PayflowWorkflowRunModel.id == run_id)
            .with_for_update()
            .first()
        )
        if not run or run.status in _TERMINAL:
            return {"skipped": True, "reason": "run_missing_or_terminal", "abort_send": True}

        strategy = self.db.get(PayflowStrategyModel, run.strategy_id)
        account = self.db.get(PayflowAccountModel, run.account_id)
        if not strategy or not account:
            return {"skipped": True, "reason": "missing_strategy_or_account", "abort_send": True}
        if not bool(getattr(strategy, "adaptive", True)):
            return {"skipped": True, "reason": "not_adaptive", "abort_send": False}

        job = self.db.get(PayflowWorkflowJobModel, job_id) if job_id else None
        if job and self._job_decision_superseded(job):
            if commit:
                self.db.commit()
            return {
                "skipped": True,
                "reason": "decision_superseded",
                "abort_send": True,
            }

        unread = (
            self.db.query(PayflowEngagementEventModel)
            .filter(
                PayflowEngagementEventModel.run_id == run.id,
                PayflowEngagementEventModel.processed_at.is_(None),
            )
            .order_by(PayflowEngagementEventModel.occurred_at.asc(), PayflowEngagementEventModel.id.asc())
            .all()
        )

        resolved_trigger = (trigger or "").strip()
        if not resolved_trigger:
            resolved_trigger = str((job.payload or {}).get("trigger") or "event") if job else "event"
        if resolved_trigger not in {"event", "timer", "message_due", "manual"}:
            resolved_trigger = "event"

        # Paused for human review: only mark events read.
        if run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value:
            self._mark_processed(unread)
            self._refresh_engagement_state(run, unread)
            run.last_reassessed_at = _utcnow()
            if commit:
                self.db.commit()
            return {
                "skipped": True,
                "reason": "paused_human_review",
                "events_marked": len(unread),
                "abort_send": True,
            }

        exec_svc = WorkflowExecutionService(self.db)
        steps = list(exec_svc._run_steps(run, strategy))
        context = self._build_context(
            run=run,
            account=account,
            strategy=strategy,
            steps=steps,
            new_events=unread,
            trigger=resolved_trigger,
            job=job,
            due_step_id=due_step_id,
        )

        decision = self._safety_decision(run, account, unread)
        if decision is None:
            # Pre-send with no new events: keep_plan without an AI call (MD B5).
            if resolved_trigger == "message_due" and not unread:
                decision = {
                    "action": "keep_plan",
                    "reason": "No change",
                    "decided_by": "rules",
                    "confidence": None,
                    "needs_approval": False,
                }
            else:
                try:
                    raw = decide_next_action(context) or {}
                except Exception as exc:  # noqa: BLE001
                    logger.exception("decide_next_action failed run_id=%s", run.id)
                    decision = {
                        "action": "keep_plan",
                        "reason": f"decide_next_action failed: {exc}",
                        "decided_by": "rules",
                        "confidence": None,
                        "needs_approval": False,
                        "_failed": True,
                    }
                else:
                    decision = self._normalize_decision(raw)

        # Quiet keep_plan on message_due: no audit spam, just continue the send.
        if (
            resolved_trigger == "message_due"
            and not unread
            and decision.get("action") == "keep_plan"
            and not decision.get("_failed")
        ):
            run.last_reassessed_at = _utcnow()
            if commit:
                self.db.commit()
            return {
                "action": "keep_plan",
                "status": "executed",
                "events_marked": 0,
                "abort_send": False,
            }

        row = self._save_decision(
            run=run,
            account=account,
            strategy=strategy,
            decision=decision,
            trigger_event_ids=[e.id for e in unread],
            context=context,
        )

        abort_send = False
        needs_notify = False
        client = self.db.get(PayflowClientModel, run.client_id)
        if decision.get("_failed"):
            row.status = "failed"
            row.outcome = {"error": decision.get("reason")}
        elif self._requires_human_approval(decision, client):
            # B6 approval flow; run continues on the normal plan meanwhile. Only the latest waits, so
            # Operations act on what is true now.
            self._supersede_waiting(run, except_id=row.id)
            row.status = "awaiting_approval"
            row.needs_approval = True
            needs_notify = True
        else:
            if decision.get("action") in _PLAN_CHANGING_ACTIONS:
                self._supersede_prior_decisions(run, except_id=row.id)
            outcome = self._apply_decision(
                run,
                account,
                strategy,
                steps,
                decision,
                row,
                due_step_id=due_step_id,
            )
            row.status = outcome.get("status") or "executed"
            row.outcome = outcome
            row.executed_at = _utcnow()
            if outcome.get("human_review_id"):
                row.human_review_id = outcome["human_review_id"]
            abort_send = bool(outcome.get("abort_send"))

        self._mark_processed(unread)
        self._refresh_engagement_state(run, unread)
        run.last_reassessed_at = _utcnow()

        if run.status in _TERMINAL or run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value:
            abort_send = True

        exec_svc.log_event(
            event_type="adaptive_decision",
            message=(
                f"Adaptive {decision.get('action')} "
                f"({decision.get('decided_by')}: {decision.get('reason') or '—'})"
            ),
            run_id=run.id,
            account_id=account.id,
            strategy_id=strategy.id,
            job_id=job_id,
            detail=str(decision.get("action")),
        )
        if commit:
            self.db.commit()
        if needs_notify:
            self._notify_decision_awaiting(row)
        return {
            "decision_id": row.id,
            "action": row.action,
            "status": row.status,
            "events_marked": len(unread),
            "abort_send": abort_send,
        }

    # ------------------------------------------------------------------ B6 APIs

    def get_run_adaptive(self, run_id: int) -> dict[str, Any] | None:
        """Snapshot for GET /workflow-runs/{id}/adaptive and workflow-execution."""
        run = self.db.get(PayflowWorkflowRunModel, run_id)
        if not run:
            return None
        strategy = self.db.get(PayflowStrategyModel, run.strategy_id)
        exec_svc = WorkflowExecutionService(self.db)
        steps = list(exec_svc._run_steps(run, strategy)) if strategy else []
        steps_by_id = {s.get("id"): s for s in steps if isinstance(s, dict) and s.get("id")}

        pending = (
            self.db.query(PayflowWorkflowJobModel)
            .filter(
                PayflowWorkflowJobModel.run_id == run.id,
                PayflowWorkflowJobModel.job_type == PayflowWorkflowJobType.EXECUTE_STEP.value,
                PayflowWorkflowJobModel.status == PayflowWorkflowJobStatus.PENDING.value,
            )
            .order_by(PayflowWorkflowJobModel.scheduled_at.asc())
            .first()
        )
        next_scheduled = None
        next_message = None
        if pending and run.status not in _TERMINAL:
            step_def = steps_by_id.get(pending.step_id or "") or {}
            next_scheduled = {
                "step_id": pending.step_id,
                "kind": step_def.get("kind"),
                "title": step_def.get("title"),
                "at": pending.scheduled_at,
            }
            # The message that job leads to (past waits and checks) - None when none, or a check could go either way.
            ahead = messages_after(steps_by_id, pending.step_id)
            if len(ahead) == 1:
                next_message = {
                    "step_id": ahead[0].get("id"),
                    "title": ahead[0].get("title"),
                    "not_before": pending.scheduled_at,
                }

        events = (
            self.db.query(PayflowEngagementEventModel)
            .filter(PayflowEngagementEventModel.run_id == run.id)
            .order_by(PayflowEngagementEventModel.occurred_at.desc())
            .limit(100)
            .all()
        )
        decisions = (
            self.db.query(PayflowAdaptiveDecisionModel)
            .filter(PayflowAdaptiveDecisionModel.run_id == run.id)
            .order_by(PayflowAdaptiveDecisionModel.id.desc())
            .limit(50)
            .all()
        )
        account = self.db.get(PayflowAccountModel, run.account_id)
        messages = self._sent_messages(run, steps_by_id)
        decision_items = []
        for decision in decisions:
            item = self._serialize_decision(decision, client_id=run.client_id)
            item["replace_title"] = (steps_by_id.get(decision.replace_step_id or "") or {}).get("title")
            item["follow_up_title"] = (steps_by_id.get(decision.follow_up_step_id or "") or {}).get("title")
            follow = steps_by_id.get(decision.follow_up_step_id or "") if decision.action == "use_follow_up" else None
            if follow and account:
                item["preview"] = self._message_preview(follow, account, strategy)
            decision_items.append(item)
        return {
            "run_id": run.id,
            "account_id": run.account_id,
            "client_id": run.client_id,
            "strategy_id": run.strategy_id,
            "adaptive": bool(getattr(strategy, "adaptive", True)) if strategy else False,
            "engagement_state": run.engagement_state,
            "preferred_channel": run.preferred_channel,
            "swaps": dict(run.swaps or {}),
            "last_reassessed_at": run.last_reassessed_at,
            "next_scheduled": next_scheduled,
            "next_message": next_message,
            "events": [self._serialize_event(e) for e in events],
            "decisions": decision_items,
            "messages": messages,
            "lined_up": [
                {
                    "replace_step_id": planned_id,
                    "replace_title": (steps_by_id.get(planned_id) or {}).get("title"),
                    "follow_up_step_id": follow_id,
                    "follow_up_title": (steps_by_id.get(follow_id) or {}).get("title"),
                    "follow_up_level": follow_up_level(steps_by_id.get(follow_id)),
                }
                for planned_id, follow_id in (run.swaps or {}).items()
            ],
            "follow_ups": self._follow_up_usage(run, steps_by_id, messages),
        }

    def _follow_up_usage(
        self, run: PayflowWorkflowRunModel, steps_by_id: dict[str, dict], messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Every follow-up of the run's workflow and where it stands for this customer: "sent" (when, in place
        of which message), "lined_up" (waiting to go in a planned message's place), "available" or "not_written".
        A message that failed or was blocked does not count as sent (last_failed says so)."""
        lined_up = {follow_id: planned_id for planned_id, follow_id in (run.swaps or {}).items()}
        sent: dict[str, dict[str, Any]] = {}
        failed: set[str] = set()
        for message in messages:  # latest first
            if not message["follow_up"]:
                continue
            if message["status"] in NOT_SENT:
                failed.add(message["step_id"])
            else:
                sent.setdefault(message["step_id"], message)
        usage = []
        for step_id, step in steps_by_id.items():
            if not is_follow_up_step(step):
                continue
            message = sent.get(step_id)
            if message:
                state, replaced_id = "sent", message["replaced_step_id"]
            elif step_id in lined_up:
                state, replaced_id = "lined_up", lined_up[step_id]
            else:
                state, replaced_id = ("available" if has_text(step) else "not_written"), None
            usage.append({
                "step_id": step_id,
                "follow_up_for": follow_up_for(step),
                "level": follow_up_level(step),
                "title": step.get("title"),
                "state": state,
                "sent_at": message["sent_at"] if message else None,
                "replaced_step_id": replaced_id,
                "replaced_title": (steps_by_id.get(replaced_id or "") or {}).get("title"),
                "last_failed": step_id in failed and not message,
            })
        return usage

    def _message_preview(
        self, step: dict[str, Any], account: PayflowAccountModel, strategy: PayflowStrategyModel | None
    ) -> dict[str, Any]:
        """What `step` sends this customer, picked as the worker picks it: their audience version, in their
        language when it is written in it. Variables are filled at send time."""
        cfg, version = audience_message_config(
            dict(step.get("config") or {}), account, segmentation=getattr(strategy, "segmentation_snapshot", None)
        )
        language = str(account.language or "").strip().upper()
        written_in = language if language and language in (cfg.get("translations") or {}) else None
        text = localized_message_config(cfg, written_in)
        return {
            "step_id": step.get("id"),
            "title": step.get("title"),
            "level": follow_up_level(step),
            "version": version,
            "version_label": audience_label(version),
            "language": written_in,
            **{part: text.get(part) for part in ("subject", "body", "sms_body")},
        }

    def _sent_messages(self, run: PayflowWorkflowRunModel, steps_by_id: dict[str, dict]) -> list[dict[str, Any]]:
        """The run's messages, latest first: which text went out and, for a follow-up, the planned message it
        was sent in place of (from the worker's step runs)."""
        replaced: dict[str, str] = {}
        step_runs = self.db.query(PayflowWorkflowStepRunModel).filter(PayflowWorkflowStepRunModel.run_id == run.id)
        for step_run in step_runs:
            result = step_run.result_json if isinstance(step_run.result_json, dict) else {}
            if result.get("adaptive_follow_up"):
                replaced[result["adaptive_follow_up"]] = result.get("adaptive_swap_from")
        messages = (
            self.db.query(PayflowCommunicationModel)
            .filter(PayflowCommunicationModel.run_id == run.id)
            .order_by(PayflowCommunicationModel.id.desc())
            .limit(100)
            .all()
        )
        items = []
        for message in messages:
            step = steps_by_id.get(message.step_id or "") or {}
            follow_up = is_follow_up_step(step)
            replaced_id = replaced.get(message.step_id or "") if follow_up else None
            items.append({
                "communication_id": message.id,
                "step_id": message.step_id,
                "title": step.get("title"),
                "channel": message.channel,
                "status": message.status,
                "sent_at": message.sent_at,
                "version": message.message_audience,
                "version_label": audience_label(message.message_audience),
                "follow_up": follow_up,
                "follow_up_level": follow_up_level(step) if follow_up else None,
                "replaced_step_id": replaced_id,
                "replaced_title": (steps_by_id.get(replaced_id or "") or {}).get("title"),
            })
        return items

    def list_decisions(
        self,
        *,
        status: str | None = None,
        client_id: int | None = None,
        run_id: int | None = None,
        limit: int = 100,
        allowed_client_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        q = (
            self.db.query(PayflowAdaptiveDecisionModel, PayflowWorkflowRunModel.client_id)
            .join(
                PayflowWorkflowRunModel,
                PayflowWorkflowRunModel.id == PayflowAdaptiveDecisionModel.run_id,
            )
        )
        if status:
            q = q.filter(PayflowAdaptiveDecisionModel.status == status)
        if client_id is not None:
            q = q.filter(PayflowWorkflowRunModel.client_id == client_id)
        if run_id is not None:
            q = q.filter(PayflowAdaptiveDecisionModel.run_id == run_id)
        if allowed_client_ids is not None:
            q = q.filter(PayflowWorkflowRunModel.client_id.in_(allowed_client_ids))
        rows = (
            q.order_by(PayflowAdaptiveDecisionModel.id.desc())
            .limit(max(1, min(limit, 500)))
            .all()
        )
        return [self._serialize_decision(d, client_id=cid) for d, cid in rows]

    def approve_decision(
        self,
        decision_id: int,
        *,
        user: UserModel,
        note: str | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        row, run, account, strategy, steps = self._load_decision_context(decision_id)
        if row.status != "awaiting_approval":
            raise ConflictError(f"Decision is {row.status}, not awaiting_approval")

        self._stamp_reviewer(row, user=user, note=note)
        self._close_notifications([row.id])
        applies, why = self._decision_still_applies(run, row)
        if not applies:
            row.status = "expired"
            row.outcome = {"expired_reason": why}
            row.executed_at = _utcnow()
            if commit:
                self.db.commit()
            return self._serialize_decision(row, client_id=run.client_id)

        decision = self._decision_dict_from_row(row)
        if decision.get("action") in _PLAN_CHANGING_ACTIONS:
            self._supersede_prior_decisions(run, except_id=row.id)
        outcome = self._apply_decision(run, account, strategy, steps, decision, row)
        # It could not be carried out (e.g. SMS for a customer without a mobile number): the history says so.
        row.status = "failed" if outcome.get("status") == "failed" else "approved"
        row.outcome = outcome
        row.executed_at = _utcnow()
        if outcome.get("human_review_id"):
            row.human_review_id = outcome["human_review_id"]
        WorkflowExecutionService(self.db).log_event(
            event_type="adaptive_decision_approved",
            message=f"Adaptive decision {row.id} approved ({row.action})",
            run_id=run.id,
            account_id=account.id,
            strategy_id=strategy.id,
            detail=row.action,
        )
        if commit:
            self.db.commit()
        return self._serialize_decision(row, client_id=run.client_id)

    def modify_decision(
        self,
        decision_id: int,
        *,
        user: UserModel,
        note: str | None = None,
        action: str | None = None,
        replace_step_id: str | None = None,
        follow_up_step_id: str | None = None,
        at: str | datetime | None = None,
        channel: str | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        row, run, account, strategy, steps = self._load_decision_context(decision_id)
        if row.status != "awaiting_approval":
            raise ConflictError(f"Decision is {row.status}, not awaiting_approval")

        ai_snapshot = self._decision_fields(row)
        proposed = {
            "action": (action or row.action or "keep_plan"),
            "replace_step_id": (
                replace_step_id if replace_step_id is not None else row.replace_step_id
            ),
            "follow_up_step_id": (
                follow_up_step_id if follow_up_step_id is not None else row.follow_up_step_id
            ),
            "at": at if at is not None else (row.at.isoformat() if row.at else None),
            "channel": channel if channel is not None else row.channel,
            "reason": (note or row.reason or "Modified by Operations"),
            "confidence": row.confidence,
            "decided_by": "operations",
            "needs_approval": False,
        }
        decision = self._normalize_decision(proposed)
        if decision.get("_failed") or decision.get("action") not in _ALLOWED_ACTIONS:
            raise ValidationAppError(decision.get("reason") or "Invalid modified decision")

        row.action = decision["action"]
        row.replace_step_id = decision.get("replace_step_id")
        row.follow_up_step_id = decision.get("follow_up_step_id")
        row.at = self._parse_at(decision.get("at"))
        row.channel = decision.get("channel")
        row.reason = decision.get("reason")
        row.decided_by = "operations"
        self._stamp_reviewer(row, user=user, note=note)
        self._close_notifications([row.id])

        applies, why = self._decision_still_applies(run, row)
        if not applies:
            row.status = "expired"
            row.outcome = {"expired_reason": why, "ai_decision": ai_snapshot, "reviewer": decision}
            row.executed_at = _utcnow()
            if commit:
                self.db.commit()
            return self._serialize_decision(row, client_id=run.client_id)

        if decision.get("action") in _PLAN_CHANGING_ACTIONS:
            self._supersede_prior_decisions(run, except_id=row.id)
        outcome = self._apply_decision(run, account, strategy, steps, decision, row)
        row.status = "failed" if outcome.get("status") == "failed" else "modified"
        row.outcome = {
            "ai_decision": ai_snapshot,
            "reviewer": decision,
            **(outcome if isinstance(outcome, dict) else {"applied": outcome}),
        }
        row.executed_at = _utcnow()
        if outcome.get("human_review_id"):
            row.human_review_id = outcome["human_review_id"]
        WorkflowExecutionService(self.db).log_event(
            event_type="adaptive_decision_modified",
            message=f"Adaptive decision {row.id} modified → {row.action}",
            run_id=run.id,
            account_id=account.id,
            strategy_id=strategy.id,
            detail=row.action,
        )
        if commit:
            self.db.commit()
        return self._serialize_decision(row, client_id=run.client_id)

    def reject_decision(
        self,
        decision_id: int,
        *,
        user: UserModel,
        note: str | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        row, run, account, strategy, _steps = self._load_decision_context(decision_id)
        if row.status != "awaiting_approval":
            raise ConflictError(f"Decision is {row.status}, not awaiting_approval")

        self._stamp_reviewer(row, user=user, note=note)
        self._close_notifications([row.id])
        row.status = "rejected"
        row.outcome = {"rejected": True}
        row.executed_at = _utcnow()
        WorkflowExecutionService(self.db).log_event(
            event_type="adaptive_decision_rejected",
            message=f"Adaptive decision {row.id} rejected ({row.action})",
            run_id=run.id,
            account_id=account.id,
            strategy_id=strategy.id,
            detail=row.action,
        )
        if commit:
            self.db.commit()
        return self._serialize_decision(row, client_id=run.client_id)

    def _load_decision_context(
        self, decision_id: int
    ) -> tuple[
        PayflowAdaptiveDecisionModel,
        PayflowWorkflowRunModel,
        PayflowAccountModel,
        PayflowStrategyModel,
        list[dict[str, Any]],
    ]:
        row = self.db.get(PayflowAdaptiveDecisionModel, decision_id)
        if not row:
            raise NotFoundError("Adaptive decision not found")
        run = self.db.get(PayflowWorkflowRunModel, row.run_id)
        if not run:
            raise NotFoundError("Workflow run not found")
        account = self.db.get(PayflowAccountModel, run.account_id)
        if not account:
            raise NotFoundError("Account not found")
        strategy = self.db.get(PayflowStrategyModel, run.strategy_id)
        if not strategy:
            raise NotFoundError("Strategy not found")
        steps = list(WorkflowExecutionService(self.db)._run_steps(run, strategy))
        return row, run, account, strategy, steps

    def _stamp_reviewer(
        self,
        row: PayflowAdaptiveDecisionModel,
        *,
        user: UserModel,
        note: str | None,
    ) -> None:
        label = (getattr(user, "full_name", None) or getattr(user, "email", None) or str(user.id))
        row.decided_by_user = str(label)[:255]
        row.decision_note = (note or "").strip() or None
        row.decided_at = _utcnow()

    def _decision_still_applies(
        self, run: PayflowWorkflowRunModel, row: PayflowAdaptiveDecisionModel
    ) -> tuple[bool, str]:
        if row.status not in {"awaiting_approval"}:
            return False, "not_awaiting"
        if run.status in _TERMINAL:
            return False, "run_terminal"
        if row.action == "use_follow_up" and row.replace_step_id:
            if self._step_already_sent(run.id, row.replace_step_id):
                return False, "step_already_sent"
        if row.action == "move_next":
            pending = (
                self.db.query(PayflowWorkflowJobModel.id)
                .filter(
                    PayflowWorkflowJobModel.run_id == run.id,
                    PayflowWorkflowJobModel.job_type == PayflowWorkflowJobType.EXECUTE_STEP.value,
                    PayflowWorkflowJobModel.status == PayflowWorkflowJobStatus.PENDING.value,
                )
                .first()
            )
            if not pending:
                return False, "no_pending_step"
        return True, "ok"

    def _decision_fields(self, row: PayflowAdaptiveDecisionModel) -> dict[str, Any]:
        return {
            "action": row.action,
            "replace_step_id": row.replace_step_id,
            "follow_up_step_id": row.follow_up_step_id,
            "at": row.at.isoformat() if row.at else None,
            "channel": row.channel,
            "reason": row.reason,
            "confidence": row.confidence,
            "decided_by": row.decided_by,
        }

    def _decision_dict_from_row(self, row: PayflowAdaptiveDecisionModel) -> dict[str, Any]:
        return {
            "action": row.action,
            "replace_step_id": row.replace_step_id,
            "follow_up_step_id": row.follow_up_step_id,
            "at": row.at.isoformat() if row.at else None,
            "channel": row.channel,
            "reason": row.reason,
            "confidence": row.confidence,
            "decided_by": row.decided_by or "rules",
            "needs_approval": False,
        }

    def _serialize_decision(
        self, row: PayflowAdaptiveDecisionModel, *, client_id: int | None = None
    ) -> dict[str, Any]:
        return {
            "id": row.id,
            "run_id": row.run_id,
            "account_id": row.account_id,
            "strategy_id": row.strategy_id,
            "client_id": client_id,
            "trigger_event_ids": list(row.trigger_event_ids or []),
            "decided_by": row.decided_by,
            "action": row.action,
            "replace_step_id": row.replace_step_id,
            "follow_up_step_id": row.follow_up_step_id,
            "at": row.at,
            "channel": row.channel,
            "reason": row.reason,
            "confidence": row.confidence,
            "needs_approval": bool(row.needs_approval),
            "status": row.status,
            "decided_by_user": row.decided_by_user,
            "decision_note": row.decision_note,
            "decided_at": row.decided_at,
            "human_review_id": row.human_review_id,
            "executed_at": row.executed_at,
            "outcome": row.outcome,
            "created_at": row.created_at,
        }

    def _serialize_event(self, event: PayflowEngagementEventModel) -> dict[str, Any]:
        step_id = None
        if event.communication_id:
            comm = self.db.get(PayflowCommunicationModel, event.communication_id)
            step_id = comm.step_id if comm else None
        return {
            "id": event.id,
            "type": event.event_type,
            "at": event.occurred_at,
            "channel": event.channel,
            "source": event.source,
            "step_id": step_id,
            "detail": event.detail,
            "processed_at": event.processed_at,
        }

    def _notify_decision_awaiting(self, row: PayflowAdaptiveDecisionModel) -> None:
        try:
            from app.modules.payflow.services.notification_service import PayflowNotificationService

            run = self.db.get(PayflowWorkflowRunModel, row.run_id)
            if not run:
                return
            PayflowNotificationService(self.db).notify_adaptive_decision_awaiting(
                decision_id=row.id,
                run_id=run.id,
                account_id=run.account_id,
                client_id=run.client_id,
                action=row.action,
                reason=row.reason,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to notify adaptive decision awaiting approval id=%s", row.id
            )

    # ------------------------------------------------------------------ safety

    def _safety_decision(
        self,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        events: list[PayflowEngagementEventModel],
    ) -> dict[str, Any] | None:
        """Always-automatic rules. Returns a decision or None to continue to NBA."""
        bal = float(account.outstanding_balance or 0)
        paid_event = any(e.event_type == "payment_completed" for e in events)
        # Only a real payment / nothing owed. Not payment_status(): it also says "Paid In Full" for a
        # case closed with money owed, and the engine already ends those runs as Cancelled.
        if paid_event or bal <= 0.005:
            if run.status not in _TERMINAL:
                return {
                    "action": "stop",
                    "reason": "Account paid in full / nothing owed — stop adaptive communications",
                    "decided_by": "safety",
                    "confidence": 100,
                    "needs_approval": False,
                }

        email_dead = (
            account.email_consent is False
            or any(e.event_type == "email_unsubscribed" for e in events)
            or any(e.event_type == "email_bounced" for e in events)
        )
        sms_opted = bool(account.sms_opt_out) or any(
            e.event_type == "sms_opted_out" for e in events
        )
        has_mobile = bool((account.phone_mobile or account.phone_work or "").strip())
        sms_possible = has_mobile and not sms_opted
        email_ok = account.email_consent is not False and bool((account.email or "").strip())

        if email_dead and not sms_possible:
            return {
                "action": "to_operations",
                "reason": "Customer unreachable on email and SMS — route to Operations",
                "decided_by": "safety",
                "confidence": 100,
                "needs_approval": False,
            }
        # Switch once. email_consent False stays set, so without this every reassessment would
        # switch again, never reach decide_next_action, and supersede (drop) its follow-up swaps.
        if email_dead and sms_possible and run.preferred_channel != "SMS":
            return {
                "action": "switch_channel",
                "channel": "SMS",
                "reason": "Email unreachable; switch remaining contact to SMS",
                "decided_by": "safety",
                "confidence": 100,
                "needs_approval": False,
            }
        if sms_opted and email_ok and not email_dead and run.preferred_channel != "Email":
            # SMS dead but email still possible.
            if any(e.event_type in {"sms_opted_out", "sms_failed"} for e in events):
                return {
                    "action": "switch_channel",
                    "channel": "Email",
                    "reason": "SMS unreachable; switch remaining contact to Email",
                    "decided_by": "safety",
                    "confidence": 100,
                    "needs_approval": False,
                }
        return None

    # ------------------------------------------------------------------ apply (B5)

    def _apply_decision(
        self,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        steps: list[dict[str, Any]],
        decision: dict[str, Any],
        row: PayflowAdaptiveDecisionModel,
        *,
        due_step_id: str | None = None,
    ) -> dict[str, Any]:
        action = decision.get("action") or "keep_plan"
        if action not in _ALLOWED_ACTIONS:
            return {"status": "failed", "error": f"unsupported action {action}", "abort_send": False}

        if action == "keep_plan":
            return {"status": "executed", "applied": "keep_plan", "abort_send": False}

        if action == "drop_follow_up":
            return self._drop_follow_up(run, decision)

        if action == "switch_channel":
            channel = (decision.get("channel") or "").strip()
            if channel not in {"Email", "SMS"}:
                return {"status": "failed", "error": "channel must be Email or SMS", "abort_send": False}
            if not self._channel_allowed_for_account(account, channel):
                return {
                    "status": "failed",
                    "error": f"channel {channel} not allowed for customer",
                    "abort_send": False,
                }
            run.preferred_channel = channel
            return {
                "status": "executed",
                "applied": "switch_channel",
                "channel": channel,
                "abort_send": False,
            }

        if action == "recheck_at":
            at = self._parse_at(decision.get("at"))
            if at is None:
                return {"status": "failed", "error": "recheck_at requires at", "abort_send": False}
            job = enqueue_reassess_run(
                self.db,
                run_id=run.id,
                account_id=run.account_id,
                strategy_id=run.strategy_id,
                scheduled_at=at,
                payload={"trigger": "event", "decision_id": row.id},
                commit=False,
            )
            return {
                "status": "executed",
                "applied": "recheck_at",
                "job_id": job.id if job else None,
                "at": at.isoformat(),
                "abort_send": False,
            }

        if action in {"pause", "to_operations"}:
            reason = (decision.get("reason") or "Adaptive: paused").strip()
            review_id = self._pause_run(run, account, strategy, reason=reason)
            return {
                "status": "executed",
                "applied": action,
                "human_review_id": review_id,
                "abort_send": True,
            }

        if action == "stop":
            reason = (decision.get("reason") or "Adaptive stop").strip()
            cancel_pending_jobs_for_run(self.db, run.id)
            run.status = PayflowWorkflowRunStatus.COMPLETED.value
            run.completed_at = _utcnow()
            run.last_error = f"Stopped by adaptive decision: {reason}"
            # Never apply an Outcome / balance change.
            return {"status": "executed", "applied": "stop", "abort_send": True}

        if action == "use_follow_up":
            replace_id = (decision.get("replace_step_id") or "").strip()
            follow_id = (decision.get("follow_up_step_id") or "").strip()
            by_id = {s.get("id"): s for s in steps if isinstance(s, dict) and s.get("id")}
            planned = by_id.get(replace_id)
            follow = by_id.get(follow_id)
            if (
                not planned
                or not follow
                or planned.get("kind") != "Communication"
                or is_follow_up_step(planned)
                or not is_follow_up_step(follow)
            ):
                return {
                    "status": "failed",
                    "error": "invalid replace/follow_up step ids",
                    "abort_send": False,
                }
            if self._step_already_sent(run.id, replace_id):
                return {
                    "status": "failed",
                    "error": f"planned step {replace_id} already sent",
                    "abort_send": False,
                }
            swaps = dict(run.swaps or {})
            swaps[replace_id] = follow_id
            run.swaps = swaps
            # Optional channel: e.g. "email not opened" → the follow-up (and later messages) go by SMS first.
            channel = (decision.get("channel") or "").strip()
            previous_channel = run.preferred_channel
            if channel in {"Email", "SMS"} and self._channel_allowed_for_account(account, channel):
                run.preferred_channel = channel
            at = self._parse_at(decision.get("at"))
            abort = False
            if at is not None:
                self._reschedule_pending_step(run.id, replace_id, at, decision_id=row.id)
                # Pre-send: this step was due now but timing moved → defer send.
                if due_step_id and due_step_id == replace_id and at > _utcnow() + timedelta(seconds=30):
                    from app.modules.payflow.services.workflow_queue import enqueue_execute_step

                    enqueue_execute_step(
                        self.db,
                        run_id=run.id,
                        step_id=replace_id,
                        account_id=run.account_id,
                        strategy_id=run.strategy_id,
                        scheduled_at=at,
                        payload={"decision_id": row.id, "after_comm_delay": True},
                        commit=False,
                    )
                    abort = True
            return {
                "status": "executed",
                "applied": "use_follow_up",
                "replace_step_id": replace_id,
                "follow_up_step_id": follow_id,
                # What drop_follow_up restores when the customer answers the situation before it is sent.
                "previous_channel": previous_channel,
                "abort_send": abort,
            }

        if action == "move_next":
            at = self._parse_at(decision.get("at"))
            if at is None:
                return {"status": "failed", "error": "move_next requires at", "abort_send": False}
            pending = (
                self.db.query(PayflowWorkflowJobModel)
                .filter(
                    PayflowWorkflowJobModel.run_id == run.id,
                    PayflowWorkflowJobModel.job_type == PayflowWorkflowJobType.EXECUTE_STEP.value,
                    PayflowWorkflowJobModel.status == PayflowWorkflowJobStatus.PENDING.value,
                )
                .order_by(PayflowWorkflowJobModel.scheduled_at.asc())
                .first()
            )
            # Pre-send path: the due step's job is already RUNNING — schedule a new one.
            if pending is None and due_step_id:
                from app.modules.payflow.services.workflow_queue import enqueue_execute_step

                if self._prevent_communication_blocks_earlier(account, at, None):
                    return {
                        "status": "failed",
                        "error": "cannot move earlier while Prevent Communication applies",
                        "abort_send": False,
                    }
                job = enqueue_execute_step(
                    self.db,
                    run_id=run.id,
                    step_id=due_step_id,
                    account_id=run.account_id,
                    strategy_id=run.strategy_id,
                    scheduled_at=at,
                    payload={"decision_id": row.id, "after_comm_delay": True},
                    commit=False,
                )
                return {
                    "status": "executed",
                    "applied": "move_next",
                    "step_id": due_step_id,
                    "at": at.isoformat(),
                    "job_id": job.id,
                    "abort_send": True,
                }
            if not pending:
                return {
                    "status": "failed",
                    "error": "no pending execute_step to move",
                    "abort_send": False,
                }
            if self._prevent_communication_blocks_earlier(account, at, pending.scheduled_at):
                return {
                    "status": "failed",
                    "error": "cannot move earlier while Prevent Communication applies",
                    "abort_send": False,
                }
            pending.scheduled_at = at
            payload = dict(pending.payload or {})
            payload["decision_id"] = row.id
            pending.payload = payload
            abort = bool(due_step_id and pending.step_id == due_step_id)
            return {
                "status": "executed",
                "applied": "move_next",
                "step_id": pending.step_id,
                "at": at.isoformat(),
                "abort_send": abort,
            }

        return {"status": "failed", "error": f"unhandled action {action}", "abort_send": False}

    def _supersede_prior_decisions(
        self, run: PayflowWorkflowRunModel, *, except_id: int | None = None
    ) -> int:
        """Newer plan-changing decision wins: supersede priors, cancel their jobs, drop swaps."""
        q = self.db.query(PayflowAdaptiveDecisionModel).filter(
            PayflowAdaptiveDecisionModel.run_id == run.id,
            PayflowAdaptiveDecisionModel.status.in_(list(_OPEN_DECISION_STATUSES)),
            PayflowAdaptiveDecisionModel.action.in_(list(_PLAN_CHANGING_ACTIONS)),
        )
        if except_id is not None:
            q = q.filter(PayflowAdaptiveDecisionModel.id != except_id)
        swaps = dict(run.swaps or {})
        # A follow-up already sent (no longer lined up) is done: it stays "executed" in the case history.
        priors = [
            prior for prior in q.all()
            if not (
                prior.action == "use_follow_up"
                and prior.status in _APPLIED_DECISION_STATUSES
                and swaps.get(prior.replace_step_id or "") != prior.follow_up_step_id
            )
        ]
        if not priors:
            return 0
        self._close_notifications([prior.id for prior in priors if prior.status == "awaiting_approval"])
        for prior in priors:
            prior.status = "superseded"
            self._cancel_jobs_for_decision(run.id, prior.id)
            if (
                prior.action == "use_follow_up"
                and prior.replace_step_id
                and swaps.get(prior.replace_step_id) == prior.follow_up_step_id
            ):
                swaps.pop(prior.replace_step_id, None)
        run.swaps = swaps or None
        return len(priors)

    def _supersede_waiting(self, run: PayflowWorkflowRunModel, *, except_id: int) -> int:
        """An older decision of the run still waiting for approval is replaced by the newer one."""
        waiting = (
            self.db.query(PayflowAdaptiveDecisionModel)
            .filter(
                PayflowAdaptiveDecisionModel.run_id == run.id,
                PayflowAdaptiveDecisionModel.status == "awaiting_approval",
                PayflowAdaptiveDecisionModel.id != except_id,
            )
            .all()
        )
        for row in waiting:
            row.status = "superseded"
            row.outcome = {"superseded_by": except_id}
        self._close_notifications([row.id for row in waiting])
        return len(waiting)

    def expire_waiting_decisions(self) -> int:
        """Decisions waiting for approval that no longer apply - the case ended, the message they would replace
        went out, nothing is left to move - are closed as "expired", with the reason. The worker calls this with
        its timer sweep; the caller commits."""
        waiting = (
            self.db.query(PayflowAdaptiveDecisionModel)
            .filter(PayflowAdaptiveDecisionModel.status == "awaiting_approval")
            .all()
        )
        expired: list[int] = []
        for row in waiting:
            run = self.db.get(PayflowWorkflowRunModel, row.run_id)
            applies, why = self._decision_still_applies(run, row) if run else (False, "run_missing")
            if applies:
                continue
            row.status = "expired"
            row.outcome = {"expired_reason": why}
            row.executed_at = _utcnow()
            expired.append(row.id)
        self._close_notifications(expired)
        return len(expired)

    def _close_notifications(self, decision_ids: list[int]) -> None:
        """These decisions no longer wait for approval: their "awaiting approval" notifications are marked read,
        so the bell lists only decisions still waiting."""
        if not decision_ids:
            return
        (
            self.db.query(PayflowNotificationModel)
            .filter(
                PayflowNotificationModel.entity_type == "adaptive_decision",
                PayflowNotificationModel.entity_id.in_([str(decision_id) for decision_id in decision_ids]),
                PayflowNotificationModel.read_at.is_(None),
            )
            .update({PayflowNotificationModel.read_at: _utcnow()}, synchronize_session=False)
        )

    def _drop_follow_up(self, run: PayflowWorkflowRunModel, decision: dict[str, Any]) -> dict[str, Any]:
        """The customer answered a lined-up follow-up's situation (e.g. opened the email): the planned message
        goes instead, by the channel used before the follow-up changed it."""
        replace_id = (decision.get("replace_step_id") or "").strip()
        follow_id = (decision.get("follow_up_step_id") or "").strip()
        swaps = dict(run.swaps or {})
        if not replace_id or swaps.get(replace_id) != follow_id:
            return {"status": "failed", "error": "that follow-up is not lined up", "abort_send": False}
        swaps.pop(replace_id)
        run.swaps = swaps or None

        lined_up = (
            self.db.query(PayflowAdaptiveDecisionModel)
            .filter(
                PayflowAdaptiveDecisionModel.run_id == run.id,
                PayflowAdaptiveDecisionModel.action == "use_follow_up",
                PayflowAdaptiveDecisionModel.replace_step_id == replace_id,
                PayflowAdaptiveDecisionModel.follow_up_step_id == follow_id,
                PayflowAdaptiveDecisionModel.status.in_(list(_APPLIED_DECISION_STATUSES)),
            )
            .order_by(PayflowAdaptiveDecisionModel.id.desc())
            .first()
        )
        if lined_up is not None:
            lined_up.status = "superseded"
            self._cancel_jobs_for_decision(run.id, lined_up.id)
            outcome = lined_up.outcome if isinstance(lined_up.outcome, dict) else {}
            if lined_up.channel and run.preferred_channel == lined_up.channel and not self._channel_switched_after(
                run.id, lined_up.id
            ):
                run.preferred_channel = outcome.get("previous_channel")
        return {
            "status": "executed",
            "applied": "drop_follow_up",
            "replace_step_id": replace_id,
            "follow_up_step_id": follow_id,
            "abort_send": False,
        }

    def _channel_switched_after(self, run_id: int, decision_id: int) -> bool:
        """A channel switch carried out after `decision_id` (e.g. the email bounced): it stays."""
        return (
            self.db.query(PayflowAdaptiveDecisionModel.id)
            .filter(
                PayflowAdaptiveDecisionModel.run_id == run_id,
                PayflowAdaptiveDecisionModel.id > decision_id,
                PayflowAdaptiveDecisionModel.action == "switch_channel",
                PayflowAdaptiveDecisionModel.status.in_([*_APPLIED_DECISION_STATUSES, "superseded"]),
            )
            .first()
            is not None
        )

    def _cancel_jobs_for_decision(self, run_id: int, decision_id: int) -> int:
        """Cancel the replaced decision's "check again" jobs. A step it moved keeps its new time: cancelling
        that job would drop the message and stop the customer's workflow there."""
        rows = (
            self.db.query(PayflowWorkflowJobModel)
            .filter(
                PayflowWorkflowJobModel.run_id == run_id,
                PayflowWorkflowJobModel.status == PayflowWorkflowJobStatus.PENDING.value,
            )
            .all()
        )
        cancelled = 0
        for job in rows:
            payload = job.payload if isinstance(job.payload, dict) else {}
            if payload.get("decision_id") != decision_id:
                continue
            if job.job_type == PayflowWorkflowJobType.EXECUTE_STEP.value:
                job.payload = {key: value for key, value in payload.items() if key != "decision_id"}
            else:
                job.status = PayflowWorkflowJobStatus.CANCELLED.value
                cancelled += 1
        return cancelled

    def _job_decision_superseded(self, job: PayflowWorkflowJobModel) -> bool:
        """A "check again" job whose decision was replaced is skipped. A step job always runs (see
        _cancel_jobs_for_decision)."""
        if job.job_type != PayflowWorkflowJobType.REASSESS_RUN.value:
            return False
        payload = job.payload if isinstance(job.payload, dict) else {}
        decision_id = payload.get("decision_id")
        if not decision_id:
            return False
        row = self.db.get(PayflowAdaptiveDecisionModel, int(decision_id))
        return bool(row and row.status == "superseded")

    def _step_already_sent(self, run_id: int, step_id: str) -> bool:
        done = (
            self.db.query(PayflowWorkflowStepRunModel.id)
            .filter(
                PayflowWorkflowStepRunModel.run_id == run_id,
                PayflowWorkflowStepRunModel.step_id == step_id,
                PayflowWorkflowStepRunModel.status == "done",
            )
            .first()
        )
        if done:
            return True
        sent = (
            self.db.query(PayflowCommunicationModel.id)
            .filter(
                PayflowCommunicationModel.run_id == run_id,
                PayflowCommunicationModel.step_id == step_id,
                PayflowCommunicationModel.sent_at.isnot(None),
            )
            .first()
        )
        return sent is not None

    def _channel_allowed_for_account(self, account: PayflowAccountModel, channel: str) -> bool:
        if channel == "Email":
            return account.email_consent is not False and bool((account.email or "").strip())
        if channel == "SMS":
            return not bool(account.sms_opt_out) and bool(
                (account.phone_mobile or account.phone_work or "").strip()
            )
        return False

    def _prevent_communication_blocks_earlier(
        self,
        account: PayflowAccountModel,
        at: datetime,
        current_scheduled: datetime | None,
    ) -> bool:
        """Prevent Communication drops messages — never use move_next to send earlier under it."""
        if current_scheduled is not None and at >= _as_utc(current_scheduled):
            return False
        from app.modules.payflow.services.rule_runtime_service import RuleRuntimeService

        matched = RuleRuntimeService(self.db).first_blocking_action(
            account, for_communication=True
        )
        return bool(matched and matched.get("action") == "Prevent Communication")

    def _pause_run(
        self,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        *,
        reason: str,
    ) -> int | None:
        from app.modules.payflow.services.review_service import PayflowReviewService

        review = PayflowReviewService(self.db).create_review(
            account_id=account.id,
            reason=reason[:255] if reason else "Adaptive: paused",
            proposed_action="Review adaptive workflow decision",
            strategy_id=strategy.id,
            workflow_version=run.strategy_version,
            exception_step_id=run.current_step_id,
            actor_name="Adaptive workflow",
            commit=False,
            notify=True,
            explanation=[reason] if reason else None,
        )
        run.status = PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value
        run.human_review_id = review.id
        account.human_review = True
        account.next_action = "Awaiting human review"
        cancel_pending_jobs_for_run(self.db, run.id)
        return review.id

    def _reschedule_pending_step(
        self,
        run_id: int,
        step_id: str,
        at: datetime,
        *,
        decision_id: int | None,
    ) -> None:
        """Move `step_id`'s waiting job to `at`. When that step has no job yet (a check comes first), the run's
        next step is held until `at` instead - only later, never sooner."""
        pending_steps = self.db.query(PayflowWorkflowJobModel).filter(
            PayflowWorkflowJobModel.run_id == run_id,
            PayflowWorkflowJobModel.job_type == PayflowWorkflowJobType.EXECUTE_STEP.value,
            PayflowWorkflowJobModel.status == PayflowWorkflowJobStatus.PENDING.value,
        )
        pending = (
            pending_steps.filter(PayflowWorkflowJobModel.step_id == step_id)
            .order_by(PayflowWorkflowJobModel.id.desc())
            .first()
        )
        if pending is None:
            pending = pending_steps.order_by(PayflowWorkflowJobModel.scheduled_at.asc()).first()
            if pending is None or (pending.scheduled_at is not None and _as_utc(pending.scheduled_at) >= at):
                return
        pending.scheduled_at = at
        payload = dict(pending.payload or {})
        if decision_id is not None:
            payload["decision_id"] = decision_id
        pending.payload = payload

    # ------------------------------------------------------------------ helpers

    def _requires_human_approval(
        self, decision: dict[str, Any], client: PayflowClientModel | None
    ) -> bool:
        """MD B6: never for safety / keep_plan / recheck_at / pause / to_operations."""
        action = decision.get("action") or "keep_plan"
        decided_by = decision.get("decided_by") or "rules"
        if decided_by == "safety":
            return False
        if action in {"keep_plan", "recheck_at", "pause", "to_operations"}:
            return False
        if decision.get("needs_approval"):
            return True
        ai_mode = str(getattr(client, "ai_mode", None) or "").strip().lower()
        if ai_mode in {"supervised_ai", "supervised"} and decided_by == "ai":
            return True
        return False

    def _normalize_decision(self, raw: dict[str, Any]) -> dict[str, Any]:
        action = str(raw.get("action") or "keep_plan").strip()
        if action not in _ALLOWED_ACTIONS:
            return {
                "action": "keep_plan",
                "reason": f"Invalid action '{action}' — keeping plan",
                "decided_by": "rules",
                "confidence": None,
                "needs_approval": False,
                "_failed": True,
            }
        decided_by = str(raw.get("decided_by") or "rules").strip() or "rules"
        if decided_by not in {"rules", "ai", "safety", "operations"}:
            decided_by = "rules"
        confidence = raw.get("confidence")
        try:
            confidence_i = int(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence_i = None
        return {
            "action": action,
            "replace_step_id": raw.get("replace_step_id"),
            "follow_up_step_id": raw.get("follow_up_step_id"),
            "at": raw.get("at"),
            "channel": raw.get("channel"),
            "reason": str(raw.get("reason") or "")[:2000] or None,
            "confidence": confidence_i,
            "decided_by": decided_by,
            "needs_approval": bool(raw.get("needs_approval")),
        }

    def _save_decision(
        self,
        *,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        decision: dict[str, Any],
        trigger_event_ids: list[int],
        context: dict[str, Any],
    ) -> PayflowAdaptiveDecisionModel:
        at = self._parse_at(decision.get("at"))
        row = PayflowAdaptiveDecisionModel(
            run_id=run.id,
            account_id=account.id,
            strategy_id=strategy.id,
            trigger_event_ids=trigger_event_ids,
            decided_by=str(decision.get("decided_by") or "rules"),
            action=str(decision.get("action") or "keep_plan"),
            replace_step_id=(str(decision["replace_step_id"]) if decision.get("replace_step_id") else None),
            follow_up_step_id=(
                str(decision["follow_up_step_id"]) if decision.get("follow_up_step_id") else None
            ),
            at=at,
            channel=(str(decision["channel"]) if decision.get("channel") else None),
            reason=decision.get("reason"),
            confidence=decision.get("confidence"),
            needs_approval=bool(decision.get("needs_approval")),
            status="executed",
            context=context,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _mark_processed(self, events: list[PayflowEngagementEventModel]) -> None:
        now = _utcnow()
        for event in events:
            event.processed_at = now

    def _refresh_engagement_state(
        self,
        run: PayflowWorkflowRunModel,
        events: list[PayflowEngagementEventModel],
    ) -> None:
        for event in reversed(events):
            label = _ENGAGEMENT_STATE_BY_EVENT.get(event.event_type)
            if label:
                run.engagement_state = label
                return
        if events:
            # Fall back to latest type as a readable label.
            run.engagement_state = (events[-1].event_type or "").replace("_", " ").title()

    def _parse_at(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            at = value
        else:
            text = str(value).strip()
            if not text:
                return None
            try:
                at = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        now = _utcnow()
        if at < now:
            at = now
        max_at = now + timedelta(days=30)
        if at > max_at:
            at = max_at
        return at

    def _build_context(
        self,
        *,
        run: PayflowWorkflowRunModel,
        account: PayflowAccountModel,
        strategy: PayflowStrategyModel,
        steps: list[dict[str, Any]],
        new_events: list[PayflowEngagementEventModel],
        trigger: str,
        job: PayflowWorkflowJobModel | None,
        due_step_id: str | None = None,
    ) -> dict[str, Any]:
        client = self.db.get(PayflowClientModel, run.client_id)
        # Each situation's first-time follow-up (its repeat texts are in `steps`, with config.follow_up_level).
        follow_ups = {follow_up_for(s): s.get("id") for s in steps if is_follow_up_step(s) and follow_up_level(s) == 1}
        pending = (
            self.db.query(PayflowWorkflowJobModel)
            .filter(
                PayflowWorkflowJobModel.run_id == run.id,
                PayflowWorkflowJobModel.job_type == PayflowWorkflowJobType.EXECUTE_STEP.value,
                PayflowWorkflowJobModel.status == PayflowWorkflowJobStatus.PENDING.value,
            )
            .order_by(PayflowWorkflowJobModel.scheduled_at.asc())
            .first()
        )
        next_planned = None
        if due_step_id:
            # Before a send (message_due) the step being sent is the next one; its job is running, not pending.
            step = next((s for s in steps if s.get("id") == due_step_id), None)
            next_planned = {
                "step_id": due_step_id,
                "kind": (step or {}).get("kind"),
                "at": _utcnow().isoformat(),
                "due_now": True,
            }
        elif pending:
            step = next((s for s in steps if s.get("id") == pending.step_id), None)
            next_planned = {
                "step_id": pending.step_id,
                "kind": (step or {}).get("kind"),
                "at": pending.scheduled_at.isoformat() if pending.scheduled_at else None,
            }

        since = _utcnow() - timedelta(days=30)
        recent = (
            self.db.query(PayflowEngagementEventModel)
            .filter(
                PayflowEngagementEventModel.run_id == run.id,
                PayflowEngagementEventModel.occurred_at >= since,
            )
            .order_by(PayflowEngagementEventModel.occurred_at.desc())
            .limit(50)
            .all()
        )
        messages = (
            self.db.query(PayflowCommunicationModel)
            .filter(PayflowCommunicationModel.run_id == run.id)
            .order_by(PayflowCommunicationModel.id.asc())
            .all()
        )
        decisions = (
            self.db.query(PayflowAdaptiveDecisionModel)
            .filter(PayflowAdaptiveDecisionModel.run_id == run.id)
            .order_by(PayflowAdaptiveDecisionModel.id.desc())
            .limit(10)
            .all()
        )

        email_ok = account.email_consent is not False and bool((account.email or "").strip())
        sms_ok = not bool(account.sms_opt_out) and bool(
            (account.phone_mobile or account.phone_work or "").strip()
        )

        week_ago = _utcnow() - timedelta(days=7)
        sent_7d = (
            self.db.query(PayflowCommunicationModel.id)
            .filter(
                PayflowCommunicationModel.account_id == account.id,
                PayflowCommunicationModel.sent_at.isnot(None),
                PayflowCommunicationModel.sent_at >= week_ago,
            )
            .count()
        )

        def _event_shape(e: PayflowEngagementEventModel) -> dict[str, Any]:
            step_id = None
            if e.communication_id:
                comm = self.db.get(PayflowCommunicationModel, e.communication_id)
                step_id = comm.step_id if comm else None
            return {
                "id": e.id,
                "type": e.event_type,
                "at": e.occurred_at.isoformat() if e.occurred_at else None,
                "channel": e.channel,
                "step_id": step_id,
                "detail": e.detail,
            }

        return {
            "trigger": trigger if trigger in {"event", "timer", "message_due", "manual"} else "event",
            "run": {
                "id": run.id,
                "status": run.status,
                "current_step_id": run.current_step_id,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "strategy_version": run.strategy_version,
                "preferred_channel": run.preferred_channel,
                "swaps": dict(run.swaps or {}),
            },
            "workflow": {
                "id": strategy.id,
                "name": strategy.name,
                "segment": strategy.segment,
                "entry_node_id": strategy.entry_node_id,
                "steps": steps,
                "follow_ups": follow_ups,
            },
            "next_planned": next_planned,
            "account": {
                "days_past_due": account.days_past_due,
                "outstanding_balance": float(account.outstanding_balance or 0),
                "original_balance": float(account.original_balance or 0),
                "recovered_balance": float(account.recovered_balance or 0),
                "collection_status": account.collection_status,
                "payment_status": payment_status(self.db, account, run=run),
                "plan": None,
                "language": account.language,
                "email_ok": email_ok,
                "sms_ok": sms_ok,
            },
            "client": {
                "id": client.id if client else run.client_id,
                "ai_mode": (client.ai_mode if client else None),
                "channels": None,
            },
            "new_events": [_event_shape(e) for e in new_events],
            "recent_events": [_event_shape(e) for e in recent],
            "messages_sent": [
                {
                    "step_id": m.step_id,
                    "channel": m.channel,
                    "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                    "status": m.status,
                    "opened": int(m.opens_count or 0) > int(m.machine_opens_count or 0),
                    "clicked": bool(m.first_clicked_at)
                    or int(m.clicks_count or 0) > 0
                    or (m.status or "") == "Payment Link Clicked",
                }
                for m in messages
            ],
            "decisions": [
                {
                    "action": d.action,
                    "status": d.status,
                    "decided_by": d.decided_by,
                    "at": d.created_at.isoformat() if d.created_at else None,
                    # Not Operations' own note: it may name the customer.
                    "reason": d.reason if d.decided_by != "operations" else None,
                }
                for d in decisions
            ],
            "limits": {
                "max_messages_per_7_days": None,
                "messages_sent_7_days": sent_7d,
            },
        }
