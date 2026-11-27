"""Adaptive workflows: audience versions of the follow-ups (plan segment 7).

The follow-ups - first-time and repeat texts - get the same audience versions as the segment's other messages
(e.g. "Age 65 and over"), only when the segment has audiences. They are planned after the flowchart's messages,
one AI call per text (main, then each audience), so the flowchart's plan is unchanged and a failure leaves only
the follow-ups without a version: those customers get the follow-up's main text. Their versions are translated in
a call of their own, a single version can be rewritten, and a 65+ customer is sent the 65+ follow-up.
"""

from datetime import date
from unittest.mock import patch

from app.core.exceptions import ServiceUnavailableError
from app.infrastructure.database.models import PayflowAccountModel
from app.modules.payflow.services.workflow_suggestion import message_brief, message_writer
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from tests.test_adaptive_follow_ups import _asked
from tests.test_adaptive_next_action import _message, _send
from tests.test_adaptive_rule_fixes import _event, _run
from tests.test_workflow_audience_messages import SENIORS, add_seniors, fake_planner, write_with
from tests.test_workflow_message_api import fake_message_ai, flowchart_messages, messages
from tests.test_workflow_message_translation import TRANSLATE, fake_writer
from tests.test_workflow_regenerate import admin, ai_workflow, context  # noqa: F401 (fixtures)
from tests.test_workflow_suggestion_api import portfolio  # noqa: F401 (fixture)

PLAN_ASKED = "Write one brief for each message and audience: "


def _planned(call):
    """What a planner call asked for: {step_id: [audiences]}."""
    line = call[1]["content"].split(PLAN_ASKED)[1].split("\n")[0]
    return {part.split(" → ")[0]: part.split(" → ")[1].split(", ") for part in line.split("; ")}


def _follow_ups(workflow):
    return {step_id: config for step_id, config in messages(workflow).items() if config.get("follow_up_for")}


# --------------------------------------------------------------------------- versions, planned on their own


def test_follow_ups_get_the_segments_audience_versions(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio)  # a version for 65+
    planner, plans = fake_planner()
    saved = write_with(db_session, admin, ai_workflow["id"], fake_message_ai()[0], planner)

    flowchart, *follow_up_calls = map(_planned, plans)
    assert not any(step_id.startswith("fu_") for step_id in flowchart)  # the flowchart's plan as before
    assert all(audiences == ["main", "age_65_plus"] for audiences in flowchart.values())
    assert [set(sum(call.values(), [])) for call in follow_up_calls] == [{"main"}, {"age_65_plus"}]
    assert all(step_id.startswith("fu_") for call in follow_up_calls for step_id in call)

    follow_ups = _follow_ups(saved)
    assert len(follow_ups) == 14
    for step_id, config in follow_ups.items():
        version = config["versions"]["age_65_plus"]
        assert version["label"] == "Age 65 and over" and version["body"] and version["sms_body"], step_id
        assert version["brief"]["approach"] == "Approach for age_65_plus"
        assert config["brief"]["approach"] == "Approach for main"


def test_without_audiences_the_follow_ups_have_one_text(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    saved = write_with(db_session, admin, ai_workflow["id"])  # the fixture's accounts: no age group of their own
    assert all(config["body"] and "versions" not in config for config in _follow_ups(saved).values())


def test_when_the_follow_ups_cannot_be_planned_the_flowchart_keeps_its_versions(db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio)
    plan, _ = fake_planner()

    def planner(messages_, schema):
        if any(step_id.startswith("fu_") for step_id in _planned(messages_)):
            raise ServiceUnavailableError("AI is busy")
        return plan(messages_, schema)

    saved = write_with(db_session, admin, ai_workflow["id"], fake_message_ai()[0], planner)
    assert all("age_65_plus" in config["versions"] for config in flowchart_messages(saved).values())
    follow_ups = _follow_ups(saved)
    assert all(config["body"] and "versions" not in config for config in follow_ups.values())


def test_a_follow_up_version_that_fails_is_left_out(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio)
    write, _ = fake_message_ai()

    def writer(messages_, schema):
        prompt = messages_[1]["content"]
        if SENIORS in prompt and any(step_id.startswith("fu_") for step_id in _asked(prompt)):
            asked = _asked(prompt)
            # Tells the customer we saw what they did: refused for a follow-up, every time.
            return {"messages": [{"step_id": step_id, **write(messages_, schema)["messages"][0],
                                  "body": "We noticed you opened our email. {{payment_link}} {{brand_name}}"}
                                 for step_id in asked]}
        return write(messages_, schema)

    saved = write_with(db_session, admin, ai_workflow["id"], writer)
    assert all("age_65_plus" in config["versions"] for config in flowchart_messages(saved).values())
    assert all(config["body"] and "versions" not in config for config in _follow_ups(saved).values())
    assert context(saved)["Audiences"] == (
        "Own version for Age 65 and over (25 accounts); main text for the other 29 accounts"
    )


def test_one_follow_up_version_can_be_rewritten(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio)
    saved = write_with(db_session, admin, ai_workflow["id"])
    before = _follow_ups(saved)
    writer, prompts = fake_message_ai()
    planner, plans = fake_planner()
    with patch.object(message_writer, "call_llm", side_effect=writer), \
            patch.object(message_brief, "call_llm", side_effect=planner):
        rewritten = WorkflowSuggestionService(db_session).write_workflow_messages(
            admin, saved["id"], step_id="fu_no_activity_repeat", audience="age_65_plus", remarks="Warmer"
        )
    assert [_planned(call) for call in plans] == [{"fu_no_activity_repeat": ["age_65_plus"]}]
    assert len(prompts) == 1 and SENIORS in prompts[0]
    after = _follow_ups(rewritten)
    assert after["fu_no_activity_repeat"]["versions"]["age_65_plus"]["body"]
    assert {k: v for k, v in after.items() if k != "fu_no_activity_repeat"} == {
        k: v for k, v in before.items() if k != "fu_no_activity_repeat"}


def test_follow_up_versions_that_cannot_be_translated_leave_the_rest(db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio, language="FR")
    translate, _ = fake_writer()
    follow_up_translations = []

    def writer(messages_, schema):
        prompt = messages_[1]["content"]
        if TRANSLATE in prompt and '"step_id": "fu_' in prompt:
            follow_up_translations.append(prompt)
            if len(follow_up_translations) == 2:  # the main text's follow-ups first, then the 65+ version's
                raise ServiceUnavailableError("AI is busy")
        return translate(messages_, schema)

    planner, _ = fake_planner()
    saved = write_with(db_session, admin, ai_workflow["id"], writer, planner)
    flowchart = flowchart_messages(saved)
    assert all(config["versions"]["age_65_plus"]["translations"]["FR"] for config in flowchart.values())
    for config in _follow_ups(saved).values():
        assert config["translations"]["FR"]  # their main text is in French: what these customers get
        assert not config["versions"]["age_65_plus"].get("translations")


def test_the_note_names_the_follow_up_versions_that_were_not_translated():
    workflow_ = {"steps": [_message("s1", "Email 1"), _message("fu", "No response", follow_up_for="no_activity")]}
    versions = {(step_id, "age_65_plus"): {"label": "Age 65 and over", "body": "Hi"} for step_id in ("s1", "fu")}

    def translate(client_, workflow__, texts, code):
        if "fu" in texts:
            raise ServiceUnavailableError("AI is busy")
        return {step_id: {"body": "Salut"} for step_id in texts}

    with patch.object(suggestion_service, "translate_messages", side_effect=translate):
        done, failed = suggestion_service._translate_versions({}, workflow_, versions, ["FR"])
    assert failed == ["Age 65 and over follow-ups in French"]
    assert done[("s1", "age_65_plus")]["translations"] == {"FR": {"body": "Salut"}}
    assert "translations" not in done[("fu", "age_65_plus")]


# --------------------------------------------------------------------------- sent


def test_a_customer_aged_65_or_more_is_sent_the_65_plus_follow_up(db_session):
    follow_up = _message("f-unopened", "Not opened", follow_up_for="email_not_opened")
    follow_up["config"]["versions"] = {"age_65_plus": {"label": "Age 65 and over", "subject": "For you",
                                                       "body": "65+ email {{payment_link}}",
                                                       "sms_body": "{{brand_name}}: 65+ SMS {{payment_link}}"}}
    steps = [
        _message("s1", "Email 1", channel="Email", next="s2"),
        _message("s2", "Email 2", channel="Email", next="s3"),
        {"id": "s3", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
        follow_up,
    ]
    run = _run(db_session, steps, current="s2")
    db_session.get(PayflowAccountModel, run.account_id).date_of_birth = date(1950, 1, 1)
    _event(db_session, run, "email_not_opened")

    sent = _send(db_session, run, "s2")
    assert [(m.step_id, m.message_audience, m.channel) for m in sent] == [("f-unopened", "age_65_plus", "SMS")]
    assert "65+ SMS" in " ".join(sent[0].body_lines or [])
