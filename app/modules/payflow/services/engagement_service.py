"""Adaptive workflow engagement events (B1) + timer silence signals (B2).

Every provider / portal / payment / unsubscribe hook calls ``record_engagement_event``.
Worker maintenance calls ``emit_adaptive_timer_events`` for open adaptive runs.
Reassessment enqueue is handed off to B4 when ``AdaptiveWorkflowService`` exists.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowCommunicationModel,
    PayflowEngagementEventModel,
    PayflowStrategyModel,
    PayflowWorkflowRunModel,
)
from app.shared.enums import PayflowCommChannel, PayflowCommStatus, PayflowWorkflowRunStatus

logger = logging.getLogger("app.payflow.engagement")

_TERMINAL_RUN_STATUSES = {
    PayflowWorkflowRunStatus.COMPLETED.value,
    PayflowWorkflowRunStatus.CANCELLED.value,
    PayflowWorkflowRunStatus.FAILED.value,
    PayflowWorkflowRunStatus.SUPERSEDED.value,
}

# Delivered is recorded for history but must not wake a run (B4).
_NO_WAKE_EVENT_TYPES = frozenset({"email_delivered", "sms_delivered"})

_FAILED_OR_BLOCKED_COMM_STATUSES = {
    PayflowCommStatus.FAILED.value,
    PayflowCommStatus.SUPPRESSED.value,
    PayflowCommStatus.CANCELLED.value,
}

# Customer-driven signals (exclude provider delivered + timer composites).
_CUSTOMER_ACTIVITY_EVENT_TYPES = frozenset(
    {
        "email_opened",
        "email_clicked",
        "payment_link_clicked",
        "payment_page_opened",
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def record_engagement_event(
    db: Session,
    *,
    account_id: int,
    event_type: str,
    source: str,
    dedupe_key: str,
    occurred_at: datetime | None = None,
    communication_id: int | None = None,
    channel: str | None = None,
    detail: dict[str, Any] | None = None,
    client_id: int | None = None,
    run_id: int | None = None,
    strategy_id: int | None = None,
) -> PayflowEngagementEventModel | None:
    """
    Persist one normalized engagement event.

    Returns the new row, or None when ``dedupe_key`` already exists / account missing.
    Never raises to the caller for duplicate races — callers should still wrap hooks.
    """
    key = (dedupe_key or "").strip()
    if not key or not event_type or not account_id:
        return None

    existing = (
        db.query(PayflowEngagementEventModel.id)
        .filter(PayflowEngagementEventModel.dedupe_key == key)
        .first()
    )
    if existing:
        return None

    account = db.get(PayflowAccountModel, account_id)
    if account is None:
        return None

    resolved_client_id = client_id or account.client_id
    when = occurred_at or _utcnow()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    resolved_run_id = run_id
    resolved_strategy_id = strategy_id
    resolved_channel = channel
    comm: PayflowCommunicationModel | None = None

    if communication_id:
        comm = db.get(PayflowCommunicationModel, communication_id)
        if comm is not None:
            if resolved_run_id is None and comm.run_id:
                resolved_run_id = int(comm.run_id)
            if resolved_strategy_id is None and comm.strategy_id:
                resolved_strategy_id = int(comm.strategy_id)
            if resolved_channel is None and comm.channel:
                resolved_channel = comm.channel
            if resolved_client_id is None:
                resolved_client_id = comm.client_id

    if resolved_run_id is None:
        open_run = _open_run_for_account(db, account_id)
        if open_run is not None:
            resolved_run_id = open_run.id
            if resolved_strategy_id is None:
                resolved_strategy_id = open_run.strategy_id
    elif resolved_strategy_id is None:
        run = db.get(PayflowWorkflowRunModel, resolved_run_id)
        if run is not None:
            resolved_strategy_id = run.strategy_id

    row = PayflowEngagementEventModel(
        client_id=int(resolved_client_id),
        account_id=int(account_id),
        run_id=resolved_run_id,
        strategy_id=resolved_strategy_id,
        communication_id=communication_id,
        event_type=str(event_type).strip(),
        channel=resolved_channel,
        occurred_at=when,
        source=str(source).strip() or "unknown",
        detail=detail,
        dedupe_key=key,
        # Delivered never wakes the run, so it is saved as already read: otherwise it would stay
        # "new" until the next send, and nearly every send would call decide_next_action.
        processed_at=_utcnow() if event_type in _NO_WAKE_EVENT_TYPES else None,
    )
    # SAVEPOINT so a duplicate-key race does not roll back the caller's transaction.
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        if row in db:
            db.expunge(row)
        return None

    _maybe_queue_reassessment(db, row)
    return row


def _open_run_for_account(db: Session, account_id: int) -> PayflowWorkflowRunModel | None:
    return (
        db.query(PayflowWorkflowRunModel)
        .filter(
            PayflowWorkflowRunModel.account_id == account_id,
            PayflowWorkflowRunModel.status.notin_(list(_TERMINAL_RUN_STATUSES)),
        )
        .order_by(PayflowWorkflowRunModel.id.desc())
        .first()
    )


def _maybe_queue_reassessment(db: Session, event: PayflowEngagementEventModel) -> None:
    """Hand off to B4 AdaptiveWorkflowService when present; otherwise no-op."""
    if event.event_type in _NO_WAKE_EVENT_TYPES:
        return
    if not event.run_id:
        return
    try:
        from app.modules.payflow.services.adaptive_workflow_service import (
            AdaptiveWorkflowService,
        )
    except ImportError:
        return
    try:
        AdaptiveWorkflowService(db).queue_reassess_for_event(event)
    except Exception:  # noqa: BLE001
        logger.exception(
            "adaptive reassess queue failed event_id=%s type=%s",
            event.id,
            event.event_type,
        )


def map_provider_event_type(
    *,
    provider_event_type: str,
    channel: str | None,
    is_payment_link_click: bool = False,
) -> str | None:
    """Map SendGrid/Twilio status strings to adaptive engagement event_type values."""
    et = (provider_event_type or "").lower().strip()
    ch = (channel or "").strip()
    is_sms = ch.lower() == "sms"

    if et in {"delivered"}:
        return "sms_delivered" if is_sms else "email_delivered"
    if et in {"open", "opened"}:
        return "email_opened"
    if et in {"click", "clicked"}:
        return "payment_link_clicked" if is_payment_link_click else "email_clicked"
    if et in {"bounce", "bounced", "dropped"}:
        return "email_bounced"
    if et in {"failed", "undelivered"}:
        return "sms_failed" if is_sms else "email_bounced"
    if et in {"unsubscribe", "group_unsubscribe", "spamreport", "spam"}:
        return "email_unsubscribed"
    return None


def safe_record_engagement_event(db: Session, **kwargs: Any) -> PayflowEngagementEventModel | None:
    """Hook-safe wrapper — never raises into webhooks / payment paths."""
    try:
        return record_engagement_event(db, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception(
            "record_engagement_event failed account_id=%s type=%s",
            kwargs.get("account_id"),
            kwargs.get("event_type"),
        )
        return None


def emit_adaptive_timer_events(db: Session) -> dict[str, int]:
    """
    B2 — create silence / non-conversion timer events for open adaptive runs.

    Safe to call repeatedly: ``dedupe_key`` ensures each timer fires once per
    message (or once per no_activity window per run).
    """
    counts = {
        "runs": 0,
        "email_not_opened": 0,
        "opened_not_clicked": 0,
        "clicked_not_paid": 0,
        "no_activity": 0,
    }
    now = _utcnow()
    not_opened_after = timedelta(hours=max(1, int(settings.ADAPTIVE_NOT_OPENED_HOURS or 48)))
    opened_not_clicked_after = timedelta(
        hours=max(1, int(settings.ADAPTIVE_OPENED_NOT_CLICKED_HOURS or 48))
    )
    clicked_not_paid_after = timedelta(
        hours=max(1, int(settings.ADAPTIVE_CLICKED_NOT_PAID_HOURS or 24))
    )
    no_activity_days = max(1, int(settings.ADAPTIVE_NO_ACTIVITY_DAYS or 7))
    no_activity_after = timedelta(days=no_activity_days)

    runs = _open_adaptive_runs(db)
    counts["runs"] = len(runs)
    if not runs:
        return counts

    run_ids = [r.id for r in runs]

    comms = (
        db.query(PayflowCommunicationModel)
        .filter(PayflowCommunicationModel.run_id.in_(run_ids))
        .all()
    )
    comms_by_run: dict[int, list[PayflowCommunicationModel]] = {}
    for comm in comms:
        if comm.run_id is None:
            continue
        comms_by_run.setdefault(int(comm.run_id), []).append(comm)

    account_ids = {r.account_id for r in runs}
    accounts = {
        a.id: a
        for a in db.query(PayflowAccountModel)
        .filter(PayflowAccountModel.id.in_(list(account_ids)))
        .all()
    }

    for run in runs:
        run_comms = comms_by_run.get(run.id, [])
        account = accounts.get(run.account_id)

        for comm in run_comms:
            if _emit_email_not_opened(db, run, comm, now=now, threshold=not_opened_after):
                counts["email_not_opened"] += 1
            if _emit_opened_not_clicked(
                db, run, comm, now=now, threshold=opened_not_clicked_after
            ):
                counts["opened_not_clicked"] += 1
            if _emit_clicked_not_paid(
                db,
                run,
                comm,
                account=account,
                now=now,
                threshold=clicked_not_paid_after,
            ):
                counts["clicked_not_paid"] += 1

        if _emit_no_activity(
            db,
            run,
            run_comms,
            now=now,
            threshold=no_activity_after,
            window_days=no_activity_days,
        ):
            counts["no_activity"] += 1

    return counts


def _open_adaptive_runs(db: Session) -> list[PayflowWorkflowRunModel]:
    q = db.query(PayflowWorkflowRunModel).filter(
        PayflowWorkflowRunModel.status.notin_(list(_TERMINAL_RUN_STATUSES)),
    )
    # B3 adds strategies.adaptive (default True). Until then every open run is eligible.
    if hasattr(PayflowStrategyModel, "adaptive"):
        q = q.join(
            PayflowStrategyModel,
            PayflowStrategyModel.id == PayflowWorkflowRunModel.strategy_id,
        ).filter(PayflowStrategyModel.adaptive.is_(True))
    return q.order_by(PayflowWorkflowRunModel.id.asc()).all()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _human_opens(comm: PayflowCommunicationModel) -> int:
    return max(0, int(comm.opens_count or 0) - int(comm.machine_opens_count or 0))


def _comm_sent_at(comm: PayflowCommunicationModel) -> datetime | None:
    return _aware(comm.sent_at) or _aware(comm.delivered_at)


def _comm_is_trackable_email(comm: PayflowCommunicationModel) -> bool:
    if (comm.channel or "").strip() != PayflowCommChannel.EMAIL.value:
        return False
    if (comm.engagement_tracking or "").strip().lower() == "not_available":
        return False
    if (comm.status or "") in _FAILED_OR_BLOCKED_COMM_STATUSES:
        return False
    if (comm.eligibility_status or "").strip().lower() == "blocked":
        return False
    return True


def _has_click(comm: PayflowCommunicationModel) -> bool:
    if int(comm.clicks_count or 0) > 0:
        return True
    if comm.first_clicked_at or comm.last_clicked_at:
        return True
    if (comm.status or "") == PayflowCommStatus.PAYMENT_LINK_CLICKED.value:
        return True
    return False


def _click_at(comm: PayflowCommunicationModel) -> datetime | None:
    return _aware(comm.first_clicked_at) or _aware(comm.last_clicked_at)


def _emit_email_not_opened(
    db: Session,
    run: PayflowWorkflowRunModel,
    comm: PayflowCommunicationModel,
    *,
    now: datetime,
    threshold: timedelta,
) -> bool:
    if not _comm_is_trackable_email(comm):
        return False
    sent_at = _comm_sent_at(comm)
    if sent_at is None or now - sent_at < threshold:
        return False
    if _human_opens(comm) > 0:
        return False
    row = record_engagement_event(
        db,
        account_id=run.account_id,
        client_id=run.client_id,
        event_type="email_not_opened",
        source="timer",
        dedupe_key=f"timer:email_not_opened:comm:{comm.id}",
        occurred_at=now,
        communication_id=comm.id,
        channel=comm.channel,
        run_id=run.id,
        strategy_id=run.strategy_id,
        detail={
            "sent_at": sent_at.isoformat(),
            "threshold_hours": int(threshold.total_seconds() // 3600),
            "opens_count": int(comm.opens_count or 0),
            "machine_opens_count": int(comm.machine_opens_count or 0),
        },
    )
    return row is not None


def _emit_opened_not_clicked(
    db: Session,
    run: PayflowWorkflowRunModel,
    comm: PayflowCommunicationModel,
    *,
    now: datetime,
    threshold: timedelta,
) -> bool:
    if not _comm_is_trackable_email(comm):
        return False
    if not comm.payment_link:
        return False
    if _human_opens(comm) <= 0:
        return False
    if _has_click(comm):
        return False
    opened_at = _aware(comm.first_opened_at) or _aware(comm.last_opened_at)
    if opened_at is None or now - opened_at < threshold:
        return False
    row = record_engagement_event(
        db,
        account_id=run.account_id,
        client_id=run.client_id,
        event_type="opened_not_clicked",
        source="timer",
        dedupe_key=f"timer:opened_not_clicked:comm:{comm.id}",
        occurred_at=now,
        communication_id=comm.id,
        channel=comm.channel,
        run_id=run.id,
        strategy_id=run.strategy_id,
        detail={
            "opened_at": opened_at.isoformat(),
            "threshold_hours": int(threshold.total_seconds() // 3600),
            "human_opens": _human_opens(comm),
        },
    )
    return row is not None


def _emit_clicked_not_paid(
    db: Session,
    run: PayflowWorkflowRunModel,
    comm: PayflowCommunicationModel,
    *,
    account: PayflowAccountModel | None,
    now: datetime,
    threshold: timedelta,
) -> bool:
    if not _has_click(comm):
        return False
    click_at = _click_at(comm)
    if click_at is None or now - click_at < threshold:
        return False
    if bool(comm.payment_completed):
        return False
    if account and account.last_payment_date:
        try:
            if account.last_payment_date >= click_at.date():
                return False
        except Exception:  # noqa: BLE001
            pass
    # Verified payment engagement after the click also blocks the timer.
    paid_after = (
        db.query(PayflowEngagementEventModel.id)
        .filter(
            PayflowEngagementEventModel.account_id == run.account_id,
            PayflowEngagementEventModel.event_type.in_(
                ["payment_completed", "payment_partial"]
            ),
            PayflowEngagementEventModel.occurred_at >= click_at,
        )
        .first()
    )
    if paid_after:
        return False
    row = record_engagement_event(
        db,
        account_id=run.account_id,
        client_id=run.client_id,
        event_type="clicked_not_paid",
        source="timer",
        dedupe_key=f"timer:clicked_not_paid:comm:{comm.id}",
        occurred_at=now,
        communication_id=comm.id,
        channel=comm.channel,
        run_id=run.id,
        strategy_id=run.strategy_id,
        detail={
            "clicked_at": click_at.isoformat(),
            "threshold_hours": int(threshold.total_seconds() // 3600),
        },
    )
    return row is not None


def _emit_no_activity(
    db: Session,
    run: PayflowWorkflowRunModel,
    run_comms: list[PayflowCommunicationModel],
    *,
    now: datetime,
    threshold: timedelta,
    window_days: int,
) -> bool:
    last_message_at: datetime | None = None
    for comm in run_comms:
        sent_at = _comm_sent_at(comm)
        if sent_at is None:
            continue
        if (comm.status or "") in _FAILED_OR_BLOCKED_COMM_STATUSES:
            continue
        if last_message_at is None or sent_at > last_message_at:
            last_message_at = sent_at
    if last_message_at is None or now - last_message_at < threshold:
        return False

    customer_after = (
        db.query(PayflowEngagementEventModel.id)
        .filter(
            PayflowEngagementEventModel.run_id == run.id,
            PayflowEngagementEventModel.event_type.in_(list(_CUSTOMER_ACTIVITY_EVENT_TYPES)),
            PayflowEngagementEventModel.occurred_at >= last_message_at,
        )
        .first()
    )
    if customer_after:
        return False

    elapsed = now - last_message_at
    window = int(elapsed.total_seconds() // (window_days * 86400))
    if window < 1:
        return False
    row = record_engagement_event(
        db,
        account_id=run.account_id,
        client_id=run.client_id,
        event_type="no_activity",
        source="timer",
        dedupe_key=f"timer:no_activity:run:{run.id}:w{window}",
        occurred_at=now,
        run_id=run.id,
        strategy_id=run.strategy_id,
        detail={
            "last_message_at": last_message_at.isoformat(),
            "window": window,
            "window_days": window_days,
        },
    )
    return row is not None
