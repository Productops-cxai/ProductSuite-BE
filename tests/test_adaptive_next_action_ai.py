"""Adaptive workflows, AI side, segment 2: the AI where the rules cannot settle it.

Asked only for: a situation with no follow-up left to use, an unclear next message, or "Reassess plan".
Never at send time, at most 3 times a week per run. It picks one of a fixed list of actions; an answer that
does not fit becomes "keep the plan". It never sees names, contact details or message texts.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.core.exceptions import ServiceUnavailableError
from app.infrastructure.database.models import PayflowAdaptiveDecisionModel
from app.modules.payflow.services.adaptive_workflow_service import AdaptiveWorkflowService
from app.modules.payflow.services.workflow_suggestion import next_action_ai
from app.modules.payflow.services.workflow_suggestion.next_action import decide_next_action
from tests.test_adaptive_next_action import STEPS, _context, _event, _run

NOW = datetime.now(timezone.utc)


class FakeAI:
    """Stands in for call_llm: answers `answer` and keeps what it was sent."""

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.prompts = answer, error, []

    def __call__(self, messages, schema, *, quick=False):
        assert quick is True  # a short call: no waiting out rate limits
        self.prompts.append(messages[-1]["content"])
        if self.error:
            raise self.error
        return self.answer

    @property
    def asked(self):
        return len(self.prompts)


def _ask(context, answer=None, error=None):
    fake = FakeAI(answer, error)
    with patch.object(next_action_ai, "call_llm", fake):
        return decide_next_action(context), fake


def _answer(action, reason="Several emails went unopened", confidence=80, **fields):
    return {"action": action, "reason": reason, "confidence": confidence, **fields}


# --------------------------------------------------------------------------- when the AI is asked


def test_the_ai_is_asked_when_no_follow_up_is_left():
    decision, ai = _ask(_context("no_activity"), _answer("switch_channel", channel="SMS"))
    assert ai.asked == 1 and "no approved follow-up" in ai.prompts[0]
    assert (decision["action"], decision["channel"]) == ("switch_channel", "SMS")
    assert (decision["decided_by"], decision["confidence"], decision["needs_approval"]) == ("ai", 80, False)


def test_a_second_time_the_same_thing_happens_the_ai_is_asked():
    context = _context("opened_not_clicked", sent=[{"step_id": "f-opened"}])  # its follow-up was used
    decision, ai = _ask(context, _answer("to_operations", reason="Opens every email, never pays"))
    assert ai.asked == 1 and decision["action"] == "to_operations"


def test_the_ai_is_not_asked_when_the_rules_are_sure():
    for context in (
        _context("opened_not_clicked"),  # its follow-up is used
        _context("email_opened"),
        _context("payment_started"),
        _context("plan_created"),
        _context("opened_not_clicked", payment_status="Payment Plan Active"),
        _context(),
    ):
        _, ai = _ask(context, _answer("to_operations"))
        assert ai.asked == 0


def test_never_at_send_time():
    context = _context("no_activity")
    context["trigger"] = "message_due"
    decision, ai = _ask(context, _answer("to_operations"))
    assert ai.asked == 0 and decision["action"] == "keep_plan"


def test_at_most_three_times_a_week_per_run_but_reassess_plan_always_asks():
    context = _context("no_activity")
    recent = [{"action": "keep_plan", "decided_by": "ai", "at": (NOW - timedelta(days=d)).isoformat()} for d in (1, 2, 3)]
    context["decisions"] = recent
    assert _ask(context, _answer("to_operations"))[1].asked == 0

    context["decisions"] = [*recent[:2], {"decided_by": "ai", "at": (NOW - timedelta(days=8)).isoformat()}]
    assert _ask(context, _answer("to_operations"))[1].asked == 1  # an older one no longer counts

    manual = _context("reassess_requested")
    manual["trigger"], manual["decisions"] = "manual", recent
    assert _ask(manual, _answer("to_operations"))[1].asked == 1


def test_reassess_plan_asks_the_ai_to_look_at_everything():
    context = _context("reassess_requested")
    context["trigger"] = "manual"
    decision, ai = _ask(context, _answer("recheck_at", reason="Opened twice today, may pay soon", in_hours=12))
    assert ai.asked == 1
    assert decision["action"] == "recheck_at" and decision["decided_by"] == "ai"
    at = datetime.fromisoformat(decision["at"])
    assert NOW + timedelta(hours=11) < at < NOW + timedelta(hours=13)


def test_the_ai_picks_the_message_when_the_next_one_is_unclear():
    steps = [dict(s) for s in STEPS]
    next(s for s in steps if s["id"] == "c1")["no"] = "s1"  # a message on both sides of the check
    answer = _answer("use_follow_up", follow_up_step_id="f-opened", replace_step_id="s2")
    decision, ai = _ask(_context("opened_not_clicked", steps=steps), answer)
    assert ai.asked == 1 and "unclear which message" in ai.prompts[0]
    assert (decision["action"], decision["replace_step_id"], decision["follow_up_step_id"]) == (
        "use_follow_up", "s2", "f-opened")


# --------------------------------------------------------------------------- checking the answer


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (_answer("stop"), "not an action"),
        (_answer("pause"), "not an action"),
        (_answer("use_follow_up", follow_up_step_id="f-nope", replace_step_id="s2"), "not available"),
        (_answer("use_follow_up", follow_up_step_id="f-opened", replace_step_id="s1"), "not the next one"),
        (_answer("switch_channel", channel="SMS"), "cannot be reached"),  # sms_ok is False below
        (_answer("switch_channel", channel="Email"), "already used first"),  # preferred is Email below
        (_answer("move_next", in_hours=2), "too soon"),  # the last message went 1 hour ago
        (_answer("move_next", in_hours=2000), "hours from now"),
        (_answer("recheck_at", in_hours=0), "hours from now"),
        (_answer("to_operations", reason="  "), "no reason"),
        (_answer("to_operations", confidence=30), "not sure"),
    ],
)
def test_an_answer_that_does_not_fit_keeps_the_plan(answer, why):
    context = _context("no_activity", sms_ok=False, preferred_channel="Email",
                       sent=[{"step_id": "s1", "sent_at": (NOW - timedelta(hours=1)).isoformat()}])
    decision, ai = _ask(context, answer)
    assert ai.asked == 1
    assert decision["action"] == "keep_plan" and decision["decided_by"] == "ai"
    assert why in decision["reason"]


def test_a_move_later_is_fine_and_sooner_waits_a_day_after_the_last_message():
    sent = [{"step_id": "s1", "sent_at": (NOW - timedelta(hours=30)).isoformat()}]
    later, _ = _ask(_context("no_activity", sent=sent), _answer("move_next", in_hours=96))
    sooner, _ = _ask(_context("no_activity", sent=sent), _answer("move_next", in_hours=6))  # 36 h after the last
    assert later["action"] == "move_next" and sooner["action"] == "move_next"


def test_when_the_ai_cannot_be_asked_the_rules_answer_stands():
    decision, ai = _ask(_context("no_activity"), error=ServiceUnavailableError("down"))
    assert ai.asked == 1
    assert (decision["action"], decision["decided_by"]) == ("keep_plan", "rules")
    assert "the AI could not be asked" in decision["reason"]
    assert set(decision) == {"action", "reason", "decided_by", "confidence", "needs_approval", "replace_step_id",
                             "follow_up_step_id", "at", "channel"}


def test_the_ai_never_sees_names_contact_details_or_message_texts():
    steps = [dict(s, config={**s["config"], "body": "SECRET TEXT for Jane"}) for s in STEPS]
    context = _context("no_activity", steps=steps, email="jane@example.com", phone="+15551234567",
                       customer_name="Jane Doe")
    _, ai = _ask(context, _answer("keep_plan"))
    prompt = ai.prompts[0]
    for private in ("SECRET TEXT", "jane@example.com", "+15551234567", "Jane"):
        assert private not in prompt
    assert '"next_message_choices"' in prompt and "Email 2" in prompt  # titles only


# --------------------------------------------------------------------------- with the worker


def _decide_with_ai(db, ai_mode, answer):
    run = _run(db, ai_mode=ai_mode)
    _event(db, run, "no_activity")  # this workflow has no "no activity" follow-up: the AI is asked
    with patch.object(next_action_ai, "call_llm", FakeAI(answer)):
        AdaptiveWorkflowService(db).reassess(run.id, trigger="timer")
    db.refresh(run)
    return run, db.query(PayflowAdaptiveDecisionModel).filter_by(run_id=run.id).one()


def test_a_supervised_clients_ai_change_waits_for_approval_and_the_plan_goes_on(db_session):
    run, decision = _decide_with_ai(db_session, "supervised_ai", _answer("switch_channel", channel="SMS"))
    assert (decision.decided_by, decision.status) == ("ai", "awaiting_approval")
    assert run.preferred_channel is None and run.status == "Active"


def test_an_autopilot_clients_ai_change_is_made(db_session):
    run, decision = _decide_with_ai(db_session, "autopilot", _answer("switch_channel", channel="SMS"))
    assert (decision.decided_by, decision.status) == ("ai", "executed")
    assert run.preferred_channel == "SMS"
