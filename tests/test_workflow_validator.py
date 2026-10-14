import copy

import pytest

from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from app.modules.payflow.services.workflow_suggestion.validator import validate_all

SEGMENTS = [{"segment_id": "A"}]
LIMITS = {
    "channels": ["Email", "SMS"],
    "min_steps": 3,
    "max_steps": 12,
    "max_communications": 5,
    "max_messages_per_7_days": 3,
}


def valid_workflow():
    """Example 1: Trigger → Email → Wait → Paid? → SMS → Wait → Paid? → Review | Outcome."""
    workflow = copy.deepcopy(load_examples()[0])
    workflow["segment_id"] = "A"
    return workflow


def step(workflow, step_id):
    return next(s for s in workflow["steps"] if s["id"] == step_id)


def errors_for(change, limits=None):
    workflow = valid_workflow()
    change(workflow)
    return validate_all([workflow], SEGMENTS, {**LIMITS, **(limits or {})})


def test_prompt_examples_are_valid():
    for i, example in enumerate(load_examples()):
        workflow = {**copy.deepcopy(example), "segment_id": "A"}
        assert validate_all([workflow], SEGMENTS, LIMITS) == [], f"example {i}"


def test_valid_workflow_has_no_errors():
    assert validate_all([valid_workflow()], SEGMENTS, LIMITS) == []


def test_structure_unknown_missing_and_duplicate_segments():
    unknown = {**valid_workflow(), "segment_id": "Z"}
    errors = validate_all([unknown], SEGMENTS, LIMITS)
    assert "Workflow Z: unknown segment_id 'Z'" in errors
    assert "Segment A: no workflow was suggested" in errors

    errors = validate_all([valid_workflow(), valid_workflow()], SEGMENTS, LIMITS)
    assert "Workflow A: more than one workflow for this segment" in errors

    errors = validate_all([{**valid_workflow(), "steps": []}], SEGMENTS, LIMITS)
    assert "Workflow A: has no steps" in errors


def _set(step_id, **values):
    def change(workflow):
        step(workflow, step_id).update(values)

    return change


def _set_config(step_id, **values):
    def change(workflow):
        step(workflow, step_id)["config"].update(values)

    return change


def _drop(step_id, key, in_config=False):
    def change(workflow):
        target = step(workflow, step_id)
        (target["config"] if in_config else target).pop(key)

    return change


def _add_orphan(workflow):
    workflow["steps"].append({"id": "s10", "kind": "Outcome", "title": "x", "config": {"outcome": "Case Closed"}})


@pytest.mark.parametrize(
    "change, expected",
    [
        # Flowchart
        (lambda w: w.update(entry_node_id="s2"), "entry_node_id must point to a Trigger step"),
        (_set("s2", next="s99"), "step s2: next points to unknown step 's99'"),
        (_drop("s4", "no"), "step s4: a Condition needs both yes and no"),
        (_set("s2", yes="s3"), "step s2: only a Condition may use yes / no"),
        (_drop("s3", "next"), "step s3: a Wait step cannot end a path"),
        (_set("s9", next="s1"), "step s9: an Outcome ends the path and cannot have next"),
        (_set("s8", kind="Trigger"), "step s8: only the entry step may be a Trigger"),
        (_add_orphan, "steps not reachable from the entry step: s10"),
        (_set("s6", next="s5"), "the flowchart contains a loop"),
        # Catalog values
        (_set("s3", kind="Email Blast"), "step s3: unknown step kind 'Email Blast'"),
        (_set_config("s2", template_id="em-firm"), "template 'em-firm' is for Email / Firm Reminder"),
        (_set_config("s2", template_id="em-nope"), "template 'em-nope' does not exist"),
        (_set_config("s4", value="Paid"), "value 'Paid' is not allowed for Payment Status"),
        (_set_config("s3", reference_event="Tomorrow"), "reference_event 'Tomorrow' is not allowed"),
        (_set_config("s3", amount=-2), "amount must be a number of 0 or more"),
        (
            _set("s8", kind="Case Action", config={"action": "Offer Payment Plan"}),
            "action 'Offer Payment Plan' is not allowed for a Case Action step",
        ),
        # Step config
        (_drop("s3", "amount", in_config=True), "step s3 (Wait): missing amount"),
        (_drop("s2", "template_id", in_config=True), "step s2 (Communication): missing template_id"),
        # Timing without a reference_event (rejected by the save step)
        (_set_config("s4", unit="Days"), "step s4 (Condition): has timing (amount / unit) but no reference_event"),
        (_set_config("s8", amount=2), "step s8 (Human Review): has timing (amount / unit) but no reference_event"),
        # Message spacing: s3 is no longer a Wait, so s2 and s5 go out back to back
        (_set("s3", kind="AI Reassessment", config={"note": "check"}), "steps s2 and s5 send messages with no Wait"),
    ],
)
def test_each_check_reports_a_clear_error(change, expected):
    errors = errors_for(change)
    assert any(expected in e for e in errors), errors


@pytest.mark.parametrize(
    "limits, expected",
    [
        ({"channels": ["Email"]}, "step s5: channel 'SMS' is not enabled for this client"),
        ({"max_steps": 5}, "has 9 steps (allowed 3-5)"),
        ({"max_communications": 1}, "has 2 Communication steps (max 1)"),
        ({"max_messages_per_7_days": 1}, "steps s2, s5 send more than 1 messages within 7 days"),
    ],
)
def test_limits(limits, expected):
    errors = errors_for(lambda w: None, limits)
    assert any(expected in e for e in errors), errors


def test_missing_reference_event_is_reported_once():
    errors = errors_for(_drop("s3", "reference_event", in_config=True))
    assert [e for e in errors if "reference_event" in e] == ["Workflow A, step s3 (Wait): missing reference_event"]


def test_weekly_cap_counts_wait_time():
    # s2 at day 0, s5 after a 10-day wait → never 2 messages in the same 7 days.
    errors = errors_for(_set_config("s3", amount=10), {"max_messages_per_7_days": 1})
    assert errors == []


def test_reasoning_is_required():
    workflow = valid_workflow()
    workflow["reasoning"].pop("timing")
    workflow["reasoning"]["channels"] = "  "
    errors = validate_all([workflow], SEGMENTS, LIMITS)
    assert "Workflow A: reasoning is missing channels, timing" in errors
    assert validate_all([{**valid_workflow(), "reasoning": None}], SEGMENTS, LIMITS) == [
        "Workflow A: reasoning is missing segment_fit, channels, timing, escalation"
    ]
