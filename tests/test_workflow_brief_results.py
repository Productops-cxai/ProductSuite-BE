"""The planner (message_brief.py, AI call 1) uses this client's measured email results (email_subject_ai
.performance_context) when there are any - the same best / weak subjects the writer gets - and never claims results
when there are none."""

import json
from unittest.mock import patch

from app.modules.payflow.services.workflow_suggestion import message_brief, message_writer
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.message_brief import MAIN, check_brief, large_balance_share
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from tests.test_workflow_audience_messages import add_seniors, fake_planner
from tests.test_workflow_message_api import fake_message_ai
from tests.test_workflow_message_brief import CLIENT, FOUND, SEGMENT, WORKFLOW, fake_ai, good
from tests.test_workflow_regenerate import admin, ai_workflow, context  # noqa: F401 (fixtures)
from tests.test_workflow_suggestion_api import portfolio  # noqa: F401 (fixture)

BEST = {"subject": "A quick reminder about your {{brand_name}} balance", "reminder_stage": "First reminder",
        "purpose": "Payment Reminder", "delivered": 820, "open_rate": "41.2%", "click_rate": "9.8%",
        "completed_payments": 34, "conversion": "4.1%"}
WEAK = {"subject": "Your account needs attention", "reminder_stage": "First reminder",
        "purpose": "Payment Reminder", "delivered": 640, "open_rate": "22.0%", "click_rate": "2.1%",
        "completed_payments": 6, "conversion": "0.9%"}
PERFORMANCE = {"best": [BEST], "weak": [WEAK], "min_deliveries": 100}


def planner_prompts(client):
    call, prompts = fake_ai()
    with patch.object(message_brief, "call_llm", side_effect=call):
        message_brief.plan_messages(client, WORKFLOW, SEGMENT, FOUND)
    return " ".join(prompts[0][0]["content"].split()), prompts[0][1]["content"]


def test_the_planner_builds_on_this_clients_measured_results():
    system, user = planner_prompts({**CLIENT, "email_performance": PERFORMANCE})
    assert "Measured results (this client's own past emails; each subject with at least 100 delivered):" in user
    assert f"Best subjects:\n{json.dumps(BEST, ensure_ascii=False)}" in user
    assert f"Weak subjects:\n{json.dumps(WEAK, ensure_ascii=False)}" in user
    assert "## Measured results" in system and "Never copy a subject word for word" in system
    assert "Judge by conversion together with delivered volume, never by raw counts" in system
    assert "never that it will work or that it caused payments" in system
    assert "Never claim a result the measured numbers do not show, and never promise one." in system
    assert "There are no results from earlier messages yet" not in system
    # Each message says its reminder stage, as the results do, so they can be matched.
    lines = [json.loads(line) for line in user.split("Messages (in order along the workflow):\n")[1]
             .split("\n\n")[0].splitlines()]
    assert {line["step_id"]: line["reminder_stage"] for line in lines} == {"s2": "First reminder",
                                                                          "s4": "Final reminder"}


def test_without_results_the_planner_never_claims_any():
    for performance in (None, {"best": [], "weak": [], "min_deliveries": 100}):
        system, user = planner_prompts({**CLIENT, "email_performance": performance})
        assert "Measured results" not in user and "## Measured results" not in system
        assert "There are no results from earlier messages yet" in system and "never claim results" in system


def test_an_audience_with_mostly_large_balances_does_not_lead_with_paying_in_full():
    options = ["pay in full", "pay part now", "set up a payment plan"]
    full = good("s2", MAIN, "Clear", lead_option="pay in full")
    assert check_brief("s2 (main)", full, ["Clear"], options, "Active", large_balances=69) == [
        "s2 (main): 69% of this audience owe $1,500 or more - lead with pay part now or set up a payment plan, "
        "not paying in full (it can still be mentioned)"
    ]
    assert check_brief("x", full, ["Clear"], options, "Active", large_balances=30) == []  # mostly smaller
    assert check_brief("x", full, ["Clear"], ["pay in full"], "Active", large_balances=90) == []  # nothing smaller
    # Already on a plan: the smaller step is paying part now (never a new plan).
    assert "lead with pay part now, not" in check_brief("x", full, ["Clear"], options, "Payment Plan", 80)[0]
    # The share comes from the audience's balance bands.
    assert large_balance_share({"balance_bands": {"$5,000 and over": 40, "$1,500 – $5,000": 29, "Under $500": 31}}) == 69
    assert large_balance_share({}) is None


def test_why_never_claims_a_preference_a_cause_or_a_promise():
    options = ["pay in full", "pay part now", "set up a payment plan"]
    for claim in ("Young adults prefer brief messages.", "This subject drove the best conversion.",
                  "Older customers respond better to a calm tone.", "It will increase payments.",
                  "That wording proved effective.", "Older customers responded best to a gentle tone.",
                  "Young adults favor concise messages."):
        errors = check_brief("x", good("s2", MAIN, "Clear", why=claim), ["Clear"], options, "Active")
        assert len(errors) == 1 and "a preference, a cause or a promise the numbers do not show" in errors[0], claim
    honest = 'The calm first-reminder subject did better (4.1% conversion on 820 delivered); short, per the guidance.'
    assert check_brief("x", good("s2", MAIN, "Clear", why=honest), ["Clear"], options, "Active") == []


def test_the_planner_sends_back_paying_in_full_for_large_balances():
    def lead_in_full_first(step_id, audience, call_number):
        return {"lead_option": "pay in full"} if call_number == 1 and audience == "age_65_plus" else {}

    call, prompts = fake_ai(lead_in_full_first)
    with patch.object(message_brief, "call_llm", side_effect=call):
        plans = message_brief.plan_messages(CLIENT, WORKFLOW, SEGMENT, FOUND)  # every account owes $2,400
    assert len(prompts) == 2 and "100% of this audience owe $1,500 or more" in prompts[1][1]["content"]
    assert plans["s2"]["age_65_plus"]["lead_option"] == "set up a payment plan"


def test_write_messages_gives_the_results_to_the_planner_and_the_writer(db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio)
    planner, plans = fake_planner()
    writer, writes = fake_message_ai()
    with patch.object(suggestion_service, "_email_performance", return_value=PERFORMANCE), \
            patch.object(message_brief, "call_llm", side_effect=planner), \
            patch.object(message_writer, "call_llm", side_effect=writer):
        saved = WorkflowSuggestionService(db_session).write_workflow_messages(admin, ai_workflow["id"])

    assert BEST["subject"] in plans[0][1]["content"] and "## Measured results" in plans[0][0]["content"]
    assert all(BEST["subject"] in prompt for prompt in writes)  # the main text and the 65+ version
    lines = context(saved)
    assert lines["Email performance"].startswith("Emails written using this client's measured results")
    assert lines["Audiences"].startswith("Own version for Age 65 and over (25 accounts)")
