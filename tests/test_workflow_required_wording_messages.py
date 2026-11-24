"""The AI includes the wording the client's compliance rules require (required_wording.py): the writer is told it,
the checks send back a message without it, translations keep it exactly as written, and reviewers see it on the
workflow. Without such rules nothing changes."""

import json
from unittest.mock import patch

import pytest

from app.core.exceptions import ServiceUnavailableError
from app.infrastructure.database.models import PayflowStrategyModel
from app.modules.payflow.services.email_subject_ai import PayflowEmailSubjectAiService
from app.modules.payflow.services.workflow_suggestion import message_brief, message_writer
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.message_validator import check_message, check_translation
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from tests.test_workflow_audience_messages import _post_update, add_seniors, fake_planner, write_with
from tests.test_workflow_message_api import fake_message_ai, messages
from tests.test_workflow_message_translation import TRANSLATE, _french, fake_writer
from tests.test_workflow_message_validator import EMAIL, SMS, brief
from tests.test_workflow_regenerate import REMARKS, admin, ai_workflow, context  # noqa: F401 (fixtures)
from tests.test_workflow_required_wording import DEBT_COLLECTOR, content_rule
from tests.test_workflow_suggestion_api import fake_llm, portfolio  # noqa: F401 (fixture)

LEGAL = "You have 30 days to dispute this debt before any legal action."  # numbers and threat words of its own


def with_wording(where=None, **wording):
    return {**brief(**({"channel": where} if where else {})), "required_wording": wording}


# --------------------------------------------------------------------------- the checks


def test_a_message_without_the_required_wording_is_sent_back():
    errors = check_message(EMAIL, with_wording(Email=[DEBT_COLLECTOR]), [])
    assert errors == [f's2: must include the required wording, word for word: "{DEBT_COLLECTOR}"']
    # Found the way the send check finds it: anywhere in the text, any case.
    ok = {**EMAIL, "body": EMAIL["body"] + "\n\n" + DEBT_COLLECTOR.upper()}
    assert check_message(ok, with_wording(Email=[DEBT_COLLECTOR]), []) == []


def test_each_channel_needs_its_own_wording():
    wording = {"Email": [DEBT_COLLECTOR], "SMS": ["SMS wording."]}
    sms = {"body": SMS["body"].replace(" Reply STOP", " SMS wording. Reply STOP")}
    assert check_message(sms, {**brief("SMS"), "required_wording": wording}, []) == []
    assert check_message(SMS, {**brief("SMS"), "required_wording": wording}, []) == [
        's2: must include the required wording, word for word: "SMS wording."'
    ]


def test_the_wording_itself_is_never_flagged_by_the_writing_rules():
    message = {**EMAIL, "body": EMAIL["body"] + "\n\n" + LEGAL}
    assert check_message(message, with_wording(Email=[LEGAL]), []) == []  # its number of days and "legal action"
    # The same words written by the AI elsewhere are still flagged.
    problems = check_message(message, brief(), [])
    assert any("number of hours / days" in p for p in problems) and any("threat" in p for p in problems)


def test_a_translation_keeps_the_wording_exactly_as_written():
    source = {"subject": EMAIL["subject"], "body": EMAIL["body"] + "\n\n" + DEBT_COLLECTOR}
    kept = {"subject": _french(source["subject"]), "body": _french(source["body"])}
    translated = {**kept, "body": kept["body"].replace(DEBT_COLLECTOR, "Ceci est une communication d'un agent.")}
    wording = {"Email": [DEBT_COLLECTOR]}
    assert check_translation("s2", kept, source, "Email", wording) == []
    assert check_translation("s2", translated, source, "Email", wording) == [
        f's2 (body): keep the required wording "{DEBT_COLLECTOR}" exactly as written - do not translate it'
    ]
    # An original without it (a person's text, say) is translated as it is.
    plain = {"subject": EMAIL["subject"], "body": EMAIL["body"]}
    assert check_translation("s2", {k: _french(v) for k, v in plain.items()}, plain, "Email", wording) == []


# --------------------------------------------------------------------------- writing a workflow's messages


def wording_writer(text):
    """fake_writer that puts `text` in every email body and SMS - as the AI does when it follows the prompt."""
    base, translations = fake_writer()
    prompts = []

    def call(messages_, schema):
        reply = base(messages_, schema)
        if TRANSLATE in messages_[1]["content"]:
            return reply
        prompts.append(messages_[1]["content"])
        for message in reply["messages"]:
            message["body"] += f"\n\n{text}"
            if message.get("sms_body"):
                message["sms_body"] = message["sms_body"].replace(" Reply STOP", f" {text} Reply STOP")
        return reply

    return call, prompts, translations


def test_without_rules_nothing_changes(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio, country_code="CA", province_state="ON")
    writer, prompts = fake_message_ai()
    saved = write_with(db_session, admin, ai_workflow["id"], writer)
    assert all("Required wording" not in prompt for prompt in prompts)
    assert all(item["label"] != "Required wording" for item in saved["ai_context"])


def test_the_ai_writes_the_wording_into_every_message_and_its_translations(db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio, country_code="CA", province_state="ON", language="FR")
    content_rule(db_session, DEBT_COLLECTOR)  # all of Canada, every channel
    writer, prompts, translations = wording_writer(DEBT_COLLECTOR)
    saved = write_with(db_session, admin, ai_workflow["id"], writer)

    assert f'- every email (in the body): "{DEBT_COLLECTOR}"' in prompts[0]
    assert f'- every SMS: "{DEBT_COLLECTOR}"' in prompts[0]
    assert translations and DEBT_COLLECTOR in translations[0][0]["content"]  # the translator is told to keep it
    for config in messages(saved).values():
        # The adaptive follow-ups too (they have no audience versions).
        for text in (config,) if config.get("follow_up_for") else (config, config["versions"]["age_65_plus"]):
            assert DEBT_COLLECTOR in text["body"] and DEBT_COLLECTOR in text["sms_body"]
            assert DEBT_COLLECTOR in text["translations"]["FR"]["body"]  # kept as written
    line = next(item["value"] for item in saved["ai_context"] if item["label"] == "Required wording")
    assert line == f'The client\'s compliance rules require, word for word: "{DEBT_COLLECTOR}" (emails and SMS)'


def test_suggest_and_regenerate_write_it_too(db_session, admin, portfolio):  # noqa: F811
    add_seniors(db_session, portfolio, country_code="CA", province_state="ON")
    content_rule(db_session, DEBT_COLLECTOR)
    design, _ = fake_llm()
    writer, _, _ = wording_writer(DEBT_COLLECTOR)
    planner, _ = fake_planner()
    with patch.object(suggestion_service, "call_llm", side_effect=design), \
            patch.object(message_writer, "call_llm", side_effect=writer), \
            patch.object(message_brief, "call_llm", side_effect=planner):
        service = WorkflowSuggestionService(db_session)
        suggested = service.suggest_workflows(admin, portfolio["client_id"], portfolio["portfolio_id"])["workflows"][0]
        regenerated = service.regenerate_workflow(admin, suggested["id"], REMARKS)
    for workflow_ in (suggested, regenerated):
        assert all(DEBT_COLLECTOR in config["body"] for config in messages(workflow_).values())
        assert context(workflow_)["Required wording"].endswith(f'"{DEBT_COLLECTOR}" (emails and SMS)')


def test_messages_without_the_wording_are_not_saved(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio, country_code="CA", province_state="ON")
    content_rule(db_session, DEBT_COLLECTOR)
    writer, prompts = fake_message_ai()  # never includes it
    with pytest.raises(ServiceUnavailableError, match="must include the required wording"):
        write_with(db_session, admin, ai_workflow["id"], writer)
    assert len(prompts) == 3  # the first answer and two fixes, each told what is missing


MISSING = f'must include the required wording, word for word: "{DEBT_COLLECTOR}"'


def test_a_persons_edit_without_the_wording_gets_a_warning(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio, country_code="CA", province_state="ON")
    content_rule(db_session, DEBT_COLLECTOR)
    writer, _, _ = wording_writer(DEBT_COLLECTOR)
    saved = write_with(db_session, admin, ai_workflow["id"], writer)
    steps = json.loads(json.dumps(saved["steps"]))
    step = next(s for s in steps if s["kind"] == "Communication")
    step["config"]["body"] = step["config"]["body"].replace(f"\n\n{DEBT_COLLECTOR}", "")  # the main email loses it
    version = step["config"]["versions"]["age_65_plus"]
    version["sms_body"] = version["sms_body"].replace(f" {DEBT_COLLECTOR}", "")  # and so does the 65+ SMS

    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()
    warnings = [(w["step_id"], w["text"], w["channel"]) for w in response.json()["text_warnings"]
                if w["problem"] == MISSING]
    assert warnings == [(step["id"], "main", "Email"), (step["id"], "age_65_plus", "SMS")]
    assert DEBT_COLLECTOR not in messages(response.json())[step["id"]]["body"]  # saved as written: a warning only


def test_an_edit_that_keeps_the_wording_gets_no_warning(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio, country_code="CA", province_state="ON")
    content_rule(db_session, DEBT_COLLECTOR)
    writer, _, _ = wording_writer(DEBT_COLLECTOR)
    saved = write_with(db_session, admin, ai_workflow["id"], writer)
    steps = json.loads(json.dumps(saved["steps"]))
    step = next(s for s in steps if s["kind"] == "Communication")
    step["config"]["body"] = step["config"]["body"].replace("Hi {{first_name}},", "Hello {{first_name}},")

    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200 and response.json()["text_warnings"] == [], response.json()


def test_the_email_proposal_checks_know_the_wording_too(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio, country_code="CA", province_state="ON")
    content_rule(db_session, DEBT_COLLECTOR, channel="Email")
    strategy = db_session.get(PayflowStrategyModel, ai_workflow["id"])
    step = next(s for s in json.loads(json.dumps(strategy.steps)) if s["kind"] == "Communication")
    problems = PayflowEmailSubjectAiService(db_session)._wording_problems(strategy, step, EMAIL["subject"],
                                                                          EMAIL["body"])
    assert any(DEBT_COLLECTOR in problem for problem in problems), problems
    with patch.object(message_writer, "call_llm", side_effect=AssertionError("no AI call")):
        assert not any(DEBT_COLLECTOR in p for p in PayflowEmailSubjectAiService(db_session)._wording_problems(
            strategy, step, EMAIL["subject"], EMAIL["body"] + "\n\n" + DEBT_COLLECTOR))
