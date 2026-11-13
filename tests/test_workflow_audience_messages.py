"""Messages written for each audience: AI call 1 plans them (briefs), call 2 writes the main text and each audience's
version from the plans; both are saved on the step (config.brief, config.versions) and translated. Plus the
writer-side rules: the email link is a "Pay Now" button, no emojis, never a word about the customer's group."""

import json
from datetime import date
from unittest.mock import patch

from app.infrastructure.database.models import PayflowAccountModel
from app.modules.payflow.services.workflow_suggestion import message_brief, message_writer
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.message_validator import check_message, check_translation
from app.modules.payflow.services.workflow_suggestion.message_writer import write_messages
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from tests.test_workflow_message_api import fake_message_ai, messages
from tests.test_workflow_message_brief import _message_lines, good
from tests.test_workflow_message_translation import fake_writer
from tests.test_workflow_message_validator import EMAIL, SMS, brief
from tests.test_workflow_message_writer import CLIENT, SEGMENT, workflow
from tests.test_workflow_regenerate import REMARKS, admin, ai_workflow, context  # noqa: F401 (fixtures)
from tests.test_workflow_suggestion_api import _account, fake_llm, portfolio  # noqa: F401 (fixtures)

SENIORS = "These texts are for: Age 65 and over - 25 accounts (their own version)"


def fake_planner():
    """AI call 1: a valid brief for every asked message and audience (first allowed tone, first offered option)."""
    calls = []

    def call(messages_, schema):
        calls.append(messages_)
        user = messages_[1]["content"]
        options = schema["properties"]["briefs"]["items"]["properties"]["lead_option"]["enum"]
        allowed = {line["step_id"]: line["allowed_tones"] for line in map(json.loads, _message_lines(user))}
        asked = user.split("Write one brief for each message and audience: ")[1].split("\n")[0]
        briefs = []
        for part in asked.split("; "):
            step_id, audiences = part.split(" → ")
            tone = allowed[step_id][0]
            for audience in audiences.split(", "):
                lead = "none" if tone == "Neutral" else options[0]
                briefs.append({**good(step_id, audience, tone), "lead_option": lead,
                               "approach": f"Approach for {audience}"})
        return {"briefs": briefs}

    return call, calls


def add_seniors(db_session, portfolio, count=25, *, start=500, born=date(1956, 1, 1), **values):  # noqa: F811
    """`count` more accounts in the portfolio, like the fixture's, born `born` (default: aged 70)."""
    for i in range(count):
        db_session.add(_account(portfolio["client_id"], portfolio["portfolio_id"], start + i,
                                outstanding_balance=300.0, days_past_due=10, collection_status="Active",
                                date_of_birth=born, **values))
    db_session.commit()


def write_with(db_session, admin, workflow_id, writer=None, planner=None, step_id=None):  # noqa: F811
    writer = writer or fake_message_ai()[0]
    planner = planner or fake_planner()[0]
    with patch.object(message_writer, "call_llm", side_effect=writer), \
            patch.object(message_brief, "call_llm", side_effect=planner):
        return WorkflowSuggestionService(db_session).write_workflow_messages(admin, workflow_id, step_id=step_id)


# --------------------------------------------------------------------------- the writer's rules


def test_in_an_email_the_payment_link_is_alone_on_its_line():
    inline = {**EMAIL, "body": EMAIL["body"].replace("secure page:\n\n{{payment_link}}", "secure page {{payment_link}} now.")}
    assert check_message(inline, brief(), []) == [
        "s2: put {{payment_link}} alone on its own line - in the email it becomes the Pay Now button, so text on "
        "the same line would be left broken"
    ]
    assert check_message(EMAIL, brief(), []) == []
    assert check_message(SMS, brief("SMS"), []) == []  # an SMS keeps the link in its text
    link = {**EMAIL, "body": EMAIL["body"].replace("on your secure page:", "with the link below:")}
    assert check_message(link, brief(), []) == [
        "s2: in an email the customer sees a Pay Now button - call it the button, not the link"
    ]


def test_no_emojis_and_never_a_word_about_the_customers_group():
    smiley = {**EMAIL, "subject": "Your balance 🙂"}
    assert check_message(smiley, brief(), []) == ["s2: has an emoji or a symbol - use words only"]
    senior = {**EMAIL, "body": EMAIL["body"].replace("Hi {{first_name}},", "Hi {{first_name}}, as a senior,")}
    assert check_message(senior, brief(), []) == [
        "s2: 'senior' is about the customer's age or group - write for them, never about them"
    ]
    assert check_message(senior, {**brief(), "english": False}, []) == []  # word lists are English only


def test_a_calm_subject_is_a_reminder_not_a_demand():
    demand = {**EMAIL, "subject": "Action needed: your balance"}
    assert check_message(demand, brief(tone="Clear"), []) == [
        "s2: 'Action needed' in the subject sounds like pressure - a Clear subject is a calm reminder"
    ]
    assert check_message(demand, brief(tone="Firm"), []) == []  # a Firm message may ask for action


def test_a_translation_keeps_the_link_alone_when_the_original_does():
    source = {"subject": "About your balance", "body": "Hi {{first_name}},\nPay with the button below.\n{{payment_link}}"}
    inline = {"subject": "Votre solde", "body": "Bonjour {{first_name}},\nPayez ici {{payment_link}}"}
    assert "s2 (body): keep {{payment_link}} alone on its own line, as in the original" in check_translation(
        "s2", inline, source, "Email"
    )
    alone = {"subject": "Votre solde", "body": "Bonjour {{first_name}},\nPayez avec le bouton ci-dessous.\n{{payment_link}}"}
    assert check_translation("s2", alone, source, "Email") == []


def test_a_translated_subject_may_leave_out_a_variable_but_never_add_one():
    source = {"subject": "A reminder from {{brand_name}}", "body": "Hi {{first_name}},\nPay below.\n{{payment_link}}"}
    body = "Bonjour {{first_name}},\nPayez ci-dessous.\n{{payment_link}}"
    assert check_translation("s2", {"subject": "Petit rappel", "body": body}, source, "Email") == []
    added = check_translation("s2", {"subject": "Rappel de {{brand_name}} pour {{first_name}}", "body": body},
                              source, "Email")
    assert added and "s2 (subject): must use exactly the variables of the original" in added[0]
    no_name = check_translation("s2", {"subject": "Petit rappel", "body": "Bonjour,\nPayez ci-dessous.\n{{payment_link}}"},
                                source, "Email")
    assert no_name and "s2 (body): must use exactly the variables of the original" in no_name[0]  # bodies keep all


def test_the_writer_follows_the_plan_and_knows_its_audience():
    call, prompts = fake_message_ai()
    plan = good("s2", "age_65_plus", "Clear", approach="Respectful and warm, one step at a time.",
                lead_option="set up a payment plan", length="medium")
    audience = {"key": "age_65_plus", "label": "Age 65 and over", "accounts": 32,
                "profile": {"accounts": 32, "age": {"65 and over": 100}, "regions": {"Ontario": 100}}}
    with patch.object(message_writer, "call_llm", side_effect=call):
        written = write_messages(CLIENT, workflow(), SEGMENT, step_ids=["s2"], plans={"s2": plan}, audience=audience)
    line = json.loads(prompts[0].split("Messages to write (one per step_id):\n")[1].split("\n")[0])
    # The rules chose Firm for s2; the plan chose one step softer: Clear (their reason is then left out).
    assert line["tone"] == "Clear" and "why_this_tone" not in line and written["s2"]["tone"] == "Clear"
    assert line["approach"] == "Respectful and warm, one step at a time." and line["length_words"] == "80-130"
    assert line["lead_option"] == "set up a payment plan" and line["call_to_action"] and line["why"]
    assert "These texts are for: Age 65 and over - 32 accounts (their own version)." in prompts[0]
    assert '"regions": {"Ontario": 100}' in prompts[0] and "never about them" in prompts[0]


# --------------------------------------------------------------------------- saved on the steps


def test_write_messages_saves_the_main_text_and_an_audience_version(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio)  # 29 without an age + 25 aged 65+ → a version for 65+
    writer, prompts = fake_message_ai()
    planner, plans = fake_planner()
    saved = write_with(db_session, admin, ai_workflow["id"], writer, planner)

    assert len(plans) == 1  # one plan for every message and audience
    assert len(prompts) == 2 and SENIORS in prompts[1] and "the main text - 29 accounts" in prompts[0]
    for step_id, config in messages(saved).items():
        assert config["body"] and config["brief"]["approach"] == "Approach for main", step_id
        version = config["versions"]["age_65_plus"]
        assert version["label"] == "Age 65 and over" and version["body"] and version["tone"]
        assert version["brief"]["approach"] == "Approach for age_65_plus"
    assert context(saved)["Audiences"] == (
        "Own version for Age 65 and over (25 accounts); main text for the other 29 accounts"
    )
    # The API returns them (they are part of the step's config).
    assert saved["steps"] == WorkflowSuggestionService(db_session).strategies.get_strategy(admin, saved["id"])["steps"]


def test_no_plans_means_one_text_for_everyone_as_before(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio)
    writer, prompts = fake_message_ai()
    with patch.object(message_writer, "call_llm", side_effect=writer):  # the planner stays off (conftest)
        saved = WorkflowSuggestionService(db_session).write_workflow_messages(admin, ai_workflow["id"])
    assert len(prompts) == 1 and "These texts are for" not in prompts[0]
    assert all(config["body"] and "versions" not in config and "brief" not in config
               for config in messages(saved).values())
    assert context(saved)["Audiences"].startswith("One text for everyone - the AI could not plan")


def test_a_version_that_fails_is_left_out_and_the_main_text_is_kept(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio)
    write, _ = fake_message_ai()
    broken, _ = fake_message_ai(fail=True)

    def writer(messages_, schema):
        return (broken if SENIORS in messages_[1]["content"] else write)(messages_, schema)

    saved = write_with(db_session, admin, ai_workflow["id"], writer)
    assert all(config["body"] and "versions" not in config for config in messages(saved).values())
    assert context(saved)["Audiences"] == (
        "main text for the other 29 accounts; not written for Age 65 and over - they get the main text"
    )


def test_audience_versions_are_translated_too(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio, language="FR")
    writer, translations = fake_writer()
    saved = write_with(db_session, admin, ai_workflow["id"], writer)
    assert len(translations) == 2  # French: one call for the main text, one for the 65+ version
    for config in messages(saved).values():
        assert config["translations"]["FR"]["body"].startswith("FR - ")
        assert config["versions"]["age_65_plus"]["translations"]["FR"]["body"].startswith("FR - ")


def test_suggest_and_regenerate_write_audience_versions(db_session, admin, portfolio):  # noqa: F811
    add_seniors(db_session, portfolio)
    design, _ = fake_llm()
    writer, _ = fake_message_ai()
    planner, _ = fake_planner()
    with patch.object(suggestion_service, "call_llm", side_effect=design), \
            patch.object(message_writer, "call_llm", side_effect=writer), \
            patch.object(message_brief, "call_llm", side_effect=planner):
        service = WorkflowSuggestionService(db_session)
        suggested = service.suggest_workflows(admin, portfolio["client_id"], portfolio["portfolio_id"])["workflows"][0]
        regenerated = service.regenerate_workflow(admin, suggested["id"], REMARKS)
    for workflow_ in (suggested, regenerated):
        assert all("age_65_plus" in config["versions"] for config in messages(workflow_).values())
        assert context(workflow_)["Audiences"].startswith("Own version for Age 65 and over (25 accounts)")


# --------------------------------------------------------------------------- reviewer edits


def _post_update(client, token, workflow_id, steps):
    return client.post(f"/payflow/workflows/{workflow_id}/update", json={"steps": steps},
                       headers={"Authorization": f"Bearer {token}"})


def _texts(workflow_):
    """Each message's main body, brief and versions - empty fields left out (the API lists every field)."""
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if item not in (None, {}, [])}
        return value

    return {step_id: clean({key: config.get(key) for key in ("body", "brief", "versions")})
            for step_id, config in messages(workflow_).items()}


def test_a_client_that_does_not_send_versions_keeps_them(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811
    add_seniors(db_session, portfolio)
    saved = write_with(db_session, admin, ai_workflow["id"])
    steps = json.loads(json.dumps(saved["steps"]))
    for step in steps:
        (step.get("config") or {}).pop("versions", None)  # an older frontend
        (step.get("config") or {}).pop("brief", None)
    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()
    assert _texts(response.json()) == _texts(saved)
    assert all(texts["versions"]["age_65_plus"]["body"] for texts in _texts(response.json()).values())


def test_a_reviewer_editing_the_main_text_keeps_the_versions(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio)
    saved = write_with(db_session, admin, ai_workflow["id"])
    before = _texts(saved)
    steps = json.loads(json.dumps(saved["steps"]))
    edited = next(step for step in steps if step["kind"] == "Communication")
    edited["config"]["body"] = edited["config"]["body"] + "\nThank you."
    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()
    after = _texts(response.json())
    assert after[edited["id"]]["body"].endswith("\nThank you.")
    # Each text is edited on its own: the versions and the AI's plan stay as they were.
    assert after[edited["id"]]["versions"] == before[edited["id"]]["versions"]
    assert after[edited["id"]]["brief"] == before[edited["id"]]["brief"]


def test_one_version_is_edited_on_its_own(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio)
    saved = write_with(db_session, admin, ai_workflow["id"])
    before = _texts(saved)
    steps = json.loads(json.dumps(saved["steps"]))
    step = next(step for step in steps if step["kind"] == "Communication")
    step["config"]["versions"]["age_65_plus"]["body"] += "\nWe are here to help."
    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()
    after = _texts(response.json())
    assert after[step["id"]]["versions"]["age_65_plus"]["body"].endswith("\nWe are here to help.")
    assert after[step["id"]]["body"] == before[step["id"]]["body"]  # the main text is untouched
    assert all(after[s]["versions"] == before[s]["versions"] for s in after if s != step["id"])


def test_versions_sent_back_without_translations_keep_them(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio, language="FR")
    writer, _ = fake_writer()
    saved = write_with(db_session, admin, ai_workflow["id"], writer)
    steps = json.loads(json.dumps(saved["steps"]))
    for step in steps:
        for version in ((step.get("config") or {}).get("versions") or {}).values():
            version.pop("translations", None)  # a screen that only shows the texts
            version.pop("brief", None)
    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()
    for config in messages(response.json()).values():
        version = config["versions"]["age_65_plus"]
        assert version["translations"]["FR"]["body"].startswith("FR - ") and version["brief"]["approach"]


def test_a_reviewer_can_remove_one_version_or_all_of_them(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio)
    add_seniors(db_session, portfolio, count=25, start=600, born=date(2000, 1, 1))  # 18 - 34 too
    saved = write_with(db_session, admin, ai_workflow["id"])
    first, second = [step for step in json.loads(json.dumps(saved["steps"])) if step["kind"] == "Communication"][:2]
    assert set(first["config"]["versions"]) == {"age_65_plus", "age_18_34"}
    first["config"]["versions"].pop("age_18_34")  # left out: removed on purpose
    second["config"]["versions"] = {}  # every version removed
    steps = [first if s["id"] == first["id"] else second if s["id"] == second["id"] else s for s in saved["steps"]]
    response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()
    after = _texts(response.json())
    assert set(after[first["id"]]["versions"]) == {"age_65_plus"}
    assert "versions" not in after[second["id"]]


def test_a_reviewer_editing_a_version_gets_it_translated_again(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio, language="FR")
    writer, translations = fake_writer()
    saved = write_with(db_session, admin, ai_workflow["id"], writer)
    steps = json.loads(json.dumps(saved["steps"]))
    step = next(step for step in steps if step["kind"] == "Communication")
    version = step["config"]["versions"]["age_65_plus"]
    version["body"] = version["body"] + "\nWe are here to help."
    with patch.object(message_writer, "call_llm", side_effect=writer):
        response = _post_update(client, admin_token, saved["id"], steps)
    assert response.status_code == 200, response.json()
    redone = messages(response.json())[step["id"]]["versions"]["age_65_plus"]
    assert redone["translations"]["FR"]["body"] == "FR - " + version["body"]
    assert response.json()["versions"][0]["note"].startswith(
        f"Edited messages (Age 65 and over version of {step['id']}) translated by AI into: French"
    )
