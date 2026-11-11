"""The AI writes each message in the other languages of the workflow's segment (config.translations).

Main text first (client's language, fully checked), then one AI call per other language translates it;
each version passes message_validator.check_translation. A language that fails is left out, never blocking.
"""

import json
from unittest.mock import patch

import pytest

from app.core.exceptions import ServiceUnavailableError
from app.infrastructure.database.models import PayflowAccountModel
from app.modules.payflow.services.workflow_suggestion import message_writer
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.context import load_segment_languages
from app.modules.payflow.services.workflow_suggestion.languages import (
    extra_languages,
    language_code,
    language_name,
)
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from tests.test_workflow_message_api import fake_message_ai
from tests.test_workflow_regenerate import REMARKS, admin, ai_workflow  # noqa: F401 (fixtures)
from tests.test_workflow_suggestion_api import fake_llm, portfolio  # noqa: F401 (fixtures)

TRANSLATE = "Messages to translate (one per step_id):\n"


def _french(text: str) -> str:
    return "FR - " + text.replace("Reply STOP to opt out.", "Répondez STOP pour vous désabonner.")


def fake_writer(translate=True):
    """Writes messages like fake_message_ai; translates them when asked (translate=False: always invalid)."""
    write, prompts = fake_message_ai()
    translations = []

    def call(messages, schema):
        prompt = messages[1]["content"]
        if TRANSLATE not in prompt:
            return write(messages, schema)
        translations.append(messages)
        originals = [json.loads(line) for line in prompt.split(TRANSLATE, 1)[1].split("\n\n", 1)[0].splitlines()]
        return {
            "messages": [
                {"step_id": o["step_id"], **({key: _french(o[key]) for key in ("subject", "body", "sms_body") if key in o}
                                             if translate else {"body": "x"})}
                for o in originals
            ]
        }

    return call, translations


def _make_french(db_session, portfolio_id, count=5):  # noqa: F811
    accounts = (
        db_session.query(PayflowAccountModel)
        .filter(PayflowAccountModel.portfolio_id == portfolio_id)
        .order_by(PayflowAccountModel.id)
        .all()
    )
    for account in accounts[1 : 1 + count]:  # the first one is Resolved (not eligible)
        account.language = "FR"
    db_session.commit()


def _suggest(db_session, admin, portfolio, translate=True):  # noqa: F811
    design, _ = fake_llm()
    writer, translations = fake_writer(translate)
    with patch.object(suggestion_service, "call_llm", side_effect=design), \
            patch.object(message_writer, "call_llm", side_effect=writer):
        result = WorkflowSuggestionService(db_session).suggest_workflows(
            admin, portfolio["client_id"], portfolio["portfolio_id"]
        )
    return result["workflows"][0], translations


def _messages(workflow):
    return [step["config"] for step in workflow["steps"] if step["kind"] == "Communication"]


def _context(workflow):
    return {item["label"]: item["value"] for item in workflow["ai_context"]}


# --------------------------------------------------------------------------- languages


def test_language_codes_and_names():
    assert [language_code(v) for v in ("FR", "fr", "French", " es ", None, "", "unknown")] == [
        "FR", "FR", "FR", "ES", None, None, None
    ]
    assert language_name("FR") == "French" and language_name("XQ") == "the language with code XQ"
    # The client's own language is the main text; "no language" is skipped; any other code is kept.
    assert extra_languages("English", ["UNKNOWN", "FR", "EN", "XQ", "FR"]) == ["FR", "XQ"]
    assert extra_languages(None, ["EN", "FR"]) == ["FR"]  # no client language: English
    assert extra_languages("French", ["EN", "FR"]) == ["EN"]


def test_segment_languages_come_from_the_accounts_it_is_for(db_session, admin, portfolio):  # noqa: F811
    workflow = {"client_id": portfolio["client_id"], "portfolio_id": portfolio["portfolio_id"],
                "segment": {"situation": "Active"}}
    assert load_segment_languages(db_session, workflow) == []  # the fixture's accounts have no language
    _make_french(db_session, portfolio["portfolio_id"])
    assert load_segment_languages(db_session, workflow) == ["FR"]
    assert load_segment_languages(db_session, {**workflow, "segment": {"situation": "Payment Plan"}}) == []


# --------------------------------------------------------------------------- writing


def test_suggest_writes_every_message_in_the_segments_other_language(db_session, admin, portfolio):  # noqa: F811
    _make_french(db_session, portfolio["portfolio_id"])
    workflow, translations = _suggest(db_session, admin, portfolio)

    assert len(translations) == 1  # one AI call for French, for all the workflow's messages
    assert "into French" in translations[0][0]["content"]
    for config in _messages(workflow):
        french = config["translations"]["FR"]
        assert french["body"] == _french(config["body"]) and french["sms_body"] == _french(config["sms_body"])
    assert _context(workflow)["Languages"] == "English (main), French"


def test_no_other_language_no_extra_call(db_session, admin, portfolio):  # noqa: F811
    workflow, translations = _suggest(db_session, admin, portfolio)
    assert translations == [] and "Languages" not in _context(workflow)
    assert all(not config.get("translations") for config in _messages(workflow))


def test_a_language_that_cannot_be_translated_is_left_out(db_session, admin, portfolio):  # noqa: F811
    _make_french(db_session, portfolio["portfolio_id"])
    workflow, translations = _suggest(db_session, admin, portfolio, translate=False)

    assert len(translations) == 1 + message_writer.MAX_FIX_ATTEMPTS  # tried, then given up
    assert all(not config.get("translations") for config in _messages(workflow))  # main text is sent
    assert _context(workflow)["Languages"].startswith("English (main); not written: French - these customers")
    assert all(config.get("body") for config in _messages(workflow))  # the save was not blocked


def test_translate_messages_sends_problems_back(db_session):
    written = {"s2": {"subject": "About {{account_reference}}", "body": "Hi {{first_name}}, pay {{payment_link}} "
                      "{{brand_name}}", "sms_body": "{{brand_name}}: pay {{payment_link}} Reply STOP to opt out.",
                      "tone": "Friendly"}}
    workflow = {"steps": [{"id": "s2", "kind": "Communication", "config": {"purpose": "Payment Reminder"}}]}
    replies = [
        {"messages": [{"step_id": "s2", "subject": _french(written["s2"]["subject"]),
                       "body": "Bonjour {{prenom}}, payez {{payment_link}} {{brand_name}}",  # renamed variable
                       "sms_body": _french(written["s2"]["sms_body"])}]},
        {"messages": [{"step_id": "s2", **{key: _french(written["s2"][key]) for key in ("subject", "body", "sms_body")}}]},
    ]
    with patch.object(message_writer, "call_llm", side_effect=replies) as call:
        versions = message_writer.translate_messages({"name": "Maple"}, workflow, written, "FR")
    assert versions["s2"]["body"] == _french(written["s2"]["body"]) and call.call_count == 2
    assert "must use exactly the variables of the original" in call.call_args_list[1].args[0][1]["content"]

    with patch.object(message_writer, "call_llm", return_value={"messages": []}):
        with pytest.raises(ServiceUnavailableError, match="could not translate the messages into French"):
            message_writer.translate_messages({"name": "Maple"}, workflow, written, "FR")


def test_regenerate_writes_the_other_languages_too(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    _make_french(db_session, portfolio["portfolio_id"])
    design, _ = fake_llm()
    writer, translations = fake_writer()
    with patch.object(suggestion_service, "call_llm", side_effect=design), \
            patch.object(message_writer, "call_llm", side_effect=writer):
        new = WorkflowSuggestionService(db_session).regenerate_workflow(admin, ai_workflow["id"], REMARKS)
    assert len(translations) == 1
    assert all(config["translations"]["FR"]["body"] == _french(config["body"]) for config in _messages(new))
    assert _context(new)["Languages"] == "English (main), French"


def test_write_messages_api_writes_and_rewrites_the_versions(client, admin_token, db_session, portfolio, ai_workflow):  # noqa: F811
    _make_french(db_session, portfolio["portfolio_id"])

    def post(writer, body=None):
        with patch.object(message_writer, "call_llm", side_effect=writer):
            return client.post(f"/payflow/workflows/{ai_workflow['id']}/content", json=body,
                               headers={"Authorization": f"Bearer {admin_token}"})

    writer, _ = fake_writer()
    response = post(writer)
    assert response.status_code == 200, response.json()
    steps = [s for s in response.json()["steps"] if s["kind"] == "Communication"]
    assert all(s["config"]["translations"]["FR"]["body"] == _french(s["config"]["body"]) for s in steps)

    # Rewriting one message whose French fails: that message loses its outdated French, the others keep theirs.
    writer, _ = fake_writer(translate=False)
    response = post(writer, {"step_id": steps[0]["id"], "remarks": "shorter"})
    after = {s["id"]: s["config"] for s in response.json()["steps"] if s["kind"] == "Communication"}
    assert not after[steps[0]["id"]].get("translations")
    assert all(after[s["id"]]["translations"]["FR"] for s in steps[1:])


# --------------------------------------------------------------------------- re-translate on edit


def _with_french_messages(client, admin_token, db_session, portfolio, ai_workflow):  # noqa: F811
    """The workflow with its messages written in English and French (as Suggest would)."""
    _make_french(db_session, portfolio["portfolio_id"])
    writer, _ = fake_writer()
    with patch.object(message_writer, "call_llm", side_effect=writer):
        response = client.post(f"/payflow/workflows/{ai_workflow['id']}/content",
                               headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200, response.json()
    return response.json()["steps"]


def _update(client, admin_token, workflow_id, payload, translate=True):
    writer, translations = fake_writer(translate)
    with patch.object(message_writer, "call_llm", side_effect=writer):
        response = client.post(f"/payflow/workflows/{workflow_id}/update", json=payload,
                               headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200, response.json()
    return response.json(), translations


def _by_id(steps):
    return {step["id"]: step["config"] for step in steps if step["kind"] == "Communication"}


def test_editing_a_message_translates_it_again_in_the_same_request(client, admin_token, db_session, portfolio, ai_workflow):  # noqa: F811,E501
    steps = _with_french_messages(client, admin_token, db_session, portfolio, ai_workflow)
    edited_id, other_id = list(_by_id(steps))[:2]
    before_other = _by_id(steps)[other_id]["translations"]["FR"]
    edited_body = _by_id(steps)[edited_id]["body"] + " Thank you."
    next(s for s in steps if s["id"] == edited_id)["config"]["body"] = edited_body  # the reviewer's edit

    body, translations = _update(client, admin_token, ai_workflow["id"], {"steps": steps, "note": "warmer"})
    after = _by_id(body["steps"])
    assert after[edited_id]["body"] == edited_body  # the reviewer's text is saved as written
    assert after[edited_id]["translations"]["FR"]["body"] == _french(edited_body)  # and translated again
    assert after[other_id]["translations"]["FR"] == before_other  # untouched messages keep their French
    assert len(translations) == 1 and edited_id in translations[0][1]["content"]
    assert other_id not in translations[0][1]["content"].split(TRANSLATE, 1)[1]  # only the edited message
    assert body["origin"] == "Human Modified"  # still the reviewer's change
    assert body["versions"][0]["note"].startswith("Edited messages (") and "translated by AI into: French" in (
        body["versions"][0]["note"]
    )


def test_a_save_without_text_changes_makes_no_ai_call(client, admin_token, db_session, portfolio, ai_workflow):  # noqa: F811
    steps = _with_french_messages(client, admin_token, db_session, portfolio, ai_workflow)
    body, translations = _update(client, admin_token, ai_workflow["id"], {"name": "Renamed", "steps": steps})
    assert translations == []
    assert all(config["translations"]["FR"] for config in _by_id(body["steps"]).values())


def test_a_failed_translation_keeps_the_edit(client, admin_token, db_session, portfolio, ai_workflow):  # noqa: F811
    steps = _with_french_messages(client, admin_token, db_session, portfolio, ai_workflow)
    edited_id = list(_by_id(steps))[0]
    edited_body = _by_id(steps)[edited_id]["body"] + " Thank you."
    next(s for s in steps if s["id"] == edited_id)["config"]["body"] = edited_body

    body, _ = _update(client, admin_token, ai_workflow["id"], {"steps": steps}, translate=False)
    after = _by_id(body["steps"])[edited_id]
    assert after["body"] == edited_body and not after.get("translations")  # French customers get the edit
    assert "not translated into French - these customers get the edited text" in body["versions"][0]["note"]


def test_versions_sent_with_the_edit_are_kept(client, admin_token, db_session, portfolio, ai_workflow):  # noqa: F811
    steps = _with_french_messages(client, admin_token, db_session, portfolio, ai_workflow)
    edited_id = list(_by_id(steps))[0]
    config = next(s for s in steps if s["id"] == edited_id)["config"]
    config["body"] += " Thank you."
    config["translations"]["FR"]["body"] = _french(config["body"])  # the reviewer also wrote the French

    body, translations = _update(client, admin_token, ai_workflow["id"], {"steps": steps})
    assert translations == []
    assert _by_id(body["steps"])[edited_id]["translations"]["FR"]["body"] == _french(config["body"])


# --------------------------------------------------------------------------- "Languages" line kept up to date


def test_write_messages_refreshes_the_languages_and_messages_lines(client, admin_token, db_session, portfolio, ai_workflow):  # noqa: F811,E501
    assert "Messages" in _context(ai_workflow)  # Suggest could not write the text (the AI is off in tests)
    _make_french(db_session, portfolio["portfolio_id"])
    writer, _ = fake_writer()
    with patch.object(message_writer, "call_llm", side_effect=writer):
        body = client.post(f"/payflow/workflows/{ai_workflow['id']}/content",
                           headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert _context(body)["Languages"] == "English (main), French"
    assert "Messages" not in _context(body)  # every message has text now

    first = next(s["id"] for s in body["steps"] if s["kind"] == "Communication")
    writer, _ = fake_writer(translate=False)
    with patch.object(message_writer, "call_llm", side_effect=writer):
        body = client.post(f"/payflow/workflows/{ai_workflow['id']}/content", json={"step_id": first},
                           headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert _context(body)["Languages"].startswith("English (main); not written: French")  # one message lacks it


# --------------------------------------------------------------------------- Regenerate keeps reviewer text


def test_regenerate_keeps_the_reviewers_text_as_written(client, admin_token, db_session, ai_workflow):  # noqa: F811,E501
    writer, _ = fake_message_ai()
    with patch.object(message_writer, "call_llm", side_effect=writer):  # the AI writes the text
        steps = client.post(f"/payflow/workflows/{ai_workflow['id']}/content",
                            headers={"Authorization": f"Bearer {admin_token}"}).json()["steps"]
    edited_id, ai_id = [s["id"] for s in steps if s["kind"] == "Communication"][:2]
    reviewer_body = "Hi {{first_name}}, a kind word from {{brand_name}}: please pay {{payment_link}} " * 3
    next(s for s in steps if s["id"] == edited_id)["config"]["body"] = reviewer_body
    client.post(f"/payflow/workflows/{ai_workflow['id']}/update", json={"steps": steps},
                headers={"Authorization": f"Bearer {admin_token}"})

    design, _ = fake_llm()
    writer, prompts = fake_message_ai()
    with patch.object(suggestion_service, "call_llm", side_effect=design), \
            patch.object(message_writer, "call_llm", side_effect=writer):
        new = WorkflowSuggestionService(db_session).regenerate_workflow(admin_user(db_session), ai_workflow["id"], REMARKS)

    asked = {b["step_id"]: b for b in map(json.loads, prompts[0].split("Messages to write (one per step_id):\n", 1)[1]
                                         .split("\n\n", 1)[0].splitlines())}
    assert edited_id not in asked  # the reviewer's message is not rewritten...
    assert "Other messages in this workflow" in prompts[0] and "a kind word from" in prompts[0]  # ...only shown
    assert "current_text" not in asked[ai_id]  # the AI's own text is written fresh
    kept = next(s for s in new["steps"] if s["id"] == edited_id)
    assert kept["config"]["body"] == reviewer_body.strip()  # kept as written
    assert _context(new)["Reviewer text kept"] == kept["title"]


def admin_user(db_session):
    from app.infrastructure.database.models import UserModel

    return db_session.query(UserModel).filter(UserModel.email == "admin@payflow.ai").one()


# --------------------------------------------------------------------------- templates in other languages


def test_the_stored_template_translations_are_complete_and_pass_the_checks():
    """The data file the worker reads (written by template_translations.py with the real AI)."""
    from app.modules.payflow.services import workflow_contact_runtime as contact_runtime
    from app.modules.payflow.services.strategy_service import MESSAGE_TEMPLATES
    from app.modules.payflow.services.workflow_suggestion.message_validator import check_translation

    stored = json.loads(contact_runtime.TEMPLATE_TRANSLATIONS_PATH.read_text(encoding="utf-8"))
    templates = {t["id"]: t for t in MESSAGE_TEMPLATES}
    assert "FR" in stored
    for code, versions in stored.items():
        assert set(versions) == set(templates), code  # every template translated
        for template_id, version in versions.items():
            original = {key: templates[template_id][key] for key in ("subject", "body") if templates[template_id].get(key)}
            assert check_translation(template_id, version, original, templates[template_id]["channel"]) == [], (
                code, template_id)


def test_worker_sends_the_template_in_the_customers_language(db_session):
    from app.modules.payflow.services import workflow_contact_runtime as contact_runtime
    from tests.test_workflow_message_text import _account, _send

    french = {"FR": {"em-reminder": {"subject": "Rappel {{account_reference}}", "body": "Bonjour {{first_name}}"},
                     "sms-reminder": {"body": "{{brand_name}} : payez. Répondez STOP"}}}
    no_text = {"kind": "Communication", "config": {"purpose": "Payment Reminder"}}  # a step without its own text
    fixed = {"kind": "Communication", "config": {"channel": "Email", "purpose": "Payment Reminder",
                                                   "template_id": "em-reminder"}}
    with patch.object(contact_runtime, "template_translations", return_value=french):
        email, _, _ = _send(db_session, no_text, account=_account(language="FR"))
        sms, _, _ = _send(db_session, no_text, account=_account(language="FR", email=None, email_consent=False))
        fixed_fr, _, _ = _send(db_session, fixed, account=_account(language="FR"))
        english, _, _ = _send(db_session, no_text, account=_account(language="EN"))
    assert (email[0]["subject"], email[0]["body"]) == ("Rappel ACC-991", "Bonjour Ali")
    assert sms[0]["body"] == "Maple Energy : payez. Répondez STOP"
    assert fixed_fr[0]["subject"] == "Rappel ACC-991"
    assert english[0]["subject"] != "Rappel ACC-991"  # no translation for English: the template as it is


def test_translate_templates_covers_every_template_in_each_asked_language():
    from app.modules.payflow.services.strategy_service import MESSAGE_TEMPLATES
    from app.modules.payflow.services.workflow_suggestion import template_translations

    writer, translations = fake_writer()
    with patch.object(message_writer, "call_llm", side_effect=writer):
        result = template_translations.translate_templates(["EN", "FR", "ES"])
    assert list(result) == ["FR", "ES"]  # English is the templates' own language
    assert all(set(versions) == {t["id"] for t in MESSAGE_TEMPLATES} for versions in result.values())
    assert len(translations) == 2  # one AI call per language
