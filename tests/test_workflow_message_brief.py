"""AI call 1 of message writing (message_brief.py): a brief per message and audience - approach, tone,
length, subject style, lead payment option, call to action, why. Code sets the limits and checks every
brief; the AI sees totals only. Planning never blocks: invalid briefs are left out, AI down → no briefs."""

import json

import pytest

from app.core.exceptions import ServiceUnavailableError
from app.modules.payflow.services.workflow_suggestion import message_brief
from app.modules.payflow.services.workflow_suggestion.audiences import find_audiences
from app.modules.payflow.services.workflow_suggestion.message_brief import (
    MAIN,
    allowed_tones,
    check_brief,
    plan_messages,
)
from tests.test_workflow_audiences import TODAY, born
from tests.test_workflow_message_tone import end, flow, message
from tests.test_workflow_segmenter import make_account

OPTIONS = ["pay in full", "pay part now", "set up a payment plan"]
CLIENT = {"name": "Northstar Utilities", "client_type": "first_party", "language": "English", "currency": "CAD",
          "payment_options": OPTIONS}
WAIT = {"id": "s3", "kind": "Wait", "next": "s4",
        "config": {"amount": 3, "unit": "Days", "direction": "After", "reference_event": "Previous Action"}}
# 30 – 59 days, contacted before: s2 is Firm (Clear allowed); s4 hands over next → Final notice (fixed).
WORKFLOW = {"name": "Mid-stage follow-up", "summary": "Two reminders, then a review.",
            **flow(message("s2", "s3"), WAIT, message("s4", "s5", purpose="Firm Reminder"), end("s5", "Human Review"))}
SEGMENT = {
    "label": "Active · 30 – 59 days past due · Contacted, nothing paid · $1,500 – $5,000",
    "segment": {"situation": "Active", "delinquency": "30 – 59 days past due", "contact_state": "contacted_no_payment",
                "balance_band": "$1,500 – $5,000"},
    "stats": {"avg_days_past_due": 44},
}
PRIVATE = dict(customer_name="Jane Doe", email="jane.doe@example.com", phone_mobile="4165550199")


def accounts():
    """40 aged 65+, 30 aged 18 – 34, 25 without a date of birth - all in Ontario, Canada."""
    common = dict(province_state="ON", country_code="CA", outstanding_balance=2400.0, **PRIVATE)
    return ([make_account(date_of_birth=born(70), **common) for _ in range(40)]
            + [make_account(date_of_birth=born(25), **common) for _ in range(30)]
            + [make_account(**common) for _ in range(25)])


FOUND = find_audiences(accounts(), TODAY)  # versions: age_65_plus, age_18_34; main: the 25 without an age


def good(step_id, audience, tone, **changes):
    brief = {"step_id": step_id, "audience": audience, "approach": "Plain and calm, one step at a time.",
             "tone": tone, "length": "short", "subject_style": "Calm and specific to the account.",
             "lead_option": "set up a payment plan", "call_to_action": "One payment link near the top.",
             "why": "A $2,400 average balance feels doable as monthly payments."}
    return {**brief, **changes}


def fake_ai(change=None):
    """Answers every asked (step, audience) with a valid brief in the first allowed tone.

    `change(step_id, audience, call_number)` → dict of field changes, or None to leave the brief out."""
    prompts = []

    def call(messages, schema):
        prompts.append(messages)
        user = messages[1]["content"]
        allowed = {line["step_id"]: line["allowed_tones"] for line in map(json.loads, _message_lines(user))}
        asked = user.split("Write one brief for each message and audience: ")[1].split("\n")[0]
        briefs = []
        for part in asked.split("; "):
            step_id, audiences = part.split(" → ")
            for audience in audiences.split(", "):
                changes = change(step_id, audience, len(prompts)) if change else {}
                if changes is not None:
                    briefs.append({**good(step_id, audience, allowed[step_id][0]), **changes})
        return {"briefs": briefs}

    return call, prompts


def _message_lines(user_prompt):
    section = user_prompt.split("Messages (in order along the workflow):\n")[1].split("\n\n")[0]
    return section.splitlines()


def plan(call, **kwargs):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(message_brief, "call_llm", call)
        return plan_messages(CLIENT, WORKFLOW, SEGMENT, kwargs.pop("found", FOUND), **kwargs)


def test_tone_may_stay_or_go_one_step_softer_never_firmer():
    assert allowed_tones("Friendly") == ["Friendly"]
    assert allowed_tones("Clear") == ["Clear", "Friendly"]
    assert allowed_tones("Firm") == ["Firm", "Clear"]
    for fixed in ("Final notice", "Supportive", "Neutral"):
        assert allowed_tones(fixed) == [fixed]


def test_every_message_is_planned_for_every_audience_in_one_call():
    call, prompts = fake_ai()
    plans = plan(call)
    assert len(prompts) == 1
    assert {step_id: sorted(by_audience) for step_id, by_audience in plans.items()} == {
        "s2": sorted([MAIN, "age_65_plus", "age_18_34"]),
        "s4": sorted([MAIN, "age_65_plus", "age_18_34"]),
    }
    assert plans["s2"]["age_65_plus"]["tone"] == "Firm" and plans["s4"][MAIN]["tone"] == "Final notice"
    assert set(plans["s2"][MAIN]) == {"approach", "tone", "length", "subject_style", "lead_option",
                                      "call_to_action", "why"}


def test_the_prompt_has_totals_the_rules_and_never_one_person():
    call, prompts = fake_ai()
    plan(call)
    system, user = " ".join(prompts[0][0]["content"].split()), prompts[0][1]["content"]
    assert "age_65_plus (Age 65 and over): 40 accounts." in user
    assert "age_18_34 (Age 18 – 34): 30 accounts." in user
    assert f"{MAIN} (the main text): 25 accounts" in user
    assert '"regions": {"Ontario": 100}' in user and "- Canada: amounts are in Canadian dollars" in user
    assert "the customer can pay in full, pay part now, set up a payment plan" in user
    assert '"allowed_tones": ["Firm", "Clear"]' in user and '"allowed_tones": ["Final notice"]' in user
    for rule in ("never make a message firmer because of who the customer is",
                 "Never mention or hint at the customer's age", "no emojis", "never claim results",
                 "65 and over: respectful"):
        assert rule in system
    everything = system + user
    for private in PRIVATE.values():
        assert private not in everything


def test_bad_briefs_go_back_to_the_ai_with_the_problems():
    def change(step_id, audience, call_number):
        if call_number > 1:
            return {}
        if (step_id, audience) == ("s2", MAIN):
            return {"tone": "Friendly"}  # two steps softer than Firm
        if (step_id, audience) == ("s4", "age_65_plus"):
            return {"tone": "Clear"}  # Final notice is fixed
        if (step_id, audience) == ("s2", "age_18_34"):
            return {"lead_option": "pay next year"}
        if (step_id, audience) == ("s4", MAIN):
            return None  # left out
        return {}

    call, prompts = fake_ai(change)
    plans = plan(call)
    assert len(prompts) == 2
    retry = prompts[1][1]["content"]
    assert "Your previous briefs for these had problems" in retry
    assert "s2 (main): tone must be Firm or Clear" in retry
    assert "s4 (age_65_plus): tone must be Final notice" in retry
    assert "s2 (age_18_34): lead_option must be one the payment page offers" in retry
    assert "s4 (main): no brief was written" in retry
    # Only the four failing briefs were asked again.
    assert retry.split("Write one brief for each message and audience: ")[1].split("\n")[0] == (
        "s2 → main, age_18_34; s4 → main, age_65_plus"
    )
    assert all(len(by_audience) == 3 for by_audience in plans.values())


def test_a_brief_that_stays_bad_is_left_out_and_the_rest_are_kept():
    call, prompts = fake_ai(lambda s, a, n: {"tone": "Friendly"} if (s, a) == ("s2", MAIN) else {})
    plans = plan(call)
    assert len(prompts) == 3  # first answer + 2 fixes
    assert MAIN not in plans["s2"] and len(plans["s4"]) == 3


def test_ai_down_means_no_briefs_and_no_error():
    def down(messages, schema):
        raise ServiceUnavailableError("AI service could not be reached")

    assert plan(down) == {}


def test_only_the_asked_messages_and_a_small_group_gets_only_the_main_brief():
    call, _ = fake_ai()
    assert list(plan(call, step_ids=["s4"])) == ["s4"]
    small = find_audiences([make_account(date_of_birth=born(70)) for _ in range(12)], TODAY)
    call, prompts = fake_ai()
    plans = plan(call, found=small)
    assert {step_id: list(by_audience) for step_id, by_audience in plans.items()} == {"s2": [MAIN], "s4": [MAIN]}
    assert "Country notes" not in prompts[0][1]["content"]  # no country on these accounts


def test_a_country_without_notes_is_kept_neutral():
    found = find_audiences([make_account(country_code="IN") for _ in range(5)], TODAY)
    call, prompts = fake_ai()
    plan(call, found=found)
    assert "- IN: no notes for this country - keep it neutral, polite and plain." in prompts[0][1]["content"]


def test_lead_option_rules():
    brief = good("s2", MAIN, "Neutral", lead_option="pay in full")
    # Not a payment request (Neutral): no payment option to lead with.
    assert check_brief("s2 (main)", brief, ["Neutral"], OPTIONS, "Active") == [
        's2 (main): this message is not a payment request - lead_option must be "none"'
    ]
    assert check_brief("s2 (main)", {**brief, "lead_option": "none"}, ["Neutral"], OPTIONS, "Active") == []
    # Already on a plan: never lead with a new one.
    plan_brief = good("s2", MAIN, "Supportive")
    assert check_brief("x", plan_brief, ["Supportive"], OPTIONS, "Payment Plan") == [
        "x: these customers are already on a payment plan - never lead with a new plan"
    ]
    assert check_brief("x", {**plan_brief, "lead_option": "pay part now"}, ["Supportive"], OPTIONS, "Payment Plan") == []
    # Empty text and an unknown length are problems too.
    errors = check_brief("x", {**plan_brief, "why": "", "length": "huge"}, ["Supportive"], OPTIONS, "Active")
    assert errors == ["x: length must be one of short, medium, long", "x: why is empty"]


def test_the_writers_fields_never_type_an_amount():
    """The numbers are the audience's averages: "pay part of your $12,600 balance" would be wrong for most."""
    brief = good("s2", MAIN, "Clear", call_to_action="Pay part of your $12,600 balance with the button below.")
    assert check_brief("x", brief, ["Clear"], OPTIONS, "Active") == [
        'x: call_to_action types an amount - the numbers are averages, not one customer\'s: say "their balance"'
    ]
    # The reason may quote the numbers (reviewers read it).
    assert check_brief("x", good("s2", MAIN, "Clear", why="Average balance $12,600: a plan feels doable."),
                       ["Clear"], OPTIONS, "Active") == []
