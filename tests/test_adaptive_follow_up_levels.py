"""Adaptive workflows: first-time and repeat follow-ups, and "plan setup not finished" (plan segment 4).

1. Each situation may have a first-time text and a repeat text (config.follow_up_level 2): the first time the
   first-time text, when it happens again the repeat text, then the AI may look (timing, channel, Operations).
2. "Plan setup not finished": 24 h after the customer began setting up a payment plan without finishing it (a
   timer), or when they leave that checkout. Workflows without that follow-up use "payment not finished".
3. Silence after a part payment's first follow-up gets the part payment's repeat text.
4. Setting up a plan answers a lined-up "payment not finished" / "plan setup" follow-up: it is dropped.
5. Steps keep their level when saved; the backend's follow-up map stays first-time only.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.core.exceptions import ValidationAppError
from app.infrastructure.database.models import PayflowCommunicationModel, PayflowEngagementEventModel
from app.modules.payflow.schemas import StrategyNodeConfig
from app.modules.payflow.services import adaptive_workflow_service
from app.modules.payflow.services.adaptive_workflow_service import AdaptiveWorkflowService
from app.modules.payflow.services.engagement_service import emit_adaptive_timer_events, record_engagement_event
from app.modules.payflow.services.strategy_service import _normalize_step, follow_up_level
from app.modules.payflow.services.workflow_suggestion.next_action import decide_next_action
from tests.test_adaptive_next_action import STEPS, _context, _message, _send
from tests.test_adaptive_next_action_ai import _answer, _ask
from tests.test_adaptive_rule_fixes import _event, _run

NOW = datetime.now(timezone.utc)


def _step(step_id, steps=STEPS):
    return next(step for step in steps if step["id"] == step_id)


def _repeat(step):
    """The same situation's repeat text."""
    return {**step, "id": f"{step['id']}-2", "title": f"{step['title']} (repeat)",
            "config": {**step["config"], "follow_up_level": 2}}


LEVEL_STEPS = [
    *STEPS,
    _repeat(_step("f-opened")),
    _repeat(_step("f-part")),
    _message("f-plan", "Plan setup not finished", follow_up_for="plan_setup_not_finished"),
    _message("f-quiet", "No response", follow_up_for="no_activity"),
]


def _levels(*event_types, **kwargs):
    return _context(*event_types, steps=LEVEL_STEPS, **kwargs)


def _sent(*step_ids, status="Sent"):
    return [{"step_id": step_id, "status": status} for step_id in step_ids]


# --------------------------------------------------------------------------- 1. first time, then repeat


def test_the_first_time_gets_the_first_time_text_and_the_next_time_the_repeat():
    first = decide_next_action(_levels("opened_not_clicked"))
    again = decide_next_action(_levels("opened_not_clicked", sent=_sent("f-opened")))
    assert (first["action"], first["follow_up_step_id"]) == ("use_follow_up", "f-opened")
    assert (again["action"], again["follow_up_step_id"]) == ("use_follow_up", "f-opened-2")
    assert "Opened, not clicked (repeat)" in again["reason"]


def test_after_every_text_was_sent_the_ai_may_look():
    context = _levels("opened_not_clicked", sent=_sent("f-opened", "f-opened-2"))
    decision, ai = _ask(context, _answer("to_operations", reason="Opened twice, never clicked"))
    assert ai.asked == 1 and "every follow-up for it was already sent" in ai.prompts[0]
    assert decision["action"] == "to_operations"


def test_a_first_time_text_that_did_not_go_out_is_used_again_not_the_repeat():
    decision = decide_next_action(_levels("opened_not_clicked", sent=_sent("f-opened", status="Failed")))
    assert decision["follow_up_step_id"] == "f-opened"


def test_a_lined_up_first_time_text_is_not_followed_by_its_repeat():
    decision = decide_next_action(_levels("opened_not_clicked", swaps={"s2": "f-opened"}))
    assert decision["action"] == "keep_plan" and "already lined up" in decision["reason"]


def test_the_ai_is_offered_each_situations_next_text():
    context = _levels("no_activity", sent=_sent("f-quiet", "f-opened"), swaps={"s9": "f-part"})
    _, ai = _ask(context, _answer("keep_plan"))
    offered = ai.prompts[0]
    assert '"step_id": "f-opened-2"' in offered and '"level": "repeat"' in offered
    assert '"step_id": "f-opened",' not in offered  # sent: its repeat is offered instead
    assert '"f-part' not in offered.split('"follow_ups_available"')[1]  # its first text is lined up


# --------------------------------------------------------------------------- 2. plan setup not finished


def _plan_checkout(context, hours_ago=30, option_type="payment_plan"):
    context["recent_events"] = [{"type": "payment_started", "at": (NOW - timedelta(hours=hours_ago)).isoformat(),
                                 "detail": {"option_type": option_type, "attempt_id": "att-1"}}]
    return context


def test_the_plan_setup_timer_uses_its_follow_up():
    decision = decide_next_action(_levels("plan_setup_not_finished"))
    assert (decision["action"], decision["follow_up_step_id"]) == ("use_follow_up", "f-plan")
    assert decision["reason"].startswith("Customer began setting up a payment plan but did not finish")


def test_leaving_a_plan_checkout_is_plan_setup_not_payment_not_finished():
    for kind in ("payment_abandoned", "clicked_not_paid"):
        decision = decide_next_action(_plan_checkout(_levels(kind)))
        assert decision["follow_up_step_id"] == "f-plan", kind
    paying = decide_next_action(_plan_checkout(_levels("payment_abandoned"), option_type="pay_in_full"))
    assert paying["follow_up_step_id"] == "f-clicked"
    finished = _plan_checkout(_levels("payment_abandoned"))
    finished["recent_events"].append({"type": "plan_created", "at": (NOW - timedelta(hours=1)).isoformat()})
    assert decide_next_action(finished)["follow_up_step_id"] == "f-clicked"


def test_without_a_plan_setup_follow_up_the_payment_not_finished_one_is_used():
    decision = decide_next_action(_plan_checkout(_context("plan_setup_not_finished")))  # the old six only
    assert (decision["action"], decision["follow_up_step_id"]) == ("use_follow_up", "f-clicked")
    assert decide_next_action(_plan_checkout(_context("payment_abandoned")))["follow_up_step_id"] == "f-clicked"


# --------------------------------------------------------------------------- 3. silence after a part payment


def test_silence_after_a_part_payment_gets_its_repeat_text():
    decision = decide_next_action(_levels("no_activity", sent=_sent("f-part")))
    assert (decision["action"], decision["follow_up_step_id"]) == ("use_follow_up", "f-part-2")
    assert decision["reason"].startswith("No activity from the customer")
    # Without a part payment's follow-up sent, or with its repeat sent too: the usual "no response".
    assert decide_next_action(_levels("no_activity"))["follow_up_step_id"] == "f-quiet"
    assert decide_next_action(_levels("no_activity", sent=_sent("f-part", "f-part-2")))["follow_up_step_id"] == "f-quiet"


# --------------------------------------------------------------------------- 4. a plan set up answers it


def test_setting_up_a_plan_drops_a_lined_up_payment_or_plan_setup_follow_up():
    for follow_up in ("f-clicked", "f-plan"):
        decision = decide_next_action(_levels("plan_created", swaps={"s2": follow_up}))
        assert (decision["action"], decision["follow_up_step_id"]) == ("drop_follow_up", follow_up)
        assert "set up a payment plan" in decision["reason"]
    assert decide_next_action(_levels("plan_created", swaps={"s2": "f-part"}))["action"] == "keep_plan"
    assert decide_next_action(_levels("plan_created"))["action"] == "keep_plan"


# --------------------------------------------------------------------------- 5. saving, timer, backend


def _normalized(**config):
    return _normalize_step({"id": "fu", "kind": "Communication", "title": "Follow-up",
                            "config": {"purpose": "Payment Reminder", **config}})


def test_a_follow_up_keeps_its_level_when_saved():
    assert _normalized(follow_up_for="clicked_not_paid", follow_up_level=2)["config"]["follow_up_level"] == 2
    assert "follow_up_level" not in _normalized(follow_up_for="clicked_not_paid", follow_up_level=1)["config"]
    assert "follow_up_level" not in _normalized(follow_up_level=2)["config"]  # only on a follow-up
    assert _normalized(follow_up_for="plan_setup_not_finished")["config"]["follow_up_for"] == "plan_setup_not_finished"
    for wrong in ("two", 0, 10, 2.5, True):
        with pytest.raises(ValidationAppError):
            _normalized(follow_up_for="clicked_not_paid", follow_up_level=wrong)
    assert follow_up_level({"config": {"follow_up_level": 2}}) == 2 and follow_up_level({"config": {}}) == 1


def test_the_api_schema_keeps_the_level():
    assert StrategyNodeConfig(follow_up_for="no_activity", follow_up_level=2).model_dump()["follow_up_level"] == 2
    with pytest.raises(ValidationError):
        StrategyNodeConfig(follow_up_level=0)


def _checkout_started(db, run, *, hours_ago, option_type="payment_plan", key="start"):
    record_engagement_event(db, account_id=run.account_id, event_type="payment_started", source="test",
                            dedupe_key=f"lv:{run.id}:{key}", run_id=run.id,
                            occurred_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
                            detail={"option_type": option_type, "attempt_id": key})
    db.commit()


def _plan_setup_events(db, run):
    return (
        db.query(PayflowEngagementEventModel)
        .filter_by(run_id=run.id, event_type="plan_setup_not_finished")
        .all()
    )


_OPENED = _message("f-opened", "Opened, not clicked", channel="Email", follow_up_for="opened_not_clicked")
WORKER_STEPS = [
    _message("s1", "Email 1", channel="Email", next="s2"),
    _message("s2", "Email 2", channel="Email", next="s3"),
    _message("s3", "Email 3", channel="Email", next="s4"),
    {"id": "s4", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
    _OPENED,
    _repeat(_OPENED),
]


def test_the_plan_setup_timer_fires_24_hours_after_an_unfinished_plan_checkout(db_session):
    unfinished = _run(db_session, WORKER_STEPS)
    _checkout_started(db_session, unfinished, hours_ago=25)
    too_soon = _run(db_session, WORKER_STEPS)
    _checkout_started(db_session, too_soon, hours_ago=23)
    paying = _run(db_session, WORKER_STEPS)
    _checkout_started(db_session, paying, hours_ago=25, option_type="pay_in_full")
    finished = _run(db_session, WORKER_STEPS)
    _checkout_started(db_session, finished, hours_ago=25)
    _event(db_session, finished, "plan_created")

    emit_adaptive_timer_events(db_session)
    emit_adaptive_timer_events(db_session)  # once per checkout

    assert [len(_plan_setup_events(db_session, run)) for run in (unfinished, too_soon, paying, finished)] == [1, 0, 0, 0]


def test_the_backend_map_is_first_time_only_and_the_rules_see_the_repeats(db_session):
    run = _run(db_session, WORKER_STEPS)
    contexts = []
    with patch.object(adaptive_workflow_service, "decide_next_action",
                      side_effect=lambda ctx: contexts.append(ctx) or {"action": "keep_plan", "reason": "x"}):
        AdaptiveWorkflowService(db_session).reassess(run.id, trigger="event")
    assert contexts[0]["workflow"]["follow_ups"] == {"opened_not_clicked": "f-opened"}
    assert "f-opened-2" in {step["id"] for step in contexts[0]["workflow"]["steps"]}


def test_the_worker_sends_the_first_time_text_then_the_repeat(db_session):
    run = _run(db_session, WORKER_STEPS, current="s2")
    _event(db_session, run, "opened_not_clicked")
    first = _send(db_session, run, "s2")  # Email 2 → the first-time text

    _event(db_session, run, "opened_not_clicked")  # the same again, after Email 2's slot
    second = _send(db_session, run, "s3")  # Email 3 → the repeat text

    sent = db_session.query(PayflowCommunicationModel).filter_by(run_id=run.id).order_by(PayflowCommunicationModel.id)
    assert [m.step_id for m in first] == ["f-opened"]
    assert [m.step_id for m in sent] == ["f-opened", "f-opened-2"] and len(second) == 2
