"""The worker sends each customer the text written for their audience (config.versions - their age from their date of
birth), in their language, and records which text it sent on the communication (step_id, message_audience)."""

from datetime import date
from unittest.mock import patch

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowStrategyModel,
    PayflowWorkflowRunModel,
)
from app.modules.payflow.services.workflow_contact_runtime import audience_message_config
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.shared.enums import PayflowWorkflowRunStatus
from tests.test_workflow_message_languages import FRENCH
from tests.test_workflow_message_text import BODY, SMS_BODY, SUBJECT, _account, _send

SENIOR_TEXT = {
    "label": "Age 65 and over",
    "subject": "Your account {{account_reference}}",
    "body": "Dear {{first_name}}, {{outstanding_balance}} is open. One step at a time:\n{{payment_link}}\n{{brand_name}}",
    "sms_body": "{{brand_name}}: Dear {{first_name}}, here is your page {{payment_link}} Reply STOP to opt out.",
    "tone": "Clear",
}
SENIOR_FRENCH = {
    "subject": "Votre dossier {{account_reference}}",
    "body": "Madame, Monsieur {{first_name}}, {{outstanding_balance}} reste ouvert.\n{{payment_link}}\n{{brand_name}}",
    "sms_body": "{{brand_name}} : votre page {{payment_link}} Répondez STOP pour vous désabonner.",
}
SENIOR_BORN = date(1950, 5, 1)


def step(*, versions=True, senior_french=False, french=False, channel=None):
    config = {"purpose": "Payment Reminder", "subject": SUBJECT, "body": BODY, "sms_body": SMS_BODY}
    if channel:
        config["channel"] = channel
    if versions:
        config["versions"] = {"age_65_plus": {**SENIOR_TEXT,
                                              **({"translations": {"FR": SENIOR_FRENCH}} if senior_french else {})}}
    if french:
        config["translations"] = {"FR": FRENCH}
    return {"id": "s2", "kind": "Communication", "config": config}


def test_a_customer_of_the_audience_gets_its_version(db_session):
    calls, _, _ = _send(db_session, step(), account=_account(date_of_birth=SENIOR_BORN))
    assert calls[0]["channel"] == "Email"
    assert calls[0]["subject"] == "Your account ACC-991" and calls[0]["body"].startswith("Dear Ali, $120.00 is open.")
    assert calls[0]["message_audience"] == "age_65_plus"  # the step itself goes with the tracking (end-to-end test)


def test_everyone_else_gets_the_main_text(db_session):
    for born in (date(1990, 1, 1), None):  # another age; no date of birth
        calls, _, _ = _send(db_session, step(), account=_account(date_of_birth=born))
        assert calls[0]["subject"] == "About account ACC-991", born
        assert calls[0]["message_audience"] == "main"


def test_the_version_has_its_own_sms(db_session):
    account = _account(date_of_birth=SENIOR_BORN, email=None, email_consent=False)
    calls, _, _ = _send(db_session, step(), account=account)
    assert calls[0]["channel"] == "SMS"
    assert calls[0]["body"] == "Maple Energy: Dear Ali, here is your page https://pay.example/abc Reply STOP to opt out."


def test_the_customers_language_comes_first(db_session):
    senior_fr = _account(date_of_birth=SENIOR_BORN, language="FR")
    # The version in French.
    calls, _, _ = _send(db_session, step(senior_french=True, french=True), account=senior_fr)
    assert calls[0]["subject"] == "Votre dossier ACC-991" and calls[0]["message_audience"] == "age_65_plus"
    # Only the main text is in French: French matters more than the audience's style.
    calls, _, _ = _send(db_session, step(french=True), account=senior_fr)
    assert calls[0]["subject"] == "Votre compte ACC-991" and calls[0]["message_audience"] == "main"
    # Nothing in their language: the version, in the main language.
    calls, _, _ = _send(db_session, step(french=True), account=_account(date_of_birth=SENIOR_BORN, language="ES"))
    assert calls[0]["subject"] == "Your account ACC-991" and calls[0]["message_audience"] == "age_65_plus"


def test_fixed_channel_steps_use_the_version_too(db_session):
    calls, _, _ = _send(db_session, step(channel="Email"), account=_account(date_of_birth=SENIOR_BORN))
    assert calls[0]["subject"] == "Your account ACC-991" and calls[0]["message_audience"] == "age_65_plus"


def test_a_step_without_text_of_its_own_sends_its_template(db_session):
    template_step = {"id": "s2", "kind": "Communication",
                     "config": {"channel": "Email", "purpose": "Payment Reminder", "template_id": "em-reminder"}}
    calls, _, _ = _send(db_session, template_step, account=_account(date_of_birth=SENIOR_BORN))
    assert calls[0]["subject"] == "A friendly reminder about your balance" and calls[0]["message_audience"] is None
    assert audience_message_config({}, _account()) == ({}, None)


def test_the_sent_message_records_its_step_and_text(db_session):
    """End to end: the worker runs the step and the communication row says what was sent."""
    client = PayflowClientModel(code="aud-c", name="Audience Client", country="Canada")
    db_session.add(client)
    db_session.flush()
    accounts = {
        born: PayflowAccountModel(
            client_id=client.id, customer_name="Jane Doe", customer_first_name="Jane",
            account_reference=f"AUD-{i}", case_reference=f"CASE-AUD-{i}", original_balance=500.0,
            outstanding_balance=500.0, collection_status="Active", email=f"jane{i}@example.com",
            country_code="CA", province_state="Ontario", date_of_birth=born,
        )
        for i, born in enumerate((SENIOR_BORN, date(1990, 1, 1)))
    }
    steps = [{**step(), "id": "s1", "title": "Reminder", "next": "s9"},
             {"id": "s9", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}}]
    strategy = PayflowStrategyModel(code="aud-wf", name="Audience WF", client_id=client.id, status="Active",
                                    steps=steps, entry_node_id="s1")
    db_session.add_all([*accounts.values(), strategy])
    db_session.flush()
    runs = [PayflowWorkflowRunModel(account_id=account.id, strategy_id=strategy.id, client_id=client.id,
                                    status=PayflowWorkflowRunStatus.ACTIVE.value, current_step_id="s1",
                                    strategy_version=1) for account in accounts.values()]
    db_session.add_all(runs)
    db_session.commit()

    with patch.object(WorkflowExecutionService, "_payment_link", return_value=""):
        for run in runs:
            WorkflowExecutionService(db_session).execute_step(run.id, "s1")

    sent = {row.account_id: row for row in db_session.query(PayflowCommunicationModel).all()}
    senior, younger = sent[accounts[SENIOR_BORN].id], sent[accounts[date(1990, 1, 1)].id]
    assert (senior.step_id, senior.message_audience, senior.subject) == ("s1", "age_65_plus", "Your account AUD-0")
    assert (younger.step_id, younger.message_audience) == ("s1", "main")
