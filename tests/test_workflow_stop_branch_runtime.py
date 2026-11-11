"""AI workflows run with the worker's own code: the Condition check and the Outcome the engine applies.

- Stop branch: before the fix it was "paid in full OR on a plan → Outcome Paid In Full", and the worker then
  closed the case and set the balance to 0 — for a customer who had only set up a payment plan.
- Payment Status (issue #2): a part payment counts only when made since the workflow started, and a customer
  on a plan is "Unpaid" while an instalment is missed.
"""

import copy

import pytest

from app.infrastructure.database.models import PayflowWorkflowRunModel
from app.modules.payflow.services.workflow_condition_runtime import apply_outcome_to_account
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.modules.payflow.services.workflow_suggestion.message_tone import message_tones
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from app.modules.payflow.services.workflow_suggestion.validator import validate_all
from tests.test_workflow_condition_runtime import _client_account, _plan

# customer → (account, where the first payment check leads, collection status after, balance after)
CUSTOMERS = {
    "unpaid": (dict(outstanding=500.0), "next reminder", "Active", 500.0),
    "paid part": (dict(outstanding=300.0, recovered=200.0), "Human Review", "Active", 300.0),
    "on a plan": (dict(outstanding=500.0, status="Payment Plan"), "Payment Plan Active", "Payment Plan", 500.0),
    "paid in full": (dict(outstanding=0.0, recovered=500.0), "Paid In Full", "Resolved", 0.0),
}


def _run_checks(db, account, workflow, step_id, run=None):
    """Follow Conditions from `step_id` with the engine's own check; the first step that is not a Condition."""
    steps = {step["id"]: step for step in workflow["steps"]}
    engine = WorkflowExecutionService(db)
    step = steps[step_id]
    while step["kind"] == "Condition":
        matched = engine._eval_condition(account, step["config"], step, {}, run=run)
        step = steps[step["yes"] if matched else step["no"]]
    return step


def _first_payment_check(workflow):
    """The "any payment or plan?" check before the first follow-up message."""
    return next(s["id"] for s in workflow["steps"] if s["kind"] == "Condition" and s["config"]["value"] == "Unpaid")


@pytest.mark.parametrize("customer", list(CUSTOMERS))
@pytest.mark.parametrize("example", load_examples(), ids=lambda example: example["name"])
def test_stop_branch_with_the_workers_own_checks(db_session, example, customer):
    account_values, expected, status_after, balance_after = CUSTOMERS[customer]
    _, account = _client_account(db_session, **account_values)

    end = _run_checks(db_session, account, example, _first_payment_check(example))
    if expected == "next reminder":
        assert end["kind"] == "Communication"  # nothing paid: the reminders go on
    elif expected == "Human Review":
        assert end["kind"] == "Human Review"  # paid part: a person agrees the rest
    else:
        assert end["kind"] == "Outcome" and end["config"]["outcome"] == expected
        apply_outcome_to_account(account, end["config"]["outcome"])  # what the engine does at an Outcome
    assert account.collection_status == status_after
    assert float(account.outstanding_balance) == balance_after


def test_the_old_branch_cleared_a_plan_customers_balance(db_session):
    """Shows the bug this fixes: old branch + the worker's Outcome = the plan customer's balance set to 0."""
    old = copy.deepcopy(load_examples()[0])
    next(s for s in old["steps"] if s["id"] == "s10").update(
        config={"attribute": "Payment Status", "operator": "Not Equals", "value": "Partial Payment"},
        yes="s9",
        no="s11",
    )
    _, account = _client_account(db_session, outstanding=500.0, status="Payment Plan")

    end = _run_checks(db_session, account, old, _first_payment_check(old))
    assert end["config"]["outcome"] == "Paid In Full"
    apply_outcome_to_account(account, end["config"]["outcome"])
    assert account.collection_status == "Resolved" and float(account.outstanding_balance) == 0.0


# --------------------------------------------------------------------------- Payment Status since the start


@pytest.mark.parametrize("example", load_examples(), ids=lambda example: example["name"])
def test_a_part_payment_from_before_the_workflow_keeps_the_reminders_going(db_session, example):
    """Partly-paid segment: the old part payment must not stop the workflow at its first check."""
    _, account = _client_account(db_session, outstanding=300.0, recovered=200.0)  # paid 200 before enrolment
    run = PayflowWorkflowRunModel(recovered_at_start=200.0)
    check = _first_payment_check(example)

    assert _run_checks(db_session, account, example, check, run=run)["kind"] == "Communication"  # next reminder
    account.recovered_balance, account.outstanding_balance = 250.0, 250.0  # pays 50 during the workflow
    assert _run_checks(db_session, account, example, check, run=run)["kind"] == "Human Review"  # agree the rest


# A Payment Plan workflow as the prompt describes it: reminder → Wait → instalment missed? → follow-up → ...
PLAN_WORKFLOW = {
    "segment_id": "A",
    "name": "Payment Plan Check-In",
    "summary": "Plan reminder, then a follow-up only if an instalment is missed.",
    "reasoning": {"segment_fit": "On a plan.", "channels": "PayFlow picks.", "timing": "3 days.", "escalation": "Review."},
    "entry_node_id": "s1",
    "steps": [
        {"id": "s1", "kind": "Trigger", "title": "Case received", "config": {"reference_event": "Case Received"},
         "next": "s2"},
        {"id": "s2", "kind": "Communication", "title": "Plan reminder",
         "config": {"purpose": "Payment Plan Reminder", "amount": 0, "unit": "Days", "direction": "After",
                    "reference_event": "Case Received"}, "next": "s3"},
        {"id": "s3", "kind": "Wait", "title": "Wait 3 days",
         "config": {"amount": 3, "unit": "Days", "direction": "After", "reference_event": "Previous Action"},
         "next": "s4"},
        {"id": "s4", "kind": "Condition", "title": "Instalment missed?",
         "config": {"attribute": "Payment Status", "operator": "Equals", "value": "Unpaid"}, "yes": "s5", "no": "s9"},
        {"id": "s5", "kind": "Communication", "title": "Missed instalment reminder",
         "config": {"purpose": "Payment Plan Reminder", "amount": 0, "unit": "Days", "direction": "After",
                    "reference_event": "Previous Action"}, "next": "s6"},
        {"id": "s6", "kind": "Wait", "title": "Wait 3 days",
         "config": {"amount": 3, "unit": "Days", "direction": "After", "reference_event": "Previous Action"},
         "next": "s7"},
        {"id": "s7", "kind": "Condition", "title": "Still missed?",
         "config": {"attribute": "Payment Status", "operator": "Equals", "value": "Unpaid"}, "yes": "s8", "no": "s9"},
        {"id": "s8", "kind": "Human Review", "title": "Plan review", "config": {"note": "Instalment still missed."}},
        {"id": "s9", "kind": "Outcome", "title": "Plan on track", "config": {"outcome": "Payment Plan Active"}},
    ],
}


def test_the_payment_plan_pattern_is_a_valid_ai_design():
    segments = [{"segment_id": "A", "segment": {"situation": "Payment Plan"}}]
    limits = {"channels": ["Email", "SMS"], "min_steps": 3, "max_steps": 20, "max_communications": 5,
              "max_messages_per_7_days": None}
    assert validate_all([copy.deepcopy(PLAN_WORKFLOW)], segments, limits) == []


@pytest.mark.parametrize(
    "plan_status, instalments, expected",
    [
        ("Active", ("Paid", "Pending"), "Payment Plan Active"),  # on track: the workflow ends, plan kept
        ("Overdue", ("Paid", "Overdue"), "follow-up"),  # instalment missed
        ("Active", ("Failed", "Pending"), "follow-up"),  # a charge failed
    ],
    ids=["on track", "instalment missed", "charge failed"],
)
def test_payment_plan_workflow_with_the_workers_own_checks(db_session, plan_status, instalments, expected):
    _, account = _client_account(db_session, outstanding=400.0, recovered=100.0, status="Payment Plan")
    _plan(db_session, account, plan_status, instalments)

    end = _run_checks(db_session, account, PLAN_WORKFLOW, "s4", run=PayflowWorkflowRunModel(recovered_at_start=100.0))
    if expected == "follow-up":
        assert end["id"] == "s5"  # the missed-instalment reminder goes out
    else:
        assert end["kind"] == "Outcome" and end["config"]["outcome"] == expected
        apply_outcome_to_account(account, expected)
        assert account.collection_status == "Payment Plan" and float(account.outstanding_balance) == 400.0


def test_the_missed_instalment_reminder_is_written_firmer():
    """The text writer's tone rules read the same branch: after "instalment missed? = yes" the tone is Firm."""
    tones = message_tones(PLAN_WORKFLOW, {"situation": "Payment Plan"})
    assert tones["s5"]["tone"] == "Firm" and "plan payment missed" in tones["s5"]["why"]
"""AI workflows run with the worker's own code: the Condition check and the Outcome the engine applies.

- Stop branch: before the fix it was "paid in full OR on a plan → Outcome Paid In Full", and the worker then
  closed the case and set the balance to 0 — for a customer who had only set up a payment plan.
- Payment Status (issue #2): a part payment counts only when made since the workflow started, and a customer
  on a plan is "Unpaid" while an instalment is missed.
"""

import copy

import pytest

from app.infrastructure.database.models import PayflowWorkflowRunModel
from app.modules.payflow.services.workflow_condition_runtime import apply_outcome_to_account
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.modules.payflow.services.workflow_suggestion.message_tone import message_tones
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from app.modules.payflow.services.workflow_suggestion.validator import validate_all
from tests.test_workflow_condition_runtime import _client_account, _plan

# customer → (account, where the first payment check leads, collection status after, balance after)
CUSTOMERS = {
    "unpaid": (dict(outstanding=500.0), "next reminder", "Active", 500.0),
    "paid part": (dict(outstanding=300.0, recovered=200.0), "Human Review", "Active", 300.0),
    "on a plan": (dict(outstanding=500.0, status="Payment Plan"), "Payment Plan Active", "Payment Plan", 500.0),
    "paid in full": (dict(outstanding=0.0, recovered=500.0), "Paid In Full", "Resolved", 0.0),
}


def _run_checks(db, account, workflow, step_id, run=None):
    """Follow Conditions from `step_id` with the engine's own check; the first step that is not a Condition."""
    steps = {step["id"]: step for step in workflow["steps"]}
    engine = WorkflowExecutionService(db)
    step = steps[step_id]
    while step["kind"] == "Condition":
        matched = engine._eval_condition(account, step["config"], step, {}, run=run)
        step = steps[step["yes"] if matched else step["no"]]
    return step


def _first_payment_check(workflow):
    """The "any payment or plan?" check before the first follow-up message."""
    return next(s["id"] for s in workflow["steps"] if s["kind"] == "Condition" and s["config"]["value"] == "Unpaid")


@pytest.mark.parametrize("customer", list(CUSTOMERS))
@pytest.mark.parametrize("example", load_examples(), ids=lambda example: example["name"])
def test_stop_branch_with_the_workers_own_checks(db_session, example, customer):
    account_values, expected, status_after, balance_after = CUSTOMERS[customer]
    _, account = _client_account(db_session, **account_values)

    end = _run_checks(db_session, account, example, _first_payment_check(example))
    if expected == "next reminder":
        assert end["kind"] == "Communication"  # nothing paid: the reminders go on
    elif expected == "Human Review":
        assert end["kind"] == "Human Review"  # paid part: a person agrees the rest
    else:
        assert end["kind"] == "Outcome" and end["config"]["outcome"] == expected
        apply_outcome_to_account(account, end["config"]["outcome"])  # what the engine does at an Outcome
    assert account.collection_status == status_after
    assert float(account.outstanding_balance) == balance_after


def test_the_old_branch_no_longer_clears_a_plan_customers_balance(db_session):
    """The old branch still leads a plan customer to Outcome "Paid In Full" (workflows saved before the
    fix, or built by hand), but the worker no longer closes the case or sets the balance to 0 for it."""
    old = copy.deepcopy(load_examples()[0])
    next(s for s in old["steps"] if s["id"] == "s10").update(
        config={"attribute": "Payment Status", "operator": "Not Equals", "value": "Partial Payment"},
        yes="s9",
        no="s11",
    )
    _, account = _client_account(db_session, outstanding=500.0, status="Payment Plan")

    end = _run_checks(db_session, account, old, _first_payment_check(old))
    assert end["config"]["outcome"] == "Paid In Full"
    applied = apply_outcome_to_account(account, end["config"]["outcome"])
    assert applied["applied"] is False and account.human_review is True  # a person looks at it
    assert account.collection_status == "Payment Plan" and float(account.outstanding_balance) == 500.0


# --------------------------------------------------------------------------- Payment Status since the start


@pytest.mark.parametrize("example", load_examples(), ids=lambda example: example["name"])
def test_a_part_payment_from_before_the_workflow_keeps_the_reminders_going(db_session, example):
    """Partly-paid segment: the old part payment must not stop the workflow at its first check."""
    _, account = _client_account(db_session, outstanding=300.0, recovered=200.0)  # paid 200 before enrolment
    run = PayflowWorkflowRunModel(recovered_at_start=200.0)
    check = _first_payment_check(example)

    assert _run_checks(db_session, account, example, check, run=run)["kind"] == "Communication"  # next reminder
    account.recovered_balance, account.outstanding_balance = 250.0, 250.0  # pays 50 during the workflow
    assert _run_checks(db_session, account, example, check, run=run)["kind"] == "Human Review"  # agree the rest


# A Payment Plan workflow as the prompt describes it: reminder → Wait → instalment missed? → follow-up → ...
PLAN_WORKFLOW = {
    "segment_id": "A",
    "name": "Payment Plan Check-In",
    "summary": "Plan reminder, then a follow-up only if an instalment is missed.",
    "reasoning": {"segment_fit": "On a plan.", "channels": "PayFlow picks.", "timing": "3 days.", "escalation": "Review."},
    "entry_node_id": "s1",
    "steps": [
        {"id": "s1", "kind": "Trigger", "title": "Case received", "config": {"reference_event": "Case Received"},
         "next": "s2"},
        {"id": "s2", "kind": "Communication", "title": "Plan reminder",
         "config": {"purpose": "Payment Plan Reminder", "amount": 0, "unit": "Days", "direction": "After",
                    "reference_event": "Case Received"}, "next": "s3"},
        {"id": "s3", "kind": "Wait", "title": "Wait 3 days",
         "config": {"amount": 3, "unit": "Days", "direction": "After", "reference_event": "Previous Action"},
         "next": "s4"},
        {"id": "s4", "kind": "Condition", "title": "Instalment missed?",
         "config": {"attribute": "Payment Status", "operator": "Equals", "value": "Unpaid"}, "yes": "s5", "no": "s9"},
        {"id": "s5", "kind": "Communication", "title": "Missed instalment reminder",
         "config": {"purpose": "Payment Plan Reminder", "amount": 0, "unit": "Days", "direction": "After",
                    "reference_event": "Previous Action"}, "next": "s6"},
        {"id": "s6", "kind": "Wait", "title": "Wait 3 days",
         "config": {"amount": 3, "unit": "Days", "direction": "After", "reference_event": "Previous Action"},
         "next": "s7"},
        {"id": "s7", "kind": "Condition", "title": "Still missed?",
         "config": {"attribute": "Payment Status", "operator": "Equals", "value": "Unpaid"}, "yes": "s8", "no": "s9"},
        {"id": "s8", "kind": "Human Review", "title": "Plan review", "config": {"note": "Instalment still missed."}},
        {"id": "s9", "kind": "Outcome", "title": "Plan on track", "config": {"outcome": "Payment Plan Active"}},
    ],
}


def test_the_payment_plan_pattern_is_a_valid_ai_design():
    segments = [{"segment_id": "A", "segment": {"situation": "Payment Plan"}}]
    limits = {"channels": ["Email", "SMS"], "min_steps": 3, "max_steps": 20, "max_communications": 5,
              "max_messages_per_7_days": None}
    assert validate_all([copy.deepcopy(PLAN_WORKFLOW)], segments, limits) == []


@pytest.mark.parametrize(
    "plan_status, instalments, expected",
    [
        ("Active", ("Paid", "Pending"), "Payment Plan Active"),  # on track: the workflow ends, plan kept
        ("Overdue", ("Paid", "Overdue"), "follow-up"),  # instalment missed
        ("Active", ("Failed", "Pending"), "follow-up"),  # a charge failed
    ],
    ids=["on track", "instalment missed", "charge failed"],
)
def test_payment_plan_workflow_with_the_workers_own_checks(db_session, plan_status, instalments, expected):
    _, account = _client_account(db_session, outstanding=400.0, recovered=100.0, status="Payment Plan")
    _plan(db_session, account, plan_status, instalments)

    end = _run_checks(db_session, account, PLAN_WORKFLOW, "s4", run=PayflowWorkflowRunModel(recovered_at_start=100.0))
    if expected == "follow-up":
        assert end["id"] == "s5"  # the missed-instalment reminder goes out
    else:
        assert end["kind"] == "Outcome" and end["config"]["outcome"] == expected
        apply_outcome_to_account(account, expected)
        assert account.collection_status == "Payment Plan" and float(account.outstanding_balance) == 400.0


def test_the_missed_instalment_reminder_is_written_firmer():
    """The text writer's tone rules read the same branch: after "instalment missed? = yes" the tone is Firm."""
    tones = message_tones(PLAN_WORKFLOW, {"situation": "Payment Plan"})
    assert tones["s5"]["tone"] == "Firm" and "plan payment missed" in tones["s5"]["why"]
