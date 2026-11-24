"""Adaptive workflows, AI side, segment 3: AI workflows come with their adaptive follow-ups.

Suggest and Regenerate add one message step per situation (not opened, opened but not paid, payment not finished,
part paid, no response), off the flowchart. Their text is written with the workflow's other messages (main text
and translations, no audience versions), in a tone the code decides, and reviewed with the workflow. The design
AI never sees them; learning reads the flowchart only. A follow-up without text is never used.
"""

import json
from unittest.mock import patch

from app.modules.payflow.services.workflow_suggestion import message_writer
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.adaptive_follow_ups import add_follow_ups
from app.modules.payflow.services.workflow_suggestion.context import load_client_context, load_rules
from app.modules.payflow.services.workflow_suggestion.learning import _candidate, _valid_today
from app.modules.payflow.services.workflow_suggestion.message_prompt import build_message_system_prompt
from app.modules.payflow.services.workflow_suggestion.message_validator import check_message
from app.modules.payflow.services.workflow_suggestion.next_action import decide_next_action
from app.modules.payflow.services.workflow_suggestion.rules import design_limits
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from app.infrastructure.database.models import PayflowStrategyModel
from tests.test_workflow_audience_messages import write_with
from tests.test_workflow_message_api import fake_message_ai, flowchart_messages, good_text
from tests.test_workflow_message_validator import EMAIL, brief
from tests.test_workflow_regenerate import REMARKS, admin, context  # noqa: F401 (fixture)
from tests.test_workflow_suggestion_api import fake_llm, portfolio  # noqa: F401 (fixture)

SITUATIONS = ["email_not_opened", "opened_not_clicked", "clicked_not_paid", "payment_failed", "payment_partial",
              "no_activity"]
FOLLOW_UP_IDS = [f"fu_{situation}" for situation in SITUATIONS]


def _suggest(db_session, admin, portfolio, writer=None):  # noqa: F811
    design, _ = fake_llm()
    with patch.object(suggestion_service, "call_llm", side_effect=design), \
            patch.object(message_writer, "call_llm", side_effect=writer or fake_message_ai()[0]):
        return WorkflowSuggestionService(db_session).suggest_workflows(
            admin, portfolio["client_id"], portfolio["portfolio_id"]
        )["workflows"][0]


def _follow_ups(workflow):
    return {step["id"]: step for step in workflow["steps"] if (step.get("config") or {}).get("follow_up_for")}


def _asked(prompt):
    """The writer's "Messages to write" lines, by step_id."""
    lines = prompt.split("Messages to write (one per step_id):\n", 1)[1].split("\n\n", 1)[0].splitlines()
    return {line["step_id"]: line for line in map(json.loads, lines)}


# --------------------------------------------------------------------------- Suggest


def test_suggest_adds_a_follow_up_for_each_situation_off_the_flowchart(db_session, admin, portfolio):  # noqa: F811
    workflow = _suggest(db_session, admin, portfolio)
    follow_ups = _follow_ups(workflow)
    assert list(follow_ups) == FOLLOW_UP_IDS
    for step_id, step in follow_ups.items():
        config = step["config"]
        assert step["kind"] == "Communication" and not any(step.get(link) for link in ("next", "yes", "no"))
        assert config["follow_up_for"] == step_id.removeprefix("fu_") and config["purpose"] == "Payment Reminder"
        assert config["body"] and config["sms_body"] and config["subject"] and config["tone"]  # email and SMS
        assert "versions" not in config and "brief" not in config  # no audience versions for follow-ups
        assert "channel" not in config  # the worker picks email or SMS
    assert workflow["stats"]["follow_ups"] == 6
    assert workflow["stats"]["emails"] == len(flowchart_messages(workflow))  # they are not counted as reminders
    line = context(workflow)["Adaptive follow-ups"]
    assert line.startswith("Sent in place of the next planned reminder, only to customers in that situation.")
    assert "Written: Follow-up: email not opened, Follow-up: opened, not paid" in line and "Not written" not in line


def test_the_writer_is_told_what_each_follow_up_is_for(db_session, admin, portfolio):  # noqa: F811
    writer, prompts = fake_message_ai()
    _suggest(db_session, admin, portfolio, writer)
    asked = _asked(prompts[0])
    for step_id in FOLLOW_UP_IDS:
        assert asked[step_id]["path_so_far"].startswith("Follow-up, not on the timeline: sent in place of the next")
    assert "opened the last email but did not go to the payment page" in asked["fu_opened_not_clicked"]["path_so_far"]
    assert "the payment did not go through (they saw it fail)" in asked["fu_payment_failed"]["path_so_far"]
    assert asked["fu_payment_partial"]["tone"] == "Supportive"
    assert asked["fu_no_activity"]["tone"] in ("Friendly", "Clear")  # never firmer than Clear
    assert "Never say or hint that you can see what the customer opened" in build_message_system_prompt()


def test_a_follow_up_the_writer_cannot_get_right_is_left_unwritten_not_the_workflow(db_session, admin, portfolio):  # noqa: F811,E501
    def writer(messages_, schema):
        asked = _asked(messages_[1]["content"])
        return {"messages": [{"step_id": step_id, **({"body": "pay now"} if step_id == "fu_no_activity"
                                                     else good_text(brief))} for step_id, brief in asked.items()]}

    workflow = _suggest(db_session, admin, portfolio, writer)
    assert all(config["body"] for config in flowchart_messages(workflow).values())  # the flowchart's messages
    follow_ups = _follow_ups(workflow)
    assert not follow_ups["fu_no_activity"]["config"].get("body")
    assert all(follow_ups[step_id]["config"]["body"] for step_id in FOLLOW_UP_IDS[:-1])  # all but "no response"
    assert "Messages" not in context(workflow)
    assert context(workflow)["Adaptive follow-ups"].endswith(
        "Not written yet, so never sent until written (Write messages): Follow-up: no response."
    )


def test_when_the_writer_is_down_the_follow_ups_wait_for_write_messages(db_session, admin, portfolio):  # noqa: F811
    design, _ = fake_llm()
    with patch.object(suggestion_service, "call_llm", side_effect=design):  # the writer stays off (conftest)
        workflow = WorkflowSuggestionService(db_session).suggest_workflows(
            admin, portfolio["client_id"], portfolio["portfolio_id"]
        )["workflows"][0]
    assert list(_follow_ups(workflow)) == FOLLOW_UP_IDS
    assert not any(step["config"].get("body") for step in _follow_ups(workflow).values())
    assert "Not written yet" in context(workflow)["Adaptive follow-ups"]
    # Not used while it has no text: the rules keep the plan.
    decision = decide_next_action(_rules_context(workflow, "opened_not_clicked"))
    assert decision["action"] == "keep_plan" and "has no text yet" in decision["reason"]

    written = write_with(db_session, admin, workflow["id"])
    assert all(step["config"]["body"] for step in _follow_ups(written).values())
    assert "Not written" not in context(written)["Adaptive follow-ups"]
    decision = decide_next_action(_rules_context(written, "opened_not_clicked"))
    assert (decision["action"], decision["follow_up_step_id"]) == ("use_follow_up", "fu_opened_not_clicked")


def test_write_messages_can_rewrite_one_follow_up(db_session, admin, portfolio):  # noqa: F811
    workflow = _suggest(db_session, admin, portfolio)
    writer, prompts = fake_message_ai()
    saved = write_with(db_session, admin, workflow["id"], writer, step_id="fu_clicked_not_paid")
    assert list(_asked(prompts[0])) == ["fu_clicked_not_paid"]
    assert _follow_ups(saved)["fu_clicked_not_paid"]["config"]["body"]


def test_a_follow_up_never_says_we_saw_what_the_customer_did():
    """Sentences the real model wrote before this check; a follow-up must read naturally either way."""
    base = {**brief(), "tone": "Clear", "follow_up": True}
    tracked = [
        "We noticed you haven’t opened our recent reminder about your balance.",
        "You opened our last email but haven’t completed the payment.",
        "It looks like you started the payment process but didn't finish.",
        "Your payment of {{outstanding_balance}} was not finished.",
        "You have not yet clicked the button in our last message.",
    ]
    for sentence in tracked:
        email = {**EMAIL, "body": EMAIL["body"].replace("Hi {{first_name}},", "Hi {{first_name}},\n" + sentence)}
        errors = check_message(email, base, [])
        assert len(errors) == 1 and "tells the customer we saw what they opened" in errors[0], sentence
    for fine in ("If something stopped you from paying, you can finish it now or choose a payment plan.",
                 "Your recent payment did not go through. You can try again or choose another way to pay."):
        email = {**EMAIL, "body": EMAIL["body"].replace("Hi {{first_name}},", "Hi {{first_name}},\n" + fine)}
        assert check_message(email, base, []) == [], fine  # a failed payment may be named: they saw it fail
    # The flowchart's messages are not checked for it (a plan reminder may say "you have started your plan").
    started = {**EMAIL, "body": EMAIL["body"].replace("Hi {{first_name}},", "Hi {{first_name}},\n" + tracked[1])}
    assert check_message(started, {**base, "follow_up": False}, []) == []


# --------------------------------------------------------------------------- Regenerate and learning


def test_regenerate_keeps_the_follow_ups_from_the_design_ai_and_a_reviewers_text(db_session, admin, portfolio):  # noqa: F811,E501
    original = _suggest(db_session, admin, portfolio)
    steps = json.loads(json.dumps(original["steps"]))
    edited = next(step for step in steps if step["id"] == "fu_payment_partial")
    edited["config"]["body"] = edited["config"]["body"] + "\nThank you for your payment."
    service = WorkflowSuggestionService(db_session)
    service.update_workflow(admin, original["id"], {"steps": steps})

    design, calls = fake_llm()
    with patch.object(suggestion_service, "call_llm", side_effect=design), \
            patch.object(message_writer, "call_llm", side_effect=fake_message_ai()[0]):
        new = service.regenerate_workflow(admin, original["id"], REMARKS)

    design_prompt = calls[0][1]["content"]
    assert "follow_up_for" not in design_prompt and "fu_" not in design_prompt  # the flowchart only
    assert list(_follow_ups(new)) == FOLLOW_UP_IDS  # added back by code
    kept = _follow_ups(new)["fu_payment_partial"]["config"]
    assert kept["body"].endswith("\nThank you for your payment.")  # the reviewer's text is kept
    assert "Follow-up: part paid" in context(new)["Reviewer text kept"]
    assert [item["label"] for item in new["ai_context"]].count("Adaptive follow-ups") == 1  # not the original's too


def test_learning_uses_workflows_with_follow_ups_and_shows_only_their_flowchart(db_session, admin, portfolio):  # noqa: F811,E501
    workflow = _suggest(db_session, admin, portfolio)
    row = db_session.get(PayflowStrategyModel, workflow["id"])
    limits = design_limits(load_client_context(db_session, row.client_id), load_rules(db_session, row.client_id))
    assert _valid_today(row, limits)  # the follow-ups are off the flowchart, not unreachable steps
    example = _candidate(db_session, row)["example"]
    assert example["steps"] and not any("follow_up_for" in step["config"] for step in example["steps"])


# --------------------------------------------------------------------------- which workflows get them


def _message(step_id, purpose, **links):
    return {"id": step_id, "kind": "Communication", "title": step_id, "config": {"purpose": purpose}, **links}


def test_only_a_workflow_with_an_ordinary_reminder_gets_follow_ups():
    plan_only = {"steps": [_message("s1", "Payment Plan Reminder", next="s2"),
                           _message("s2", "Promise-to-Pay Reminder")]}
    assert add_follow_ups(plan_only) == plan_only
    reminders = {"steps": [_message("s1", "Payment Plan Reminder", next="s2"), _message("s2", "Firm Reminder")]}
    assert [s["id"] for s in add_follow_ups(reminders)["steps"]] == ["s1", "s2", *FOLLOW_UP_IDS]


def test_follow_ups_already_there_are_kept_and_ids_never_clash():
    workflow = {"steps": [
        _message("s1", "Payment Reminder", next="fu_no_activity"),
        {"id": "fu_no_activity", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
        {**_message("mine", "Payment Reminder"), "config": {"purpose": "Payment Reminder", "follow_up_for": "email_not_opened",
                                                             "body": "kept"}},
    ]}
    steps = add_follow_ups(workflow)["steps"]
    follow_ups = {step["config"]["follow_up_for"]: step["id"] for step in steps if step["config"].get("follow_up_for")}
    assert follow_ups["email_not_opened"] == "mine" and steps[2]["config"]["body"] == "kept"
    assert follow_ups["no_activity"] == "fu_no_activity_2"
    assert len(follow_ups) == 6 and add_follow_ups({"steps": steps})["steps"] == steps  # adding again changes nothing


# --------------------------------------------------------------------------- helpers


def _rules_context(workflow, event_type):
    """What the backend gives decide_next_action for a run of `workflow` whose next step is its second message."""
    planned = [step for step in workflow["steps"] if step["kind"] == "Communication"
               and not step["config"].get("follow_up_for")]
    return {
        "trigger": "timer",
        "run": {"swaps": {}, "preferred_channel": None},
        "workflow": {"steps": workflow["steps"],
                     "follow_ups": {step["config"]["follow_up_for"]: step["id"] for step in _follow_ups(workflow).values()}},
        "next_planned": {"step_id": planned[1]["id"], "kind": "Communication", "at": "2099-01-01T00:00:00+00:00"},
        "account": {"payment_status": "Unpaid", "email_ok": True, "sms_ok": True},
        "new_events": [{"id": 1, "type": event_type, "step_id": planned[0]["id"]}],
        "messages_sent": [{"step_id": planned[0]["id"], "opened": True, "clicked": False}],
        "decisions": [],
    }
