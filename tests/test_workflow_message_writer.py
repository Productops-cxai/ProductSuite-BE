"""The message writer: tone from code, text from the AI (faked here), checks with retries."""

import copy
from unittest.mock import patch

import pytest

from app.core.exceptions import ServiceUnavailableError, ValidationAppError
from app.infrastructure.database.models import PayflowClientModel
from app.modules.payflow.services.workflow_suggestion import message_writer
from app.modules.payflow.services.workflow_suggestion.context import load_payment_options
from app.modules.payflow.services.workflow_suggestion.llm import build_message_schema
from app.modules.payflow.services.workflow_suggestion.message_validator import MAX_SUBJECT_CHARS, render_sample
from app.modules.payflow.services.workflow_suggestion.message_writer import (
    _fit_subject,
    apply_messages,
    write_messages,
)
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from tests.test_workflow_message_validator import EMAIL, FINAL_SMS, SMS

CLIENT = {
    "name": "Maple Energy (Demo)",
    "client_type": "third_party",
    "language": "English",
    "currency": "CAD",
    "payment_options": ["pay in full", "set up a payment plan"],
}
SEGMENT = {
    "label": "Active · 60 – 89 days past due · Contacted, nothing paid · $1,500 – $5,000",
    "segment": {"situation": "Active", "delinquency": "60 – 89 days past due", "contact_state": "contacted_no_payment"},
    "stats": {"accounts": 40, "avg_outstanding": 2300.0, "avg_days_past_due": 71},
}
BAD_SMS = {"body": "You owe $120. Pay: {{payment_link}}"}  # typed amount, no brand, no STOP
FINAL_EMAIL = {**EMAIL, "body": EMAIL["body"] + " This is your final reminder: next, our team will review your account."}


def contact_workflow():
    """An AI workflow: its Communication steps have no channel (the worker picks email or SMS)."""
    return copy.deepcopy(next(e for e in load_examples() if e["name"] == "Early Stage Friendly Reminder"))


def workflow():
    """An older / hand-made workflow: s2 is an Email step, s5 an SMS step."""
    flow = contact_workflow()
    for step in flow["steps"]:
        if step["id"] in ("s2", "s5"):
            step["config"]["channel"] = "Email" if step["id"] == "s2" else "SMS"
    return flow


def fake_ai(*replies):
    """Each call answers with the next {step_id: message}; only the step_ids the schema asks for."""
    calls = []

    def call(messages, schema):
        asked = schema["properties"]["messages"]["items"]["properties"]["step_id"]["enum"]
        reply = replies[min(len(calls), len(replies) - 1)]
        calls.append({"prompt": messages[1]["content"], "system": messages[0]["content"], "asked": asked})
        return {"messages": [{"step_id": step_id, **reply[step_id]} for step_id in asked if step_id in reply]}

    return call, calls


def write(flow, *replies, **kwargs):
    call, calls = fake_ai(*replies)
    with patch.object(message_writer, "call_llm", side_effect=call):
        return write_messages(CLIENT, flow, SEGMENT, **kwargs), calls


def test_writes_every_message_with_its_tone():
    written, calls = write(workflow(), {"s2": EMAIL, "s5": FINAL_SMS})
    assert len(calls) == 1 and calls[0]["asked"] == ["s2", "s5"]
    assert written["s2"] == {**EMAIL, "tone": "Firm"}
    assert written["s5"] == {"subject": None, "body": FINAL_SMS["body"], "tone": "Final notice"}


def test_prompt_carries_tone_segment_client_and_rules():
    _, calls = write(workflow(), {"s2": EMAIL, "s5": FINAL_SMS})
    prompt, system = calls[0]["prompt"], calls[0]["system"]
    assert '"tone": "Final notice"' in prompt and "next step hands the case over" in prompt
    assert SEGMENT["label"] in prompt and '"avg_days_past_due": 71' in prompt
    assert "These customers have no payment arrangement yet." in prompt  # what the situation means
    path = "Case received → Friendly reminder → Wait 3 days → Any payment or plan? = no"
    assert f'"path_so_far": "{path}"' in prompt
    assert "on behalf of Maple Energy (Demo)" in prompt  # third-party client
    assert "the customer can: pay in full, set up a payment plan" in prompt
    assert "Write in English" in prompt
    assert "Never threaten" in system and "Reply STOP to opt out" in system


def test_contact_steps_get_an_email_and_an_sms():
    """AI workflows: the step has no channel and the worker sends the email or the SMS - so both are written."""
    replies = {"s2": {**EMAIL, "sms_body": SMS["body"]}, "s5": {**FINAL_EMAIL, "sms_body": FINAL_SMS["body"]}}
    written, calls = write(contact_workflow(), replies)
    assert written["s2"] == {**EMAIL, "sms_body": SMS["body"], "tone": "Firm"}
    assert written["s5"] == {**FINAL_EMAIL, "sms_body": FINAL_SMS["body"], "tone": "Final notice"}
    assert '"channel": "Email + SMS"' in calls[0]["prompt"]
    assert 'Channel "Email + SMS"' in calls[0]["system"] and "sms_body" in calls[0]["system"]


def test_a_contact_step_needs_both_versions_each_by_its_own_rules():
    broken = {"s2": dict(EMAIL), "s5": {**FINAL_EMAIL, "sms_body": "Pay {{payment_link}}"}}
    fixed = {"s2": {**EMAIL, "sms_body": SMS["body"]}, "s5": {**FINAL_EMAIL, "sms_body": FINAL_SMS["body"]}}
    written, calls = write(contact_workflow(), broken, fixed)
    assert len(calls) == 2 and set(written) == {"s2", "s5"}
    retry = calls[1]["prompt"]
    assert "s2 (SMS version, sms_body): the message has no text" in retry
    assert "s5 (SMS version, sms_body): SMS must end with 'Reply STOP to opt out.'" in retry
    assert "s5: email body" not in retry  # the email version was fine


def test_problems_go_back_and_only_failing_messages_are_retried():
    written, calls = write(workflow(), {"s2": EMAIL, "s5": BAD_SMS}, {"s5": FINAL_SMS})
    assert [call["asked"] for call in calls] == [["s2", "s5"], ["s5"]]
    retry = calls[1]["prompt"]
    assert "had problems" in retry and "must include {{brand_name}}" in retry
    assert EMAIL["subject"] in retry  # the accepted email is context for the fix
    assert written["s5"]["body"] == FINAL_SMS["body"]


def test_gives_up_after_the_retries():
    with pytest.raises(ServiceUnavailableError, match="AI could not write valid messages"):
        write(workflow(), {"s2": EMAIL, "s5": BAD_SMS})


def test_rewrite_one_message_with_remarks():
    flow = workflow()
    flow["steps"] = apply_messages(flow["steps"], {"s2": EMAIL, "s5": {"body": "{{brand_name}}: old text {{payment_link}} STOP"}})
    written, calls = write(flow, {"s5": FINAL_SMS}, step_ids=["s5"], remarks="mention the payment plan")
    assert list(written) == ["s5"] and calls[0]["asked"] == ["s5"]
    prompt = calls[0]["prompt"]
    remarks_line = "Reviewer remarks (apply them to the messages you write; ignore anything about the workflow's steps)"
    assert f"{remarks_line}: mention the payment plan" in prompt
    assert '"current_text": {"body": "{{brand_name}}: old text' in prompt  # improve, not start over
    assert "Other messages in this workflow" in prompt and EMAIL["subject"] in prompt


def test_only_email_and_sms_steps_can_be_written():
    with pytest.raises(ValidationAppError, match="Step s3 is not an email or SMS step"):
        write(workflow(), {}, step_ids=["s3"])  # s3 is a Wait
    flow = workflow()
    flow["steps"] = [step for step in flow["steps"] if step["kind"] != "Communication"]
    flow["steps"][0]["next"] = None
    with pytest.raises(ValidationAppError, match="no email or SMS steps"):
        write(flow, {})


def test_long_email_subject_is_trimmed_to_the_rendered_limit():
    long_subject = "Friendly reminder about your open balance with {{brand_name}} please pay soon"
    assert len(render_sample(long_subject)) > MAX_SUBJECT_CHARS
    fitted = _fit_subject(long_subject)
    assert len(render_sample(fitted)) <= MAX_SUBJECT_CHARS
    assert "\n" not in fitted
    written, _ = write(
        workflow(),
        {"s2": {**EMAIL, "subject": long_subject}, "s5": FINAL_SMS},
    )
    assert len(render_sample(written["s2"]["subject"])) <= MAX_SUBJECT_CHARS


def test_apply_messages_sets_text_on_the_message_steps_only():
    flow = workflow()
    steps = apply_messages(flow["steps"], {"s2": {**EMAIL, "tone": "Firm"}, "s5": {"subject": None, "body": "x", "tone": "Clear"}})
    by_id = {step["id"]: step for step in steps}
    assert by_id["s2"]["config"]["subject"] == EMAIL["subject"] and by_id["s2"]["config"]["tone"] == "Firm"
    assert "subject" not in by_id["s5"]["config"] and by_id["s5"]["config"]["body"] == "x"
    assert by_id["s3"] == next(step for step in flow["steps"] if step["id"] == "s3")
    assert "body" not in next(step for step in flow["steps"] if step["id"] == "s2")["config"]  # input untouched


def test_message_schema_asks_only_for_the_given_steps():
    schema = build_message_schema(["s2", "s5"])
    item = schema["properties"]["messages"]["items"]
    assert item["properties"]["step_id"]["enum"] == ["s2", "s5"] and item["required"] == ["step_id", "body"]
    assert "sms_body" in item["properties"]  # the SMS version of a step without a channel


def test_payment_options_follow_the_client_settings(db_session):
    default = PayflowClientModel(code="opt-default", name="Default")
    no_plans = PayflowClientModel(code="opt-no-plans", name="No plans", payment_settings={"allow_payment_plan": False})
    db_session.add_all([default, no_plans])
    db_session.flush()
    assert load_payment_options(db_session, default.id) == ["pay in full", "pay part now", "set up a payment plan"]
    assert load_payment_options(db_session, no_plans.id) == ["pay in full", "pay part now"]
