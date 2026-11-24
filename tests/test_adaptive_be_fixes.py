"""Fixes to the adaptive workflow BE (found before the AI part starts).

1. A customer who unsubscribed from email: the "switch to SMS" safety rule fired at every reassessment
   (email_consent stays False), so decide_next_action was never called and follow-up swaps were dropped.
2. A case closed with money owed was stopped as "paid in full" (Completed) instead of ending as Cancelled.
3. A waiting reassessment due later (a recheck days ahead, or the 5-minute batch) held back urgent events.
4. "Delivered" events stayed unread, so nearly every send called decide_next_action and saved a decision.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowAdaptiveDecisionModel,
    PayflowClientModel,
    PayflowStrategyModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
)
from app.modules.payflow.services import adaptive_workflow_service
from app.modules.payflow.services.adaptive_workflow_service import AdaptiveWorkflowService
from app.modules.payflow.services.engagement_service import record_engagement_event
from app.modules.payflow.services.workflow_queue import enqueue_reassess_run

STEPS = [
    {"id": "s1", "kind": "Communication", "title": "Email 1",
     "config": {"channel": "Email", "purpose": "Payment Reminder", "body": "Hi"}, "next": "s2"},
    {"id": "s2", "kind": "Communication", "title": "Email 2",
     "config": {"channel": "Email", "purpose": "Payment Reminder", "body": "Hi again"}, "next": "s3"},
    {"id": "s3", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
    {"id": "f1", "kind": "Communication", "title": "Opened, not clicked",
     "config": {"channel": "Email", "purpose": "Payment Reminder", "body": "You opened our email",
                "follow_up_for": "opened_not_clicked"}},
]
KEEP = {"action": "keep_plan", "decided_by": "rules", "reason": "No change"}

_SEQ = 0


def _run(db, *, email_consent=True, outstanding=500.0, status="Active"):
    global _SEQ
    _SEQ += 1
    client = PayflowClientModel(code=f"adapt-{_SEQ}", name=f"Adaptive {_SEQ}", ai_mode="autopilot")
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id, customer_name="Test User", account_reference=f"AD-{_SEQ}",
        case_reference=f"CASE-AD-{_SEQ}", original_balance=500.0, outstanding_balance=outstanding,
        collection_status=status, email="a@example.com", phone_mobile="+15551234567",
        email_consent=email_consent,
    )
    strategy = PayflowStrategyModel(code=f"adapt-wf-{_SEQ}", name="Adaptive WF", client_id=client.id,
                                    status="Active", steps=STEPS, entry_node_id="s1")
    db.add_all([account, strategy])
    db.flush()
    run = PayflowWorkflowRunModel(account_id=account.id, strategy_id=strategy.id, client_id=client.id,
                                  status="Active", current_step_id="s1", strategy_version=1)
    db.add(run)
    db.commit()
    return run


def _event(db, run, event_type, key):
    row = record_engagement_event(db, account_id=run.account_id, event_type=event_type, source="test",
                                  dedupe_key=key, run_id=run.id)
    db.commit()
    return row


def _decisions(db, run):
    return (
        db.query(PayflowAdaptiveDecisionModel)
        .filter(PayflowAdaptiveDecisionModel.run_id == run.id)
        .order_by(PayflowAdaptiveDecisionModel.id)
        .all()
    )


def _pending_reassess(db, run):
    return (
        db.query(PayflowWorkflowJobModel)
        .filter(
            PayflowWorkflowJobModel.run_id == run.id,
            PayflowWorkflowJobModel.job_type == "reassess_run",
            PayflowWorkflowJobModel.status == "pending",
        )
        .all()
    )


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _next_action(calls, answer=KEEP):
    """Stand-in for decide_next_action that records each call."""
    return patch.object(
        adaptive_workflow_service, "decide_next_action", side_effect=lambda ctx: calls.append(ctx) or answer
    )


# --------------------------------------------------------------------------- 1. unsubscribed customer


def test_unsubscribed_customer_switches_to_sms_once_then_reaches_next_action(db_session):
    run = _run(db_session, email_consent=False)
    calls = []
    with _next_action(calls):
        service = AdaptiveWorkflowService(db_session)
        _event(db_session, run, "email_opened", "fix1:a")
        service.reassess(run.id, trigger="event")
        _event(db_session, run, "email_opened", "fix1:b")
        service.reassess(run.id, trigger="event")

    db_session.refresh(run)
    assert run.preferred_channel == "SMS"
    assert [(d.action, d.decided_by) for d in _decisions(db_session, run)] == [
        ("switch_channel", "safety"),  # the first time only
        ("keep_plan", "rules"),  # then decide_next_action is asked as usual
    ]
    assert len(calls) == 1


def test_a_follow_up_swap_survives_the_send_check_of_an_unsubscribed_customer(db_session):
    run = _run(db_session, email_consent=False)
    run.preferred_channel = "SMS"  # switched earlier
    run.swaps = {"s2": "f1"}
    swap = PayflowAdaptiveDecisionModel(
        run_id=run.id, account_id=run.account_id, strategy_id=run.strategy_id, action="use_follow_up",
        replace_step_id="s2", follow_up_step_id="f1", decided_by="rules", status="executed",
    )
    db_session.add(swap)
    db_session.commit()

    with _next_action([]):
        AdaptiveWorkflowService(db_session).reassess(run.id, trigger="message_due", due_step_id="s2")

    db_session.refresh(run)
    db_session.refresh(swap)
    assert run.swaps == {"s2": "f1"}
    assert swap.status == "executed"
    assert [d.action for d in _decisions(db_session, run)] == ["use_follow_up"]  # nothing new saved


# --------------------------------------------------------------------------- 2. closed case, money owed


def test_a_closed_case_with_money_owed_is_not_stopped_as_paid(db_session):
    run = _run(db_session, status="Resolved", outstanding=500.0)
    calls = []
    with _next_action(calls):
        _event(db_session, run, "email_opened", "fix2:open")
        AdaptiveWorkflowService(db_session).reassess(run.id, trigger="event")

    db_session.refresh(run)
    assert run.status == "Active"  # the engine ends it as Cancelled at its next step
    assert [d.action for d in _decisions(db_session, run)] == ["keep_plan"]
    assert len(calls) == 1


def test_nothing_owed_still_stops_the_run_without_touching_the_account(db_session):
    run = _run(db_session, outstanding=0.0)
    with _next_action([]):
        _event(db_session, run, "email_opened", "fix2:paid")
        AdaptiveWorkflowService(db_session).reassess(run.id, trigger="event")

    db_session.refresh(run)
    account = db_session.get(PayflowAccountModel, run.account_id)
    assert run.status == "Completed"
    assert [(d.action, d.decided_by) for d in _decisions(db_session, run)] == [("stop", "safety")]
    assert account.collection_status == "Active"


# --------------------------------------------------------------------------- 3. one waiting check, moved up


def test_an_urgent_event_moves_a_later_recheck_up(db_session):
    run = _run(db_session)
    in_three_days = datetime.now(timezone.utc) + timedelta(days=3)
    enqueue_reassess_run(db_session, run_id=run.id, scheduled_at=in_three_days, payload={"decision_id": 7})
    db_session.commit()

    _event(db_session, run, "payment_partial", "fix3:partial")

    jobs = _pending_reassess(db_session, run)
    assert len(jobs) == 1
    assert _aware(jobs[0].scheduled_at) <= datetime.now(timezone.utc)
    assert jobs[0].payload["event_type"] == "payment_partial"
    assert jobs[0].payload["decision_id"] == 7  # what was there is kept


def test_an_urgent_event_does_not_wait_for_the_five_minute_batch(db_session):
    run = _run(db_session)
    _event(db_session, run, "email_opened", "fix3:open")  # batched: in 5 minutes
    assert _aware(_pending_reassess(db_session, run)[0].scheduled_at) > datetime.now(timezone.utc)

    _event(db_session, run, "email_unsubscribed", "fix3:unsub")

    jobs = _pending_reassess(db_session, run)
    assert len(jobs) == 1
    assert _aware(jobs[0].scheduled_at) <= datetime.now(timezone.utc)


def test_a_later_request_does_not_push_a_waiting_check_back(db_session):
    run = _run(db_session)
    now = datetime.now(timezone.utc)
    enqueue_reassess_run(db_session, run_id=run.id, scheduled_at=now, payload={"trigger": "event"})
    later = enqueue_reassess_run(
        db_session, run_id=run.id, scheduled_at=now + timedelta(days=3), payload={"trigger": "timer"}
    )
    db_session.commit()

    jobs = _pending_reassess(db_session, run)
    assert len(jobs) == 1 and later.id == jobs[0].id
    assert _aware(jobs[0].scheduled_at) <= now + timedelta(seconds=1)
    assert jobs[0].payload == {"trigger": "event"}


# --------------------------------------------------------------------------- 4. delivered events


def test_a_delivered_event_does_not_make_the_send_check_call_next_action(db_session):
    run = _run(db_session)
    delivered = _event(db_session, run, "email_delivered", "fix4:delivered")
    assert delivered.processed_at is not None
    assert _pending_reassess(db_session, run) == []  # still does not wake the run

    calls = []
    with _next_action(calls):
        result = AdaptiveWorkflowService(db_session).reassess(run.id, trigger="message_due", due_step_id="s2")

    assert result["abort_send"] is False
    assert calls == []
    assert _decisions(db_session, run) == []
