"""Message steps in the customer's language: other-language versions are stored, kept, dropped and sent.

config.translations = {"FR": {"subject", "body", "sms_body"}, ...} keyed by the account's language code.
The worker sends the version for the customer's language, else the step's main text.
"""

from app.modules.payflow.services.strategy_service import _carry_translations, _normalize_step
from tests.test_workflow_message_text import (
    BODY,
    SMS_BODY,
    SUBJECT,
    _account,
    _first_message,
    _get,
    _send,
    _update,
)
from tests.test_workflow_regenerate import admin, ai_workflow, portfolio  # noqa: F401 (fixtures)

FRENCH = {
    "subject": "Votre compte {{account_reference}}",
    "body": "Bonjour {{first_name}}, {{outstanding_balance}} reste à payer : {{payment_link}}\n{{brand_name}}",
    "sms_body": "{{brand_name}} : payez ici {{payment_link}} Répondez STOP pour vous désabonner.",
}


def _contact_step(translations=None):
    config = {"purpose": "Payment Reminder", "subject": SUBJECT, "body": BODY, "sms_body": SMS_BODY}
    if translations is not None:
        config["translations"] = translations
    return {"kind": "Communication", "config": config}


# --------------------------------------------------------------------------- worker


def test_worker_sends_the_customers_language(db_session):
    calls, _, _ = _send(db_session, _contact_step({"FR": FRENCH}), account=_account(language="FR"))
    assert calls[0]["channel"] == "Email"
    assert calls[0]["subject"] == "Votre compte ACC-991"
    assert calls[0]["body"].startswith("Bonjour Ali, $120.00 reste à payer")


def test_worker_sends_the_french_sms(db_session):
    account = _account(language="fr", email=None, email_consent=False)  # code in any case
    calls, _, _ = _send(db_session, _contact_step({"FR": FRENCH}), account=account)
    assert calls[0]["channel"] == "SMS"
    assert calls[0]["body"] == "Maple Energy : payez ici https://pay.example/abc Répondez STOP pour vous désabonner."


def test_worker_sends_the_main_text_without_a_version_for_the_language(db_session):
    for language in ("EN", None, "ES"):
        calls, _, _ = _send(db_session, _contact_step({"FR": FRENCH}), account=_account(language=language))
        assert calls[0]["subject"] == "About account ACC-991", language


def test_a_version_without_sms_text_keeps_the_main_sms(db_session):
    email_only = {"subject": FRENCH["subject"], "body": FRENCH["body"]}
    account = _account(language="FR", email=None, email_consent=False)
    calls, _, _ = _send(db_session, _contact_step({"FR": email_only}), account=account)
    assert calls[0]["body"].startswith("Maple Energy: please pay")


def test_fixed_channel_steps_use_the_version_too(db_session):
    step = {
        "kind": "Communication",
        "config": {"channel": "Email", "purpose": "Payment Reminder", "template_id": "em-reminder",
                   "subject": SUBJECT, "body": BODY, "translations": {"FR": FRENCH}},
    }
    calls, _, _ = _send(db_session, step, account=_account(language="FR"))
    assert calls[0]["subject"] == "Votre compte ACC-991"


# --------------------------------------------------------------------------- saving


def test_versions_are_cleaned_when_saved():
    raw = {
        "id": "s2",
        **_contact_step({"fr": {"body": " Bonjour ", "subject": ""}, "ES": {"subject": "only a subject"}, "": FRENCH}),
    }
    assert _normalize_step(raw)["config"]["translations"] == {"FR": {"body": "Bonjour"}}  # code upper-cased
    wait = {"id": "s3", "kind": "Wait", "config": {"amount": 2, "unit": "Days", "direction": "After",
                                                  "reference_event": "Previous Action", "translations": {"FR": FRENCH}}}
    assert "translations" not in _normalize_step(wait)["config"]  # only message steps have text


def test_versions_stay_while_the_main_text_is_unchanged():
    before = [{"id": "s2", **_contact_step({"FR": FRENCH})}]
    resent = [{"id": "s2", **_contact_step()}]  # a client that does not send the versions
    assert _carry_translations(before, resent)[0]["config"]["translations"] == {"FR": FRENCH}

    new_body = "Hi {{first_name}}, new text {{payment_link}} {{brand_name}}"
    edited = [{"id": "s2", **_contact_step({"FR": FRENCH})}]  # the old versions sent back as they were
    edited[0]["config"]["body"] = new_body
    assert "translations" not in _carry_translations(before, edited)[0]["config"]  # outdated: dropped

    rewritten = [{"id": "s2", **_contact_step({"FR": {**FRENCH, "body": "Bonjour, nouveau texte"}})}]
    rewritten[0]["config"]["body"] = new_body  # new main text together with new versions
    assert _carry_translations(before, rewritten)[0]["config"]["translations"]["FR"]["body"] == "Bonjour, nouveau texte"


def test_api_returns_and_keeps_the_versions(client, admin_token, ai_workflow):  # noqa: F811
    steps = _get(client, admin_token, ai_workflow["id"])["steps"]
    _first_message(steps)["config"].update({"subject": SUBJECT, "body": BODY, "translations": {"FR": FRENCH}})
    response = _update(client, admin_token, ai_workflow["id"], steps)
    assert response.status_code == 200, response.json()
    saved = _first_message(response.json()["steps"])["config"]["translations"]["FR"]
    assert (saved["subject"], saved["body"], saved["sms_body"]) == (FRENCH["subject"], FRENCH["body"], FRENCH["sms_body"])

    # The frontend sends the steps back as it got them: nothing changes, the versions stay.
    steps = _get(client, admin_token, ai_workflow["id"])["steps"]
    _first_message(steps)["config"]["note"] = None
    again = _update(client, admin_token, ai_workflow["id"], steps).json()
    assert _first_message(again["steps"])["config"]["translations"]["FR"]["body"] == FRENCH["body"]


def test_api_drops_the_versions_when_the_main_text_is_edited(client, admin_token, ai_workflow):  # noqa: F811
    steps = _get(client, admin_token, ai_workflow["id"])["steps"]
    _first_message(steps)["config"].update({"subject": SUBJECT, "body": BODY, "translations": {"FR": FRENCH}})
    _update(client, admin_token, ai_workflow["id"], steps)

    steps = _get(client, admin_token, ai_workflow["id"])["steps"]
    _first_message(steps)["config"]["body"] = BODY + " Thank you."
    edited = _update(client, admin_token, ai_workflow["id"], steps).json()
    assert _first_message(edited["steps"])["config"].get("translations") is None
