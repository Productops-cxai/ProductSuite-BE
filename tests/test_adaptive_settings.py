"""Adaptive workflows: timings, the AI limit and the "Follow-up" stage (plan segment 2).

1. "No activity" fires after ADAPTIVE_NO_ACTIVITY_HOURS (72 h) of silence after the last message - and again
   after a later message's silence (a repeat, not a duplicate of the first).
2. The AI is asked at most ADAPTIVE_AI_CALLS_PER_WEEK times a week per run.
3. A follow-up's email is recorded as reminder stage "Follow-up", so its results are kept apart from the
   reminders' in Subject Performance; the flowchart's messages keep their stages.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.core.config import settings
from app.infrastructure.database.models import PayflowCommunicationModel, PayflowEngagementEventModel
from app.modules.payflow.services.email_tracking import REMINDER_STAGES, reminder_stage
from app.modules.payflow.services.engagement_service import emit_adaptive_timer_events
from tests.test_adaptive_next_action import _event, _run, _send
from tests.test_adaptive_next_action_ai import NOW, _answer, _ask, _context

_SEQ = 0


def _sent(db, run, hours_ago):
    global _SEQ
    _SEQ += 1
    db.add(PayflowCommunicationModel(
        code=f"COM-ADS-{_SEQ}", client_id=run.client_id, account_id=run.account_id, run_id=run.id,
        channel="SMS", purpose="Payment Reminder", status="Sent",
        sent_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    ))
    db.commit()


def _silences(db, run):
    return (
        db.query(PayflowEngagementEventModel)
        .filter(PayflowEngagementEventModel.run_id == run.id, PayflowEngagementEventModel.event_type == "no_activity")
        .all()
    )


# --------------------------------------------------------------------------- 1. no activity: 72 hours


def test_no_activity_fires_after_72_hours_of_silence_not_before(db_session):
    run = _run(db_session)
    _sent(db_session, run, hours_ago=71)
    emit_adaptive_timer_events(db_session)
    assert _silences(db_session, run) == []

    quiet = _run(db_session)
    _sent(db_session, quiet, hours_ago=73)
    emit_adaptive_timer_events(db_session)
    emit_adaptive_timer_events(db_session)  # safe to repeat: still one event
    silences = _silences(db_session, quiet)
    assert len(silences) == 1 and silences[0].detail["window_hours"] == 72


def test_a_silence_after_a_later_message_is_a_new_event(db_session):
    """Each message starts a new silence: the "No Engagement" repeat text needs the second one."""
    run = _run(db_session)
    _sent(db_session, run, hours_ago=100)
    emit_adaptive_timer_events(db_session)
    _sent(db_session, run, hours_ago=73)  # e.g. the follow-up, also unanswered
    emit_adaptive_timer_events(db_session)

    assert len(_silences(db_session, run)) == 2


def test_the_silence_window_follows_the_setting(db_session):
    run = _run(db_session)
    _sent(db_session, run, hours_ago=30)
    with patch.object(settings, "ADAPTIVE_NO_ACTIVITY_HOURS", 24):
        emit_adaptive_timer_events(db_session)
    assert len(_silences(db_session, run)) == 1


# --------------------------------------------------------------------------- 2. the AI limit


def test_the_weekly_ai_limit_follows_the_setting():
    context = _context("no_activity")
    context["decisions"] = [{"action": "keep_plan", "decided_by": "ai", "at": (NOW - timedelta(days=1)).isoformat()}]

    with patch.object(settings, "ADAPTIVE_AI_CALLS_PER_WEEK", 1):
        assert _ask(context, _answer("to_operations"))[1].asked == 0
    with patch.object(settings, "ADAPTIVE_AI_CALLS_PER_WEEK", 2):
        assert _ask(context, _answer("to_operations"))[1].asked == 1


# --------------------------------------------------------------------------- 3. the "Follow-up" stage


def test_follow_ups_have_their_own_reminder_stage():
    steps = [
        {"id": "s1", "kind": "Communication", "config": {"purpose": "Payment Reminder"}},
        {"id": "s2", "kind": "Communication", "config": {"purpose": "Payment Reminder"}},
        {"id": "s3", "kind": "Communication", "config": {"purpose": "Payment Reminder"}},
        {"id": "fu", "kind": "Communication", "config": {"purpose": "Payment Reminder", "follow_up_for": "no_activity"}},
    ]
    stages = {step["id"]: reminder_stage(steps, step, "Payment Reminder") for step in steps}
    assert stages == {"s1": "First reminder", "s2": "Second reminder", "s3": "Final reminder", "fu": "Follow-up"}
    assert "Follow-up" in REMINDER_STAGES  # a Subject Performance filter value


def test_a_sent_follow_up_is_recorded_as_a_follow_up(db_session):
    run = _run(db_session)
    _event(db_session, run, "opened_not_clicked")
    followed = _send(db_session, run, "s2")  # Email 2 is replaced by its follow-up

    planned = _run(db_session)
    sent = _send(db_session, planned, "s2")  # nothing happened: Email 2 itself goes

    assert [(m.step_id, m.reminder_stage) for m in followed] == [("f1", "Follow-up")]
    assert [(m.step_id, m.reminder_stage) for m in sent] == [("s2", "Second reminder")]
