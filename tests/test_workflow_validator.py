import copy

import pytest

from app.modules.payflow.services.workflow_suggestion.config import MAX_STEPS
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from app.modules.payflow.services.workflow_suggestion.validator import validate_all

SEGMENTS = [{"segment_id": "A"}]
LIMITS = {
    "channels": ["Email", "SMS"],
    "min_steps": 3,
    "max_steps": MAX_STEPS,
    "max_communications": 5,
    "max_messages_per_7_days": 3,
}


def valid_workflow():
    """Example 1: Trigger → Email → Wait → Any payment? → SMS → Wait → Any payment? → Review | stop branch."""
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
    workflow["steps"].append({"id": "s20", "kind": "Outcome", "title": "x", "config": {"outcome": "Case Closed"}})


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
        (_add_orphan, "steps not reachable from the entry step: s20"),
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
        (_drop("s2", "purpose", in_config=True), "step s2 (Communication): missing purpose"),
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
        ({"channels": []}, "step s2: this client has no email or SMS enabled - do not use Communication steps"),
        ({"max_steps": 5}, "has 13 steps (allowed 3-5)"),
        ({"max_communications": 1}, "has 2 Communication steps (max 1)"),
        ({"max_messages_per_7_days": 1}, "steps s2, s5 send more than 1 messages within 7 days"),
    ],
)
def test_limits(limits, expected):
    errors = errors_for(lambda w: None, limits)
    assert any(expected in e for e in errors), errors


ACTIVE = [{"segment_id": "A", "segment": {"situation": "Active"}}]
OLD_PAID_CHECK = {"attribute": "Payment Status", "operator": "Equals", "value": "Paid In Full"}


def _skip_the_plan_check(workflow):
    """Stop branch without "on a plan?" (s12 / s13): not paid in full goes straight to the part-payment review."""
    step(workflow, "s10")["no"] = "s11"
    workflow["steps"] = [s for s in workflow["steps"] if s["id"] not in ("s12", "s13")]


def test_prompt_examples_stop_when_the_customer_pays():
    """Each example checked with its own segment's situation (Active / Promise to Pay)."""
    for example in load_examples():
        segments = [{"segment_id": "A", "segment": {"situation": example["for_segment"].split(" · ")[0]}}]
        assert validate_all([{**copy.deepcopy(example), "segment_id": "A"}], segments, LIMITS) == [], example["name"]


@pytest.mark.parametrize(
    "change, expected",
    [
        (
            _set("s4", config=dict(OLD_PAID_CHECK)),  # misses part payments and plans
            'step s5: sends a follow-up message after step s2 without the check "Payment Status Not Equals Unpaid"',
        ),
        (
            _set("s7", config={"attribute": "Email Delivery", "operator": "Equals", "value": "Bounced"}),
            'step s8: hands the case over after step s5 without the check "Payment Status Not Equals Unpaid"',
        ),
        (
            _set("s4", yes="s9"),
            'step s4: yes (the customer paid or set up a plan) must go to the Condition '
            '"Payment Status Equals Paid In Full"',
        ),
        (
            _set_config("s9", outcome="Case Closed"),
            'step s10: yes (paid in full) must end in an Outcome "Paid In Full"',
        ),
        (
            _skip_the_plan_check,  # a plan customer would go to review, not to their plan
            'step s10: no (not paid in full) must go to the Condition "Payment Status Equals Payment Plan Active"',
        ),
        (
            _set_config("s13", outcome="Paid In Full"),  # would close the case and clear a plan customer's balance
            'step s12: yes (on a payment plan) must end in an Outcome "Payment Plan Active"',
        ),
        (
            _set("s11", kind="Outcome", config={"outcome": "Case Closed"}),
            "step s12: no (paid part only) must go to a Human Review to decide on the rest",
        ),
    ],
)
def test_reminders_stop_once_the_customer_pays_in_full_pays_part_or_sets_up_a_plan(change, expected):
    workflow = valid_workflow()
    change(workflow)
    errors = validate_all([workflow], ACTIVE, LIMITS)
    assert any(expected in e for e in errors), errors


def test_only_customers_who_paid_in_full_reach_the_paid_in_full_outcome():
    """Every workflow type: at run time Outcome "Paid In Full" closes the case and clears the balance."""
    workflow = valid_workflow()
    _set("s4", yes="s9")(workflow)  # "any payment or plan?" yes → straight to "Paid In Full"
    expected = 'step s4: leads to Outcome "Paid In Full" (step s9) without "Payment Status Equals Paid In Full" = yes'
    no_segment = [{"segment_id": "A", "segment": {}}]  # a workflow whose segment is not known
    for segments in (ACTIVE, PLAN, no_segment):
        errors = validate_all([copy.deepcopy(workflow)], segments, LIMITS)
        assert any(expected in e for e in errors), (segments, errors)

    workflow = valid_workflow()
    step(workflow, "s10").update(yes="s12", no="s9")  # "paid in full?" no → "Paid In Full"
    errors = validate_all([workflow], ACTIVE, LIMITS)
    assert any('step s10: leads to Outcome "Paid In Full" (step s9)' in e for e in errors), errors


def test_the_old_stop_branch_that_marks_plan_customers_paid_in_full_is_rejected():
    """Old branch: "Paid in full or on a plan?" yes → Outcome "Paid In Full" — the worker then clears the balance."""
    workflow = valid_workflow()
    _set("s10", config={"attribute": "Payment Status", "operator": "Not Equals", "value": "Partial Payment"},
         yes="s9", no="s11")(workflow)
    workflow["steps"] = [s for s in workflow["steps"] if s["id"] not in ("s12", "s13")]
    errors = validate_all([workflow], ACTIVE, LIMITS)
    assert any(
        'step s4: yes (the customer paid or set up a plan) must go to the Condition "Payment Status Equals Paid In Full"'
        in e
        for e in errors
    ), errors


PLAN = [{"segment_id": "A", "segment": {"situation": "Payment Plan"}}]
MISSED_INSTALMENT = {"attribute": "Payment Status", "operator": "Equals", "value": "Unpaid"}


def test_payment_plan_workflows_check_for_a_missed_instalment_instead():
    """Already on a plan: "any payment or plan?" is always yes for them and would end the workflow at once."""
    errors = validate_all([valid_workflow()], PLAN, LIMITS)
    assert any(
        'step s4: the customer is already on a payment plan, so "Payment Status Not Equals Unpaid" is always yes' in e
        for e in errors
    ), errors
    assert any(
        'step s12: the customer is already on a payment plan, so "Payment Status Equals Payment Plan Active" is always'
        in e
        for e in errors
    ), errors

    workflow = valid_workflow()
    part_paid = {"attribute": "Payment Status", "operator": "Equals", "value": "Partial Payment"}
    for step_id, check in (("s4", MISSED_INSTALMENT), ("s7", MISSED_INSTALMENT), ("s12", part_paid)):
        _set(step_id, config=dict(check))(workflow)
    assert validate_all([workflow], PLAN, LIMITS) == []
    assert validate_all([workflow], ACTIVE, LIMITS) != []  # an Active customer would not be stopped


def test_unknown_segments_are_not_checked_for_stopping_on_payment():
    workflow = valid_workflow()
    _set("s4", config=dict(OLD_PAID_CHECK))(workflow)
    assert validate_all([workflow], ACTIVE, LIMITS) != []
    assert validate_all([workflow], SEGMENTS, LIMITS) == []


def test_contact_steps_need_no_channel_and_never_branch_on_delivery():
    """The worker picks email or SMS per customer, switches when one fails and flags who it cannot reach."""
    assert validate_all([valid_workflow()], SEGMENTS, {**LIMITS, "channels": ["Email"]}) == []  # no channel needed
    for attribute, value in (("Email Delivery", "Bounced"), ("SMS Delivery", "Failed"), ("Email Address", "Invalid"),
                             ("Mobile Number", "Missing")):
        workflow = valid_workflow()
        _set("s7", config={"attribute": attribute, "operator": "Equals", "value": value})(workflow)
        errors = validate_all([workflow], SEGMENTS, LIMITS)
        assert any(f"step s7: do not branch on {attribute} - PayFlow switches between email and SMS" in e
                   for e in errors), errors


def test_a_step_that_still_has_a_channel_is_checked_as_before():
    """Older / hand-made workflows keep their channel: it must be enabled for the client."""
    workflow = valid_workflow()
    _set_config("s5", channel="SMS", template_id="sms-link")(workflow)
    assert validate_all([workflow], SEGMENTS, LIMITS) == []
    errors = validate_all([workflow], SEGMENTS, {**LIMITS, "channels": ["Email"]})
    assert any("step s5: channel 'SMS' is not enabled for this client" in e for e in errors), errors


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


def test_waits_after_a_message_are_not_timed_from_a_past_date():
    """PayFlow waits until date + amount: Case Received / Due Date are in the past for most accounts."""
    for event in ("Case Received", "Due Date"):
        errors = errors_for(_set_config("s3", reference_event=event))  # s3: the Wait after the first message
        assert any(f'step s3: comes after a message but is timed from "{event}"' in e for e in errors), errors
    errors = errors_for(_set_config("s5", amount=2, reference_event="Case Received"))  # a message sent 2 days "later"
    assert any('step s5: comes after a message but is timed from "Case Received"' in e for e in errors), errors


def test_the_first_message_and_sends_without_delay_may_use_a_past_date():
    assert errors_for(lambda w: None) == []  # s2, the first message: "0 Days After Case Received"
    assert errors_for(_set_config("s5", reference_event="Case Received")) == []  # amount 0: sent when its turn comes


def test_customer_response_is_only_checked_for_a_dispute():
    """PayFlow counts its own messages as contact: "Received" / "None" say nothing about the customer."""
    workflow = {**copy.deepcopy(load_examples()[2]), "segment_id": "A"}  # s8: "Dispute raised?"
    assert validate_all([workflow], ACTIVE, LIMITS) == []
    for value in ("Received", "None"):
        _set_config("s8", value=value)(workflow)
        assert validate_all([workflow], ACTIVE, LIMITS) == [
            f'Workflow A, step s8: "Customer Response {value}" cannot be trusted - PayFlow counts its own messages '
            'as contact; only check "Dispute Raised"'
        ]
