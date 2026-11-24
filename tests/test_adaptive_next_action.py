"""Adaptive workflows, AI side, segment 1: the rules in decide_next_action (no AI call).

A known situation → the workflow's approved follow-up, sent in place of the next planned message (same time,
no extra message), each follow-up once; payment started → the next step waits a day; anything else → keep
the plan. Decided by "rules": the follow-ups were approved with the workflow, so no approval per customer.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowAdaptiveDecisionModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowStrategyModel,
    PayflowWorkflowRunModel,
)
from app.modules.payflow.services.adaptive_workflow_service import AdaptiveWorkflowService
from app.modules.payflow.services.engagement_service import record_engagement_event
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.modules.payflow.services.workflow_queue import enqueue_execute_step
from app.modules.payflow.services.workflow_suggestion.next_action import decide_next_action

FIELDS = {"action", "reason", "decided_by", "confidence", "needs_approval", "replace_step_id",
          "follow_up_step_id", "at", "channel"}


def _message(step_id, title, purpose="Payment Reminder", channel=None, follow_up_for=None, **links):
    config = {"purpose": purpose, "subject": title, "body": f"{title} {{{{payment_link}}}}"}
    if channel:
        config["channel"] = channel
    if follow_up_for:
        config["follow_up_for"] = follow_up_for
    return {"id": step_id, "kind": "Communication", "title": title, "config": config, **links}


# Email 1 ─ wait ─ [unpaid?] ─ yes → Email 2 → review / no → paid
STEPS = [
    _message("s1", "Email 1", next="w1"),
    {"id": "w1", "kind": "Wait", "title": "Wait 3 days", "config": {"days": 3}, "next": "c1"},
    {"id": "c1", "kind": "Condition", "title": "Still unpaid?",
     "config": {"attribute": "Payment Status", "operator": "Equals", "value": "Unpaid"}, "yes": "s2", "no": "o1"},
    _message("s2", "Email 2", purpose="Firm Reminder", next="h1"),
    {"id": "h1", "kind": "Human Review", "title": "Review", "config": {"review_reason": "No payment"}},
    {"id": "o1", "kind": "Outcome", "title": "Paid", "config": {"outcome": "Paid In Full"}},
    _message("f-opened", "Opened, not clicked", follow_up_for="opened_not_clicked"),
    _message("f-clicked", "Clicked, not paid", channel="Email", follow_up_for="clicked_not_paid"),
    _message("f-unopened", "Not opened", follow_up_for="email_not_opened"),
    _message("f-part", "Part paid", follow_up_for="payment_partial"),
    _message("f-failed", "Payment failed", follow_up_for="payment_failed"),
]
FOLLOW_UPS = {"opened_not_clicked": "f-opened", "clicked_not_paid": "f-clicked",
              "email_not_opened": "f-unopened", "payment_partial": "f-part", "payment_failed": "f-failed"}


def _context(*event_types, next_step="c1", next_at=None, steps=STEPS, follow_ups=FOLLOW_UPS, sent=(),
             swaps=None, preferred_channel=None, **account):
    now = datetime.now(timezone.utc)
    return {
        "trigger": "event",
        "run": {"id": 1, "status": "Active", "swaps": swaps or {}, "preferred_channel": preferred_channel},
        "workflow": {"id": 5, "steps": steps, "follow_ups": follow_ups},
        "next_planned": {"step_id": next_step, "kind": "Condition",
                         "at": (next_at or now + timedelta(days=2)).isoformat()} if next_step else None,
        "account": {"payment_status": "Unpaid", "email_ok": True, "sms_ok": True, **account},
        "new_events": [{"id": i, "type": kind, "at": now.isoformat(), "step_id": "s1"}
                       for i, kind in enumerate(event_types, start=1)],
        "messages_sent": [{"step_id": "s1", "opened": False, "clicked": False}, *sent],
        "decisions": [],
    }


# --------------------------------------------------------------------------- the rules


def test_opened_not_clicked_sends_its_follow_up_in_place_of_the_next_message():
    decision = decide_next_action(_context("opened_not_clicked"))
    assert decision["action"] == "use_follow_up"
    # next planned is the "still unpaid?" check: the message it leads to is Email 2
    assert (decision["replace_step_id"], decision["follow_up_step_id"]) == ("s2", "f-opened")
    assert (decision["decided_by"], decision["needs_approval"], decision["channel"]) == ("rules", False, None)
    assert "Opened, not clicked" in decision["reason"] and "Email 2" in decision["reason"]


def test_the_next_message_can_be_the_next_step_itself():
    decision = decide_next_action(_context("opened_not_clicked", next_step="s2"))
    assert decision["replace_step_id"] == "s2"


def test_when_a_check_leads_to_a_message_on_each_side_it_is_unclear_so_the_plan_stays():
    steps = [dict(s) for s in STEPS]
    next(s for s in steps if s["id"] == "c1")["no"] = "s1"  # a message on both sides of the check
    decision = decide_next_action(_context("opened_not_clicked", steps=steps))
    assert decision["action"] == "keep_plan"


def test_left_the_payment_page_uses_the_clicked_not_paid_follow_up():
    for kind in ("payment_abandoned", "clicked_not_paid"):
        decision = decide_next_action(_context(kind))
        assert (decision["action"], decision["follow_up_step_id"]) == ("use_follow_up", "f-clicked")


def test_part_payment_uses_its_follow_up_before_anything_else_that_happened():
    decision = decide_next_action(_context("email_opened", "opened_not_clicked", "payment_partial"))
    assert (decision["action"], decision["follow_up_step_id"]) == ("use_follow_up", "f-part")


def test_not_opened_goes_by_sms_first_when_it_can():
    assert decide_next_action(_context("email_not_opened"))["channel"] == "SMS"
    assert decide_next_action(_context("email_not_opened", sms_ok=False))["channel"] is None
    assert decide_next_action(_context("email_not_opened", preferred_channel="SMS"))["channel"] is None
    # a follow-up with a fixed channel is sent on it
    steps = [dict(s, config={**s["config"], "channel": "Email"}) if s["id"] == "f-unopened" else s for s in STEPS]
    assert decide_next_action(_context("email_not_opened", steps=steps))["channel"] is None


def test_each_follow_up_is_used_once():
    sent = decide_next_action(_context("opened_not_clicked", sent=[{"step_id": "f-opened"}]))
    lined_up = decide_next_action(_context("opened_not_clicked", swaps={"s2": "f-opened"}))
    elsewhere = decide_next_action(_context("opened_not_clicked", swaps={"s9": "f-opened"}))
    assert [d["action"] for d in (sent, lined_up, elsewhere)] == ["keep_plan"] * 3
    assert "already sent" in sent["reason"] and "already lined up" in lined_up["reason"]


def test_a_newer_situation_replaces_a_lined_up_follow_up():
    decision = decide_next_action(_context("clicked_not_paid", swaps={"s2": "f-opened"}))
    assert (decision["replace_step_id"], decision["follow_up_step_id"]) == ("s2", "f-clicked")


def test_a_timer_that_is_out_of_date_keeps_the_plan():
    opened = _context("email_not_opened", sent=[{"step_id": "s1", "opened": True}])
    clicked = _context("opened_not_clicked", sent=[{"step_id": "s1", "opened": True, "clicked": True}])
    assert decide_next_action(opened)["action"] == "keep_plan"
    assert decide_next_action(clicked)["action"] == "keep_plan"


def test_without_an_approved_follow_up_the_plan_stays():
    decision = decide_next_action(_context("no_activity"))
    assert decision["action"] == "keep_plan" and "no approved follow-up" in decision["reason"]


def test_plan_customers_and_plan_messages_are_left_alone():
    assert decide_next_action(_context("opened_not_clicked", payment_status="Payment Plan Active"))["action"] == "keep_plan"
    steps = [dict(s, config={**s["config"], "purpose": "Payment Plan Reminder"}) if s["id"] == "s2" else s
             for s in STEPS]
    decision = decide_next_action(_context("opened_not_clicked", steps=steps))
    assert decision["action"] == "keep_plan" and "stays as planned" in decision["reason"]


def test_a_follow_up_that_cannot_reach_the_customer_is_not_used():
    decision = decide_next_action(_context("clicked_not_paid", email_ok=False))  # f-clicked is Email only
    assert decision["action"] == "keep_plan"


def test_nothing_after_the_check_keeps_the_plan():
    decision = decide_next_action(_context("opened_not_clicked", next_step=None))
    assert decision["action"] == "keep_plan" and "no planned message" in decision["reason"]


def test_paid_or_plan_set_up_keeps_the_plan():
    for kind in ("payment_completed", "plan_created"):
        assert decide_next_action(_context("opened_not_clicked", kind))["action"] == "keep_plan"


def test_a_payment_that_did_not_go_through_gets_its_try_again_follow_up():
    decision = decide_next_action(_context("opened_not_clicked", "payment_failed"))
    assert (decision["action"], decision["replace_step_id"], decision["follow_up_step_id"]) == (
        "use_follow_up", "s2", "f-failed")
    assert decision["reason"].startswith("Customer's payment did not go through")
    # A part payment in the same check comes first; once used, the try-again message is not sent twice.
    assert decide_next_action(_context("payment_failed", "payment_partial"))["follow_up_step_id"] == "f-part"
    again = decide_next_action(_context("payment_failed", sent=[{"step_id": "f-failed"}]))
    assert again["action"] == "keep_plan" and "already sent" in again["reason"]


def test_payment_started_holds_a_step_due_within_a_day():
    now = datetime.now(timezone.utc)
    soon = decide_next_action(_context("payment_started", next_at=now + timedelta(hours=3)))
    assert soon["action"] == "move_next"
    held_until = datetime.fromisoformat(soon["at"])
    assert now + timedelta(hours=23) < held_until <= now + timedelta(hours=24, minutes=1)

    later = decide_next_action(_context("payment_started", next_at=now + timedelta(days=2)))
    assert later["action"] == "keep_plan"
    old = _context("payment_started", next_at=now + timedelta(hours=3))
    old["new_events"][0]["at"] = (now - timedelta(days=2)).isoformat()
    assert decide_next_action(old)["action"] == "keep_plan"


def test_nothing_new_or_only_opens_keeps_the_plan():
    assert decide_next_action(_context())["action"] == "keep_plan"
    decision = decide_next_action(_context("email_opened", "payment_link_clicked"))
    assert decision["action"] == "keep_plan" and "email_opened" in decision["reason"]


def test_every_answer_has_all_the_fields_and_a_broken_context_keeps_the_plan():
    for context in (_context(), _context("opened_not_clicked"), _context("payment_started"), {},
                    {"new_events": [{"type": "opened_not_clicked"}], "workflow": None, "next_planned": "x"}):
        decision = decide_next_action(context)
        assert set(decision) == FIELDS
        assert decision["decided_by"] == "rules" and decision["needs_approval"] is False


# --------------------------------------------------------------------------- with the worker

_SEQ = 0


def _run(db, *, ai_mode="supervised_ai", follow_up_channel="Email", current="s2"):
    """Email 1 → Email 2 → done, with an "opened, not clicked" and a "not opened" follow-up."""
    global _SEQ
    _SEQ += 1
    client = PayflowClientModel(code=f"nba-{_SEQ}", name=f"NBA {_SEQ}", ai_mode=ai_mode)
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id, customer_name="Jane Doe", customer_first_name="Jane",
        account_reference=f"NBA-{_SEQ}", case_reference=f"CASE-NBA-{_SEQ}", original_balance=500.0,
        outstanding_balance=500.0, collection_status="Active", email="jane@example.com",
        phone_mobile="+15551234567",
    )
    steps = [
        _message("s1", "Email 1", channel="Email", next="s2"),
        _message("s2", "Email 2", channel="Email", next="s3"),
        {"id": "s3", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
        _message("f1", "Opened, not clicked", channel=follow_up_channel, follow_up_for="opened_not_clicked"),
        _message("f2", "Not opened", follow_up_for="email_not_opened"),
    ]
    strategy = PayflowStrategyModel(code=f"nba-wf-{_SEQ}", name="NBA WF", client_id=client.id, status="Active",
                                    steps=steps, entry_node_id="s1")
    db.add_all([account, strategy])
    db.flush()
    run = PayflowWorkflowRunModel(account_id=account.id, strategy_id=strategy.id, client_id=client.id,
                                  status="Active", current_step_id=current, strategy_version=1)
    db.add(run)
    db.commit()
    return run


def _event(db, run, kind):
    record_engagement_event(db, account_id=run.account_id, event_type=kind, source="test",
                            dedupe_key=f"nba:{run.id}:{kind}", run_id=run.id)
    db.commit()


def _send(db, run, step_id):
    with patch.object(WorkflowExecutionService, "_payment_link", return_value=""):
        WorkflowExecutionService(db).execute_step(run.id, step_id)
    return db.query(PayflowCommunicationModel).filter(PayflowCommunicationModel.run_id == run.id).all()


def test_the_follow_up_is_sent_in_the_planned_slot_without_approval(db_session):
    """Supervised client: a rules decision (an approved follow-up) is carried out, not held for approval."""
    run = _run(db_session)
    enqueue_execute_step(db_session, run_id=run.id, step_id="s2", account_id=run.account_id,
                         strategy_id=run.strategy_id, scheduled_at=datetime.now(timezone.utc) + timedelta(days=2))
    _event(db_session, run, "opened_not_clicked")

    AdaptiveWorkflowService(db_session).reassess(run.id, trigger="timer")
    db_session.refresh(run)
    decision = db_session.query(PayflowAdaptiveDecisionModel).filter_by(run_id=run.id).one()
    assert (decision.action, decision.status, decision.decided_by) == ("use_follow_up", "executed", "rules")
    assert run.swaps == {"s2": "f1"}

    sent = _send(db_session, run, "s2")
    db_session.refresh(run)
    assert [(m.step_id, m.subject) for m in sent] == [("f1", "Opened, not clicked")]
    assert not run.swaps  # used once


def test_at_send_time_the_message_being_sent_is_the_one_replaced(db_session):
    """The situation is still unread when Email 2 is due: the send check swaps Email 2 itself."""
    run = _run(db_session)
    _event(db_session, run, "opened_not_clicked")

    sent = _send(db_session, run, "s2")
    assert [m.step_id for m in sent] == ["f1"]


def test_without_a_situation_the_planned_message_is_sent(db_session):
    run = _run(db_session)
    _event(db_session, run, "email_opened")

    sent = _send(db_session, run, "s2")
    assert [m.step_id for m in sent] == ["s2"]


def test_a_customer_who_does_not_open_emails_gets_the_follow_up_by_sms(db_session):
    run = _run(db_session)
    _event(db_session, run, "email_not_opened")

    sent = _send(db_session, run, "s2")
    db_session.refresh(run)
    assert run.preferred_channel == "SMS"
    assert [(m.step_id, m.channel) for m in sent] == [("f2", "SMS")]
