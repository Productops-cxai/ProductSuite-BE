"""The step limit fits the workflow the prompt asks for, and the AI is told how to fix a long or looping one.

Seen on a tester's import: the AI built the textbook workflow at the 5-message maximum (22 steps) while the limit was
20; told to cut steps, it pointed back to an earlier step (a loop) and the whole Suggest job failed.
"""

from unittest.mock import patch

from app.modules.payflow.services.workflow_suggestion import config
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.prompt import build_system_prompt
from app.modules.payflow.services.workflow_suggestion.rules import design_limits
from app.modules.payflow.services.workflow_suggestion.validator import validate_all
from tests.test_workflow_suggestion_api import _suggest, _workflow_for, portfolio  # noqa: F401 (fixture)
from tests.test_workflow_validator import SEGMENTS, errors_for

LIMITS = design_limits({"channels": ["Email", "SMS"]}, {"system": [], "client": []})
ACTIVE = [{"segment_id": "A", "segment": {"situation": "Active"}}]


def _wait(step_id, next_id):
    return {"id": step_id, "kind": "Wait", "title": "Wait", "next": next_id,
            "config": {"reference_event": "Previous Action", "amount": 4, "unit": "Days", "direction": "After"}}


def _message(step_id, next_id, event="Previous Action"):
    return {"id": step_id, "kind": "Communication", "title": "Reminder", "next": next_id,
            "config": {"purpose": "Payment Reminder", "reference_event": event, "amount": 0, "unit": "Days",
                       "direction": "After"}}


def _check(step_id, check, yes, no):
    return {"id": step_id, "kind": "Condition", "title": "Check", "yes": yes, "no": no, "config": dict(check)}


def textbook_workflow(messages=config.MAX_COMMUNICATIONS):
    """What the prompt asks for: a check before every follow-up and before the hand-over, one shared stop branch."""
    steps = [{"id": "s1", "kind": "Trigger", "title": "Start", "next": "s2", "config": {"reference_event": "Due Date"}},
             _message("s2", "s3", event="Due Date")]
    n = 3
    for _ in range(messages - 1):  # Wait -> any payment or plan? -> the follow-up
        steps += [_wait(f"s{n}", f"s{n + 1}"), _check(f"s{n + 1}", config.PAID_OR_PLAN_CHECK, "p1", f"s{n + 2}"),
                  _message(f"s{n + 2}", f"s{n + 3}")]
        n += 3
    steps += [_wait(f"s{n}", f"s{n + 1}"), _check(f"s{n + 1}", config.PAID_OR_PLAN_CHECK, "p1", f"s{n + 2}"),
              {"id": f"s{n + 2}", "kind": "Human Review", "title": "Still unpaid", "config": {"note": "Next step"}}]
    steps += [_check("p1", config.PAID_IN_FULL_CHECK, "p2", "p3"),
              {"id": "p2", "kind": "Outcome", "title": "Paid", "config": {"outcome": config.PAID_OUTCOME}},
              _check("p3", config.ON_A_PLAN_CHECK, "p4", "p5"),
              {"id": "p4", "kind": "Outcome", "title": "On a plan", "config": {"outcome": config.ON_A_PLAN_OUTCOME}},
              {"id": "p5", "kind": "Human Review", "title": "Part paid", "config": {"note": "Decide on the rest"}}]
    return {"segment_id": "A", "name": "Reminders", "summary": "Reminders until paid.", "entry_node_id": "s1",
            "steps": steps, "reasoning": {field: "Fits." for field in config.REASONING_FIELDS}}


def test_the_workflow_the_prompt_asks_for_fits_at_the_most_messages():
    workflow = textbook_workflow()
    assert len(workflow["steps"]) == 22 and LIMITS["max_steps"] == 24
    assert validate_all([workflow], ACTIVE, LIMITS) == []
    # The step cost the prompt states adds up to the same number.
    assert 2 + (config.MAX_COMMUNICATIONS - 1) * config.STEPS_PER_FOLLOW_UP + 3 + config.STOP_BRANCH_STEPS == 22


def test_too_many_steps_says_how_to_cut_them():
    errors = validate_all([textbook_workflow()], ACTIVE, {**LIMITS, "max_steps": 20})
    assert errors == ["Workflow A: has 22 steps (allowed 3-20) - use fewer messages or branches; "
                      "never point back to an earlier step to save steps"]
    # Too few steps: just the count.
    assert any(e.endswith("has 13 steps (allowed 14-24)") for e in errors_for(lambda w: None, {"min_steps": 14}))


def test_a_loop_names_the_step_that_points_back():
    def loop_back(workflow):  # s6 (Wait after the SMS) goes back to s5 (the SMS)
        next(s for s in workflow["steps"] if s["id"] == "s6")["next"] = "s5"

    assert ("Workflow A: the flowchart contains a loop - step s6 points back to step s5; every path must only move "
            "forward (to remind again, add a new step)") in errors_for(loop_back)

    def check_back(workflow):  # a Condition's no goes back to the first message
        next(s for s in workflow["steps"] if s["id"] == "s4")["no"] = "s2"

    assert any("step s4 points back to step s2" in e for e in errors_for(check_back))

    def to_itself(workflow):
        next(s for s in workflow["steps"] if s["id"] == "s3")["next"] = "s3"

    assert any("step s3 points back to step s3" in e for e in errors_for(to_itself))


def test_paths_that_join_again_are_not_a_loop():
    workflow = textbook_workflow()  # every check's yes joins the one stop branch
    assert not any("loop" in e for e in validate_all([workflow], SEGMENTS, LIMITS))


def test_the_prompt_says_how_many_steps_each_part_takes():
    prompt = " ".join(build_system_prompt(LIMITS).split())
    assert ("Between 3 and 24 steps per workflow. Count them before you answer: in Active and Promise to Pay "
            "workflows each follow-up message takes 3 steps (Wait, check, message) and the stop branch 5. If a "
            "workflow would have more, use fewer messages or branches - never point back to an earlier step.") in prompt


def test_suggest_sends_the_looping_step_back_to_the_ai(client, admin_token, portfolio):
    calls = []

    def call(messages, schema):
        calls.append(messages)
        workflow = _workflow_for("A")
        if len(calls) == 1:
            workflow["steps"][5]["next"] = "s5"  # s6 (Wait) back to s5
        return {"workflows": [workflow]}

    with patch.object(suggestion_service, "call_llm", side_effect=call):
        response = _suggest(client, admin_token, portfolio["client_id"], portfolio["portfolio_id"])
    assert response.status_code == 200, response.json()
    assert len(calls) == 2
    assert "the flowchart contains a loop - step s6 points back to step s5" in calls[1][1]["content"]
