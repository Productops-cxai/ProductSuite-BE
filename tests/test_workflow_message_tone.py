"""Code rules decide each message's tone from the segment, its position and its branch."""

from app.modules.payflow.services.strategy_service import DELINQUENCY_BANDS
from app.modules.payflow.services.workflow_suggestion.config import DELINQUENCY_RANGES
from app.modules.payflow.services.workflow_suggestion.message_tone import START_TONE, message_tones
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples

EARLY_NEW = {"situation": "Active", "delinquency": "1 – 29 days past due", "contact_state": "never_contacted"}
LATE_IGNORED = {"situation": "Active", "delinquency": "60 – 89 days past due", "contact_state": "contacted_no_payment"}


def example(name):
    return next(e for e in load_examples() if e["name"] == name)


def flow(*steps):
    return {"entry_node_id": "s1", "steps": [{"id": "s1", "kind": "Trigger", "next": steps[0]["id"]}, *steps]}


def message(step_id, next_id=None, purpose="Payment Reminder"):
    return {"id": step_id, "kind": "Communication", "config": {"channel": "Email", "purpose": purpose}, "next": next_id}


def condition(step_id, attribute, value, yes, no, operator="Equals"):
    config = {"attribute": attribute, "operator": operator, "value": value}
    return {"id": step_id, "kind": "Condition", "config": config, "yes": yes, "no": no}


def end(step_id, kind="Outcome"):
    return {"id": step_id, "kind": kind}


def tones(workflow, segment, avg_days_past_due=None):
    return {step_id: t["tone"] for step_id, t in message_tones(workflow, segment, avg_days_past_due).items()}


def test_start_tone_covers_every_stage():
    """If a stage band is renamed in the catalog, this fails instead of silently going Friendly."""
    assert set(START_TONE) == set(DELINQUENCY_BANDS[1:]) == set(DELINQUENCY_RANGES)


def test_same_workflow_is_soft_for_early_and_firm_for_late_segments():
    workflow = example("Early Stage Friendly Reminder")  # email → wait → unpaid → SMS → wait → unpaid → Human Review
    assert tones(workflow, EARLY_NEW) == {"s2": "Friendly", "s5": "Clear"}
    assert tones(workflow, LATE_IGNORED) == {"s2": "Firm", "s5": "Final notice"}


def test_every_message_gets_a_reason_and_nothing_else_gets_a_tone():
    result = message_tones(example("Early Stage Friendly Reminder"), LATE_IGNORED)
    assert set(result) == {"s2", "s5"}  # the two Communication steps only
    assert result["s5"]["why"] == (
        "60 – 89 days past due; contacted before but nothing paid; message 2 on this path; "
        "next step hands the case over"
    )


def test_link_clicked_gets_help_not_pressure():
    workflow = flow(
        message("s2", "s3"),
        condition("s3", "Payment Link", "Clicked", yes="s4", no="s5"),
        message("s4", "s6"),
        message("s5", "s6"),
        end("s6", "Human Review"),
    )
    assert tones(workflow, LATE_IGNORED) == {"s2": "Firm", "s4": "Clear", "s5": "Final notice"}


def test_first_message_is_never_a_final_notice():
    workflow = flow(message("s2", "s3"), end("s3", "Human Review"))
    assert tones(workflow, LATE_IGNORED) == {"s2": "Firm"}
    late_mid = tones(example("Mid-Stage Channel-Aware Follow-Up"), LATE_IGNORED)
    assert late_mid["s5"] == "Firm"  # the email bounced: this SMS is the first one that arrives
    assert late_mid["s6"] == "Final notice"


def test_message_after_a_bounce_counts_as_the_first_one():
    workflow = example("Mid-Stage Channel-Aware Follow-Up")  # firm email → bounced? → SMS : email
    result = tones(workflow, EARLY_NEW)
    assert result["s5"] == "Friendly"  # the email never arrived: the SMS is the first contact
    assert result["s6"] == "Clear"  # the email did arrive: second message


def test_firm_reminder_is_one_step_firmer_than_the_segment():
    """The segment still counts: an early, never-contacted customer does not jump straight to Firm."""
    workflow = flow(message("s2", "s3", "Firm Reminder"), end("s3"))
    mid = {"situation": "Active", "delinquency": "30 – 59 days past due", "contact_state": "any"}
    assert tones(workflow, EARLY_NEW) == {"s2": "Clear"}  # Friendly + 1
    assert tones(workflow, mid) == {"s2": "Firm"}  # Clear + 1
    assert tones(workflow, LATE_IGNORED) == {"s2": "Firm"}  # already Firm


def test_broken_promise_is_firm():
    promise = {"situation": "Promise to Pay", "delinquency": "All stages", "contact_state": "any"}
    for check in (
        condition("s3", "Promise-to-Pay", "Kept", yes="s4", no="s5", operator="Not Equals"),
        condition("s3", "Promise-to-Pay", "Kept", yes="s5", no="s4"),
        condition("s3", "Promise-to-Pay", "Broken", yes="s4", no="s5"),
    ):
        workflow = flow(message("s2", "s3", "Promise-to-Pay Reminder"), check, message("s4", "s5"), end("s5"))
        assert tones(workflow, promise) == {"s2": "Friendly", "s4": "Firm"}


def test_payment_plan_is_supportive_until_a_payment_is_missed():
    plan = {"situation": "Payment Plan", "delinquency": "All stages", "contact_state": "any"}
    workflow = flow(
        message("s2", "s3", "Payment Plan Reminder"),
        condition("s3", "Payment Status", "Unpaid", yes="s4", no="s5"),
        message("s4", "s5"),
        end("s5"),
    )
    assert tones(workflow, plan) == {"s2": "Supportive", "s4": "Firm"}


def test_partly_paid_is_thanked_first_and_never_pushed_to_firm():
    partly = {"situation": "Active", "delinquency": "90+ days past due", "contact_state": "partly_paid"}
    workflow = flow(message("s2", "s3"), message("s3", "s4"), message("s4", "s5"), end("s5"))
    assert tones(workflow, partly) == {"s2": "Supportive", "s3": "Clear", "s4": "Clear"}


def test_contact_update_and_dispute_are_neutral():
    workflow = flow(
        message("s2", "s3", "Contact Details Update Request"),
        condition("s3", "Customer Response", "Dispute Raised", yes="s4", no="s5"),
        message("s4", "s5"),
        end("s5"),
    )
    assert tones(workflow, LATE_IGNORED) == {"s2": "Neutral", "s4": "Neutral"}


def test_all_stages_segment_uses_the_average_but_never_starts_above_clear():
    """A mixed segment also holds early accounts — a late average must not make their first message Firm."""
    mixed = {"situation": "Active", "delinquency": "All stages", "contact_state": "any"}
    workflow = flow(message("s2", "s3"), message("s3", "s4"), end("s4"))
    assert tones(workflow, mixed, avg_days_past_due=75) == {"s2": "Clear", "s3": "Firm"}
    assert tones(workflow, mixed, avg_days_past_due=15) == {"s2": "Friendly", "s3": "Clear"}
    assert tones(workflow, mixed, avg_days_past_due=None) == {"s2": "Friendly", "s3": "Clear"}  # unknown → soft


def test_each_message_knows_what_happened_before_it():
    result = message_tones(example("Mid-Stage Channel-Aware Follow-Up"), EARLY_NEW)
    assert result["s5"]["path"] == "Case received → Firm email reminder → Wait 2 days → Email bounced? = yes"
    assert result["s6"]["path"] == "Case received → Firm email reminder → Wait 2 days → Email bounced? = no"


def test_broken_workflow_does_not_crash():
    assert message_tones({"entry_node_id": "missing", "steps": [message("s2")]}, EARLY_NEW) == {}
    loop = flow(message("s2", "s3"), {"id": "s3", "kind": "Wait", "next": "s2"})
    assert tones(loop, EARLY_NEW) == {"s2": "Friendly"}
