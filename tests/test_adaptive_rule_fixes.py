"""Adaptive workflows: rule fixes (plan segment 3).

1. A follow-up is used up only when its message actually went out - not when it failed, was cancelled or
   was blocked (e.g. by a compliance rule).
2. No contact too soon after a failed payment (2 h) or a part payment (24 h): a next message due sooner waits.
3. An open or a click after a "not opened" / "opened, not paid" / "no response" follow-up was lined up answers
   it: drop_follow_up takes it back, the planned message goes, by the channel used before.
4. The AI sees why it decided before (not Operations' own notes), and cannot drop follow-ups itself.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.core.config import settings
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
from app.modules.payflow.services.workflow_queue import enqueue_execute_step
from app.modules.payflow.services.workflow_suggestion.next_action import decide_next_action
from tests.test_adaptive_next_action import STEPS, _context, _message, _send
from tests.test_adaptive_next_action_ai import _answer, _ask

NOW = datetime.now(timezone.utc)


def _hours_from_now(value):
    return (datetime.fromisoformat(value) - datetime.now(timezone.utc)).total_seconds() / 3600


# --------------------------------------------------------------------------- 1. used only when sent


def test_a_follow_up_whose_message_did_not_go_out_is_not_used_up():
    for status in ("Failed", "Cancelled", "Suppressed"):
        decision = decide_next_action(_context("opened_not_clicked", sent=[{"step_id": "f-opened", "status": status}]))
        assert (decision["action"], decision["follow_up_step_id"]) == ("use_follow_up", "f-opened"), status


def test_a_follow_up_on_its_way_or_delivered_is_used_up():
    for status in ("Queued", "Sent", "Delivered"):
        decision = decide_next_action(_context("opened_not_clicked", sent=[{"step_id": "f-opened", "status": status}]))
        assert decision["action"] == "keep_plan" and "already sent" in decision["reason"], status


# --------------------------------------------------------------------------- 2. waits after a payment


def test_after_a_failed_payment_a_message_due_sooner_waits_two_hours():
    soon = decide_next_action(_context("payment_failed", next_at=NOW + timedelta(hours=1)))
    assert (soon["action"], soon["follow_up_step_id"]) == ("use_follow_up", "f-failed")
    assert 1.9 < _hours_from_now(soon["at"]) <= 2.0
    assert "not sooner than 2 hours" in soon["reason"]

    later = decide_next_action(_context("payment_failed", next_at=NOW + timedelta(hours=3)))
    assert later["action"] == "use_follow_up" and later["at"] is None  # its planned time is fine


def test_after_a_part_payment_a_message_due_sooner_waits_a_day():
    soon = decide_next_action(_context("payment_partial", next_at=NOW + timedelta(hours=5)))
    assert (soon["follow_up_step_id"], round(_hours_from_now(soon["at"]))) == ("f-part", 24)
    later = decide_next_action(_context("payment_partial", next_at=NOW + timedelta(days=2)))
    assert later["at"] is None


def test_the_waits_follow_the_settings_and_do_not_apply_to_other_situations():
    with patch.object(settings, "ADAPTIVE_PAYMENT_FAILED_WAIT_HOURS", 6):
        decision = decide_next_action(_context("payment_failed", next_at=NOW + timedelta(hours=3)))
    assert round(_hours_from_now(decision["at"])) == 6
    assert decide_next_action(_context("opened_not_clicked", next_at=NOW + timedelta(minutes=10)))["at"] is None


# --------------------------------------------------------------------------- 3. an open or a click answers it


STEPS_WITH_QUIET = [*STEPS, _message("f-quiet", "No response", follow_up_for="no_activity")]


def test_an_open_drops_a_lined_up_not_opened_follow_up():
    decision = decide_next_action(_context("email_opened", swaps={"s2": "f-unopened"}))
    assert (decision["action"], decision["replace_step_id"], decision["follow_up_step_id"]) == (
        "drop_follow_up", "s2", "f-unopened")
    assert (decision["decided_by"], decision["needs_approval"]) == ("rules", False)
    assert "opened an email" in decision["reason"] and "'Email 2' goes as planned" in decision["reason"]

    quiet = decide_next_action(_context("email_opened", steps=STEPS_WITH_QUIET, swaps={"s2": "f-quiet"}))
    assert quiet["action"] == "drop_follow_up"


def test_a_click_also_drops_an_opened_not_paid_follow_up_but_an_open_does_not():
    for kind in ("email_clicked", "payment_link_clicked", "payment_page_opened"):
        assert decide_next_action(_context(kind, swaps={"s2": "f-opened"}))["action"] == "drop_follow_up", kind
    assert decide_next_action(_context("email_opened", swaps={"s2": "f-opened"}))["action"] == "keep_plan"


def test_the_mail_apps_own_open_or_a_payment_follow_up_is_not_dropped():
    machine = _context("email_opened", swaps={"s2": "f-unopened"})
    machine["new_events"][0]["detail"] = {"machine_open": True}
    assert decide_next_action(machine)["action"] == "keep_plan"
    for follow_up in ("f-part", "f-failed", "f-clicked"):
        assert decide_next_action(_context("email_clicked", swaps={"s2": follow_up}))["action"] == "keep_plan"


_SEQ = 0


def _run(db, steps, *, current="s1"):
    global _SEQ
    _SEQ += 1
    client = PayflowClientModel(code=f"rf-{_SEQ}", name=f"Rule fixes {_SEQ}", ai_mode="supervised_ai")
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id, customer_name="Jane Doe", customer_first_name="Jane", account_reference=f"RF-{_SEQ}",
        case_reference=f"CASE-RF-{_SEQ}", original_balance=500.0, outstanding_balance=500.0,
        collection_status="Active", email="jane@example.com", phone_mobile="+15551234567",
    )
    strategy = PayflowStrategyModel(code=f"rf-wf-{_SEQ}", name="Rule fixes WF", client_id=client.id,
                                    status="Active", steps=steps, entry_node_id="s1")
    db.add_all([account, strategy])
    db.flush()
    run = PayflowWorkflowRunModel(account_id=account.id, strategy_id=strategy.id, client_id=client.id,
                                  status="Active", current_step_id=current, strategy_version=1)
    db.add(run)
    db.commit()
    return run


def _event(db, run, kind, **detail):
    global _SEQ
    _SEQ += 1
    record_engagement_event(db, account_id=run.account_id, event_type=kind, source="test",
                            dedupe_key=f"rf:{run.id}:{kind}:{_SEQ}", run_id=run.id, detail=detail or None)
    db.commit()


def _planned(db, run, step_id, *, hours):
    """The run's next step, waiting `hours` from now."""
    job = enqueue_execute_step(db, run_id=run.id, step_id=step_id, account_id=run.account_id,
                               strategy_id=run.strategy_id, scheduled_at=datetime.now(timezone.utc) + timedelta(hours=hours))
    db.commit()
    return job


def _hours_until(job):
    at = job.scheduled_at if job.scheduled_at.tzinfo else job.scheduled_at.replace(tzinfo=timezone.utc)
    return (at - datetime.now(timezone.utc)).total_seconds() / 3600


def _decisions(db, run):
    rows = db.query(PayflowAdaptiveDecisionModel).filter_by(run_id=run.id).order_by(PayflowAdaptiveDecisionModel.id)
    return [(d.action, d.status) for d in rows]


# Email 1 → Email 2 → done, with a "not opened" follow-up (no fixed channel) and a "payment failed" one.
WORKER_STEPS = [
    _message("s1", "Email 1", channel="Email", next="s2"),
    _message("s2", "Email 2", channel="Email", next="s3"),
    {"id": "s3", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
    _message("f-unopened", "Not opened", follow_up_for="email_not_opened"),
    _message("f-failed", "Payment failed", follow_up_for="payment_failed"),
]


def test_an_open_takes_the_lined_up_follow_up_back_and_its_sms_switch_with_it(db_session):
    run = _run(db_session, WORKER_STEPS)
    _planned(db_session, run, "s2", hours=48)
    service = AdaptiveWorkflowService(db_session)
    _event(db_session, run, "email_not_opened")
    service.reassess(run.id, trigger="timer")
    db_session.refresh(run)
    assert (run.swaps, run.preferred_channel) == ({"s2": "f-unopened"}, "SMS")

    _event(db_session, run, "email_opened")
    service.reassess(run.id, trigger="event")
    db_session.refresh(run)
    assert (run.swaps, run.preferred_channel) == (None, None)
    assert _decisions(db_session, run) == [("use_follow_up", "superseded"), ("drop_follow_up", "executed")]

    sent = _send(db_session, run, "s2")
    assert [(m.step_id, m.channel) for m in sent] == [("s2", "Email")]  # the planned message, as planned


def test_a_channel_switched_since_is_kept_when_the_follow_up_is_taken_back(db_session):
    run = _run(db_session, WORKER_STEPS)
    _planned(db_session, run, "s2", hours=48)
    service = AdaptiveWorkflowService(db_session)
    _event(db_session, run, "email_not_opened")
    service.reassess(run.id, trigger="timer")
    _event(db_session, run, "email_opened")
    switched = {"action": "switch_channel", "channel": "SMS", "decided_by": "ai", "reason": "test"}
    db_session.add(PayflowAdaptiveDecisionModel(
        run_id=run.id, account_id=run.account_id, strategy_id=run.strategy_id, status="executed", **switched))
    db_session.commit()

    service.reassess(run.id, trigger="event")
    db_session.refresh(run)
    assert (run.swaps, run.preferred_channel) == (None, "SMS")


def test_a_failed_payment_holds_the_next_message_for_two_hours_through_the_worker(db_session):
    run = _run(db_session, WORKER_STEPS)
    job = _planned(db_session, run, "s2", hours=0.5)
    _event(db_session, run, "payment_failed")

    AdaptiveWorkflowService(db_session).reassess(run.id, trigger="event")
    db_session.refresh(run)
    db_session.refresh(job)
    assert run.swaps == {"s2": "f-failed"}
    assert 1.9 < _hours_until(job) <= 2.0


def test_when_a_check_comes_first_the_next_step_is_held_instead(db_session):
    steps = [
        _message("s1", "Email 1", channel="Email", next="c1"),
        {"id": "c1", "kind": "Condition", "title": "Still unpaid?",
         "config": {"attribute": "Payment Status", "operator": "Equals", "value": "Unpaid"}, "yes": "s2", "no": "s3"},
        *WORKER_STEPS[1:],
    ]
    run = _run(db_session, steps)
    check = _planned(db_session, run, "c1", hours=0.5)
    _event(db_session, run, "payment_failed")

    AdaptiveWorkflowService(db_session).reassess(run.id, trigger="event")
    db_session.refresh(check)
    assert 1.9 < _hours_until(check) <= 2.0


def test_a_message_due_right_after_a_failed_payment_is_sent_two_hours_later(db_session):
    run = _run(db_session, WORKER_STEPS, current="s2")
    _event(db_session, run, "payment_failed")

    sent = _send(db_session, run, "s2")  # due now: the send check holds it
    db_session.refresh(run)
    assert sent == []
    assert run.swaps == {"s2": "f-failed"}
    later = (
        db_session.query(PayflowWorkflowJobModel)
        .filter_by(run_id=run.id, step_id="s2", job_type="execute_step", status="pending")
        .one()
    )
    assert 1.9 < _hours_until(later) <= 2.0


# --------------------------------------------------------------------------- 4. what the AI sees and may do


def test_the_ai_sees_why_it_decided_before():
    context = _context("no_activity")
    context["decisions"] = [{"action": "switch_channel", "status": "rejected", "decided_by": "ai",
                             "at": NOW.isoformat(), "reason": "Three emails unopened: try SMS"}]
    _, ai = _ask(context, _answer("keep_plan"))
    assert '"why": "Three emails unopened: try SMS"' in ai.prompts[0]


def test_the_backend_never_passes_operations_notes_to_the_ai(db_session):
    run = _run(db_session, WORKER_STEPS)
    db_session.add_all([
        PayflowAdaptiveDecisionModel(run_id=run.id, account_id=run.account_id, strategy_id=run.strategy_id,
                                     action="move_next", decided_by="operations", status="modified",
                                     reason="Called Jane Doe, she pays Friday"),
        PayflowAdaptiveDecisionModel(run_id=run.id, account_id=run.account_id, strategy_id=run.strategy_id,
                                     action="keep_plan", decided_by="rules", status="executed", reason="No change"),
    ])
    db_session.commit()
    contexts = []
    with patch.object(adaptive_workflow_service, "decide_next_action",
                      side_effect=lambda ctx: contexts.append(ctx) or {"action": "keep_plan", "reason": "x"}):
        AdaptiveWorkflowService(db_session).reassess(run.id, trigger="event")

    reasons = {d["decided_by"]: d["reason"] for d in contexts[0]["decisions"]}
    assert reasons == {"operations": None, "rules": "No change"}


def test_the_ai_cannot_drop_a_follow_up():
    decision, _ = _ask(_context("no_activity", swaps={"s2": "f-unopened"}), _answer("drop_follow_up"))
    assert decision["action"] == "keep_plan"
