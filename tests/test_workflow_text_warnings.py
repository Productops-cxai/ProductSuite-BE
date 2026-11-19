"""POST /payflow/workflows/{id}/update checks every text a person changed - a main text or an audience version - with
the same checks the AI's own text passes. Problems come back in "text_warnings"; the save always goes through."""

import json

from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from tests.test_workflow_audience_messages import _post_update, add_seniors, write_with
from tests.test_workflow_message_api import get, messages
from tests.test_workflow_regenerate import admin, ai_workflow  # noqa: F401 (fixtures)
from tests.test_workflow_suggestion_api import portfolio  # noqa: F401 (fixture)

NO_LINK = "must include {{payment_link}}"
NO_STOP = "SMS must end with 'Reply STOP to opt out.'"


def _written(db_session, admin, portfolio, ai_workflow, *, seniors=False):  # noqa: F811
    """The workflow with AI text (and a 65+ version on every message with seniors), and a copy of its steps."""
    if seniors:
        add_seniors(db_session, portfolio)
    saved = write_with(db_session, admin, ai_workflow["id"])
    return saved, json.loads(json.dumps(saved["steps"]))


def _first_message(steps):
    return next(step for step in steps if step["kind"] == "Communication")


def _save(client, token, workflow_id, steps):
    response = _post_update(client, token, workflow_id, steps)
    assert response.status_code == 200, response.json()
    return response.json()


def test_a_missing_payment_link_is_a_warning_and_the_text_is_still_saved(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    saved, steps = _written(db_session, admin, portfolio, ai_workflow)
    step = _first_message(steps)
    step["config"]["body"] = step["config"]["body"].replace("{{payment_link}}\n\n", "")

    body = _save(client, admin_token, saved["id"], steps)
    assert {"step_id": step["id"], "text": "main", "channel": "Email", "problem": NO_LINK} in body["text_warnings"]
    assert messages(body)[step["id"]]["body"] == step["config"]["body"]  # saved as written: a warning, not a block
    assert "text_warnings" not in get(client, admin_token, saved["id"])  # only the save's answer has them


def test_good_text_gives_no_warnings(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    saved, steps = _written(db_session, admin, portfolio, ai_workflow)
    step = _first_message(steps)
    step["config"]["body"] = step["config"]["body"].replace("Hi {{first_name}},", "Hello {{first_name}},")

    assert _save(client, admin_token, saved["id"], steps)["text_warnings"] == []


def test_only_the_texts_a_save_changed_are_checked(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    saved, steps = _written(db_session, admin, portfolio, ai_workflow)
    _first_message(steps)["config"]["body"] += "\nWe may take legal action."
    first = _save(client, admin_token, saved["id"], steps)
    assert [w["problem"] for w in first["text_warnings"]] == ["'legal action' sounds like a threat - remove it"]

    steps = json.loads(json.dumps(first["steps"]))
    steps[-1]["title"] = f"{steps[-1]['title']} (renamed)"  # the threat is still there, but not changed now
    assert _save(client, admin_token, saved["id"], steps)["text_warnings"] == []


def test_a_version_is_checked_on_its_own_with_its_sms(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    saved, steps = _written(db_session, admin, portfolio, ai_workflow, seniors=True)
    step = _first_message(steps)
    version = step["config"]["versions"]["age_65_plus"]
    version["sms_body"] = version["sms_body"].replace(" Reply STOP to opt out.", "")

    body = _save(client, admin_token, saved["id"], steps)
    assert body["text_warnings"] == [
        {"step_id": step["id"], "text": "age_65_plus", "channel": "SMS", "problem": NO_STOP}
    ]
    assert messages(body)[step["id"]]["versions"]["age_65_plus"]["sms_body"] == version["sms_body"]


def test_an_sms_only_step_is_checked_as_an_sms(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    saved, steps = _written(db_session, admin, portfolio, ai_workflow)
    step = _first_message(steps)
    step["config"].update({"channel": "SMS", "template_id": "", "subject": "Leftover subject", "sms_body": None,
                           "body": "{{brand_name}}: your balance is open. Pay: {{payment_link}}"})

    body = _save(client, admin_token, saved["id"], steps)
    assert body["text_warnings"] == [{"step_id": step["id"], "text": "main", "channel": "SMS", "problem": NO_STOP}]


def test_hand_made_workflows_are_checked_too(client, admin_token, db_session, admin, portfolio):  # noqa: F811
    strategies = PayflowStrategyService(db_session)
    workflow = strategies.create_strategy(admin, {
        "name": "Manual workflow", "client_id": portfolio["client_id"],
        "portfolio_id": portfolio["portfolio_id"], "steps": load_examples()[0]["steps"],
    })
    steps = json.loads(json.dumps(workflow["steps"]))
    template_step = _first_message(steps)
    template_step["title"] = f"{template_step['title']} (renamed)"  # a step that sends its template: nothing to check
    assert _save(client, admin_token, workflow["id"], steps)["text_warnings"] == []

    template_step["config"].update({"channel": "Email", "subject": "Your balance",
                                    "body": "Hi {{first_name}}, please pay {{outstanding_balance}}:\n\n"
                                            "{{payment_link}}\n\nThank you."})
    problems = [w["problem"] for w in _save(client, admin_token, workflow["id"], steps)["text_warnings"]]
    assert any("{{brand_name}}" in problem for problem in problems), problems
