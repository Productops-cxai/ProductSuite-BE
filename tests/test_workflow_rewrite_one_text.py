"""Write messages for one text of one message: POST /payflow/workflows/{id}/content with step_id and audience
("main" or an audience version such as "age_65_plus"). Only that text is planned, written and translated again;
the message's other texts and every other step stay exactly as they were."""

import json
from unittest.mock import patch

import pytest

from app.core.exceptions import ServiceUnavailableError, ValidationAppError
from app.modules.payflow.services.workflow_suggestion import message_brief, message_writer
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from tests.test_workflow_audience_messages import add_seniors, fake_planner, write_with
from tests.test_workflow_message_api import get, messages
from tests.test_workflow_message_translation import TRANSLATE, _french, fake_writer
from tests.test_workflow_regenerate import admin, ai_workflow  # noqa: F401 (fixtures)
from tests.test_workflow_suggestion_api import portfolio  # noqa: F401 (fixture)

MARK = "Rewritten."
SENIORS = "age_65_plus"


def marking_writer(translate=True):
    """fake_writer whose newly written texts carry MARK, so a rewritten text is told from the old one.
    Returns (call, writing prompts, translation calls)."""
    base, translations = fake_writer(translate)
    prompts = []

    def call(messages_, schema):
        reply = base(messages_, schema)
        if TRANSLATE in messages_[1]["content"]:
            return reply
        prompts.append(messages_[1]["content"])
        for message in reply["messages"]:
            message["body"] += f" {MARK}"
            if message.get("sms_body"):
                message["sms_body"] = message["sms_body"].replace(" Reply STOP", f" {MARK} Reply STOP")
        return reply

    return call, prompts, translations


def _written(db_session, admin, portfolio, ai_workflow, *, french=True):  # noqa: F811
    """The workflow with AI text: a main text and a 65+ version on every message (in French too with french)."""
    add_seniors(db_session, portfolio, **({"language": "FR"} if french else {}))
    writer, _ = fake_writer()
    return write_with(db_session, admin, ai_workflow["id"], writer)


def _first_message(workflow):
    return next(step for step in workflow["steps"] if step["kind"] == "Communication")


def _asked(planner_calls):
    """What the planner was asked to plan ("s3 → age_65_plus")."""
    user = planner_calls[0][1]["content"]
    return user.split("Write one brief for each message and audience: ")[1].split("\n")[0]


def rewrite(db_session, admin, workflow_id, step_id, audience, *, remarks=None, writer=None, planner=None):  # noqa: F811,E501
    writer_call, prompts, translations = writer or marking_writer()
    planner_call, plans = planner or fake_planner()
    with patch.object(message_writer, "call_llm", side_effect=writer_call), \
            patch.object(message_brief, "call_llm", side_effect=planner_call):
        saved = WorkflowSuggestionService(db_session).write_workflow_messages(
            admin, workflow_id, step_id=step_id, remarks=remarks, audience=audience
        )
    return saved, prompts, plans, translations


def test_rewriting_one_version_changes_only_that_version(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    saved = _written(db_session, admin, portfolio, ai_workflow)
    before = messages(get(client, admin_token, saved["id"]))  # as the API lists it
    step_id = _first_message(saved)["id"]
    writer, prompts, translations = marking_writer()
    planner, plans = fake_planner()
    with patch.object(message_writer, "call_llm", side_effect=writer), \
            patch.object(message_brief, "call_llm", side_effect=planner):
        response = client.post(f"/payflow/workflows/{saved['id']}/content",
                               json={"step_id": step_id, "audience": SENIORS},
                               headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200, response.json()
    after = messages(response.json())

    version = after[step_id]["versions"][SENIORS]
    assert version["body"].endswith(MARK) and version["sms_body"].endswith(f"{MARK} Reply STOP to opt out.")
    assert version["translations"]["FR"]["body"] == _french(version["body"])  # translated again
    assert version["label"] == "Age 65 and over" and version["brief"]["approach"] == f"Approach for {SENIORS}"
    # One plan and one text: only the 65+ version of this message, then one translation (French).
    assert len(plans) == 1 and _asked(plans) == f"{step_id} → {SENIORS}"
    assert len(prompts) == 1 and len(translations) == 1

    main = {key: value for key, value in after[step_id].items() if key != "versions"}
    assert main == {key: value for key, value in before[step_id].items() if key != "versions"}  # main text untouched
    assert {k: v for k, v in after.items() if k != step_id} == {k: v for k, v in before.items() if k != step_id}


def test_the_ai_improves_the_versions_own_text(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    saved = _written(db_session, admin, portfolio, ai_workflow, french=False)
    steps = json.loads(json.dumps(saved["steps"]))
    step = _first_message({"steps": steps})
    step["config"]["versions"][SENIORS]["body"] += "\nWe are here to help."  # a reviewer's wording in the version
    WorkflowSuggestionService(db_session).update_workflow(admin, saved["id"], {"steps": steps})

    saved, prompts, _, _ = rewrite(db_session, admin, saved["id"], step["id"], SENIORS, remarks="Shorter please")
    assert "We are here to help." in prompts[0] and "Shorter please" in prompts[0]  # its own text is improved
    assert "We are here to help." not in messages(saved)[step["id"]]["body"]  # never the main text's
    assert saved["versions"][0]["note"] == (
        f"Message text written by AI for {admin.full_name}: Age 65 and over version of {step['title']} "
        "(remarks: Shorter please)"
    )


def test_rewriting_the_main_text_keeps_the_versions(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    saved = _written(db_session, admin, portfolio, ai_workflow)
    before, step_id = messages(saved), _first_message(saved)["id"]

    saved, prompts, plans, _ = rewrite(db_session, admin, saved["id"], step_id, "main")
    after = messages(saved)
    assert after[step_id]["body"].endswith(MARK) and after[step_id]["content_origin"] == "AI Generated"
    assert after[step_id]["translations"]["FR"]["body"] == _french(after[step_id]["body"])
    assert after[step_id]["brief"]["approach"] == "Approach for main"
    assert after[step_id]["versions"] == before[step_id]["versions"]  # every version as it was
    assert _asked(plans) == f"{step_id} → main" and len(prompts) == 1
    assert {k: v for k, v in after.items() if k != step_id} == {k: v for k, v in before.items() if k != step_id}
    assert saved["versions"][0]["note"].endswith(f"main text of {_first_message(saved)['title']}")


def test_a_translation_that_fails_is_noted_and_the_version_is_kept(db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    saved = _written(db_session, admin, portfolio, ai_workflow)
    step_id = _first_message(saved)["id"]

    saved, _, _, _ = rewrite(db_session, admin, saved["id"], step_id, SENIORS, writer=marking_writer(translate=False))
    version = messages(saved)[step_id]["versions"][SENIORS]
    assert version["body"].endswith(MARK) and "translations" not in version  # the old French was outdated
    assert saved["versions"][0]["note"].endswith(
        "; not translated: Age 65 and over in French - those customers get the main text"
    )


def test_a_version_that_cannot_be_planned_is_not_written(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    saved = _written(db_session, admin, portfolio, ai_workflow, french=False)
    step_id = _first_message(saved)["id"]
    broken = (lambda *_: (_ for _ in ()).throw(ServiceUnavailableError("AI is down")), [])

    with pytest.raises(ServiceUnavailableError, match="could not plan the Age 65 and over version"):
        rewrite(db_session, admin, saved["id"], step_id, SENIORS, planner=broken)
    stored = WorkflowSuggestionService(db_session).strategies.get_strategy(admin, saved["id"])
    assert messages(stored) == messages(saved)  # nothing saved


def _no_ai(*_):
    raise AssertionError("the AI must not be called")


@pytest.mark.parametrize(
    "body, error",
    [
        ({"audience": SENIORS}, "Choose the message (step_id) whose text to rewrite"),
        ({"step_id": "nope", "audience": SENIORS}, "Step nope is not an email or SMS step of this workflow"),
        ({"step_id": "{message}", "audience": "age_18_34"}, "has no Age 18 – 34 version"),
        ({"step_id": "{message}", "audience": "everyone"}, "has no everyone version"),
    ],
)
def test_a_text_that_does_not_exist_is_refused_before_any_ai_call(client, admin_token, db_session, admin, portfolio, ai_workflow, body, error):  # noqa: F811,E501
    saved = _written(db_session, admin, portfolio, ai_workflow, french=False)  # only a 65+ version
    body = {key: value.replace("{message}", _first_message(saved)["id"]) for key, value in body.items()}
    with patch.object(message_writer, "call_llm", side_effect=_no_ai), \
            patch.object(message_brief, "call_llm", side_effect=_no_ai):
        response = client.post(f"/payflow/workflows/{saved['id']}/content", json=body,
                               headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 400 and error in response.json()["detail"], response.json()


def test_the_service_refuses_an_audience_without_a_message(db_session, admin, ai_workflow):  # noqa: F811
    with pytest.raises(ValidationAppError, match="Choose the message"):
        WorkflowSuggestionService(db_session).write_workflow_messages(admin, ai_workflow["id"], audience="main")
