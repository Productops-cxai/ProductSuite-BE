"""A reviewer changes a message step's channel or purpose: its age versions were written for the old one, so the save
removes them (an SMS-only step would otherwise send a version's email body to that age group). Versions sent with
new text stay, and a save that keeps the channel and purpose keeps every version, as before."""

import json
from datetime import date

import pytest

from tests.test_workflow_audience_messages import _post_update, _texts, add_seniors, write_with
from tests.test_workflow_audience_sending import SENIOR_BORN, sent_text
from tests.test_workflow_message_text import _account, _send
from tests.test_workflow_regenerate import admin, ai_workflow  # noqa: F401 (fixtures)
from tests.test_workflow_suggestion_api import portfolio  # noqa: F401 (fixture)

NEW_SMS = "{{brand_name}}: a new note about your account. Your options: {{payment_link}} Reply STOP to opt out."


def _written(db_session, admin, portfolio, ai_workflow, *, young=False):  # noqa: F811
    """The workflow with AI text: a main text and a 65+ version on every message (and a 25 - 34 one with young).
    Returns it and a copy of its steps to edit."""
    add_seniors(db_session, portfolio)
    if young:
        add_seniors(db_session, portfolio, start=600, born=date(2000, 1, 1))
    saved = write_with(db_session, admin, ai_workflow["id"])
    return saved, json.loads(json.dumps(saved["steps"]))


def _first_message(steps):
    return next(step for step in steps if step["kind"] == "Communication")


def _change(step, **patch):
    """The step as the step editor saves it after a channel / purpose change: its own text cleared
    (clearMessageTextPatch), everything else - its versions too - sent back as it was."""
    step["config"].update({"subject": None, "body": None, "sms_body": None, "tone": None, "content_origin": None,
                           **patch})
    return step


@pytest.mark.parametrize("channel", ["SMS", "Email"])
def test_a_new_channel_removes_the_steps_versions(client, admin_token, db_session, admin, portfolio, ai_workflow, channel):  # noqa: F811,E501
    saved, steps = _written(db_session, admin, portfolio, ai_workflow)
    before = _texts(saved)
    step = _change(_first_message(steps), channel=channel, template_id="")
    assert step["config"]["versions"]["age_65_plus"]["body"]  # sent back with the change

    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()
    after = _texts(response.json())
    assert after[step["id"]] == {}  # no text, no AI plan, no versions: the reviewer writes the new message
    assert all(after[other] == before[other] for other in after if other != step["id"])  # the others keep theirs


def test_a_new_purpose_removes_them_too(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811
    saved, steps = _written(db_session, admin, portfolio, ai_workflow)
    step = _first_message(steps)
    purpose = next(p for p in ("Firm Reminder", "Payment Reminder") if p != step["config"].get("purpose"))
    _change(step, purpose=purpose)

    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()
    assert _texts(response.json())[step["id"]] == {}


def test_a_client_that_does_not_send_versions_does_not_get_them_back(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    saved, steps = _written(db_session, admin, portfolio, ai_workflow)
    step = _change(_first_message(steps), channel="SMS", template_id="", body=NEW_SMS)
    step["config"].pop("versions")  # an older frontend
    step["config"].pop("brief", None)

    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()
    assert _texts(response.json())[step["id"]] == {"body": NEW_SMS}


def test_a_version_sent_with_new_text_stays(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    saved, steps = _written(db_session, admin, portfolio, ai_workflow, young=True)
    step = _change(_first_message(steps), channel="SMS", template_id="", body=NEW_SMS)
    assert set(step["config"]["versions"]) == {"age_65_plus", "age_25_34"}
    step["config"]["versions"]["age_65_plus"]["body"] = NEW_SMS  # rewritten for SMS with the change

    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()
    versions = _texts(response.json())[step["id"]]["versions"]
    assert set(versions) == {"age_65_plus"}  # the 25 - 34 version still had the old text
    assert versions["age_65_plus"]["body"] == NEW_SMS


def test_the_same_channel_and_purpose_keep_everything(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    saved, steps = _written(db_session, admin, portfolio, ai_workflow)
    step = _first_message(steps)
    step["title"] = f"{step['title']} (renamed)"
    step["config"]["channel"] = "Email + SMS"  # a client that sends the label instead of no channel: no change

    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()
    assert _texts(response.json()) == _texts(saved)


def test_after_the_switch_a_senior_gets_the_new_sms_not_an_email_body(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    saved, steps = _written(db_session, admin, portfolio, ai_workflow)
    step = _change(_first_message(steps), channel="SMS", template_id="", body=NEW_SMS)
    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()

    stored = next(s for s in response.json()["steps"] if s["id"] == step["id"])
    calls, _, _ = _send(db_session, stored, account=_account(date_of_birth=SENIOR_BORN))  # aged 76
    assert calls[0]["channel"] == "SMS"
    assert calls[0]["body"].startswith("Maple Energy: a new note about your account.")
    assert sent_text(calls[0]) == "main"
