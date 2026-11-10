"""Communication steps can hold their own message text (subject / body / tone / sms_body).

The API keeps and returns it, edits are tracked like any other step change, and
the worker sends it instead of the template (variables filled per account).
Contact steps (no channel) pick Email or SMS per customer and failover.
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.modules.payflow.services.communication_service import PayflowCommunicationService
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from tests.test_workflow_regenerate import admin, ai_workflow, portfolio  # noqa: F401 (fixtures)

SUBJECT = "About account {{account_reference}}"
BODY = "Hi {{first_name}}, {{outstanding_balance}} is still open. Pay here: {{payment_link}}\n{{brand_name}}"
SMS_BODY = "{{brand_name}}: please pay {{payment_link}} Reply STOP to opt out."


def _get(client, token, workflow_id):
    return client.get(f"/payflow/workflows/{workflow_id}", headers={"Authorization": f"Bearer {token}"}).json()


def _update(client, token, workflow_id, steps):
    return client.post(
        f"/payflow/workflows/{workflow_id}/update",
        json={"steps": steps},
        headers={"Authorization": f"Bearer {token}"},
    )


def _first_message(steps):
    return next(step for step in steps if step["kind"] == "Communication")


def test_message_text_is_saved_and_returned(client, admin_token, ai_workflow):  # noqa: F811
    steps = _get(client, admin_token, ai_workflow["id"])["steps"]
    _first_message(steps)["config"].update({"subject": SUBJECT, "body": BODY, "tone": "Friendly"})
    response = _update(client, admin_token, ai_workflow["id"], steps)
    assert response.status_code == 200, response.json()

    config = _first_message(_get(client, admin_token, ai_workflow["id"])["steps"])["config"]
    assert (config["subject"], config["body"], config["tone"]) == (SUBJECT, BODY, "Friendly")


def test_text_edit_is_tracked_as_a_human_change(client, admin_token, ai_workflow):  # noqa: F811
    steps = _get(client, admin_token, ai_workflow["id"])["steps"]
    message = _first_message(steps)
    message["config"]["body"] = BODY
    body = _update(client, admin_token, ai_workflow["id"], steps).json()

    assert body["origin"] == "Human Modified"
    assert f"{message['title']}: message text edited" in body["versions"][0]["changes"]


def test_resending_the_same_text_is_not_a_change(client, admin_token, ai_workflow):  # noqa: F811
    steps = _get(client, admin_token, ai_workflow["id"])["steps"]
    _first_message(steps)["config"]["body"] = BODY
    _update(client, admin_token, ai_workflow["id"], steps)

    loaded = _get(client, admin_token, ai_workflow["id"])
    versions_before = len(loaded["versions"])
    body = _update(client, admin_token, ai_workflow["id"], loaded["steps"]).json()
    assert len(body["versions"]) == versions_before  # nothing new to record


def test_empty_text_boxes_are_not_a_change(client, admin_token, ai_workflow):  # noqa: F811
    """A UI that sends "" for untouched subject / body boxes must not mark the workflow modified."""
    steps = _get(client, admin_token, ai_workflow["id"])["steps"]
    for step in steps:
        if step["kind"] == "Communication":
            step["config"].update({"subject": "", "body": "  ", "tone": ""})
    body = _update(client, admin_token, ai_workflow["id"], steps).json()

    assert body["origin"] == "AI Proposed" and body["human_modified"] is False
    assert _first_message(body["steps"])["config"]["body"] is None


def test_too_long_subject_is_refused(client, admin_token, ai_workflow):  # noqa: F811
    steps = _get(client, admin_token, ai_workflow["id"])["steps"]
    _first_message(steps)["config"]["subject"] = "x" * 256
    response = _update(client, admin_token, ai_workflow["id"], steps)
    assert response.status_code == 422


# --------------------------------------------------------------------------- worker


def _account(**overrides):
    client = SimpleNamespace(
        client_type="first_party",
        email_from=None,
        sender_name=None,
        brand_name="Maple Energy",
        name="Maple",
        channel_email=True,
        channel_sms=True,
    )
    base = dict(
        id=1,
        client_id=1,
        client=client,
        customer_first_name="Ali",
        customer_name="Ali Khan",
        outstanding_balance=120,
        account_reference="ACC-991",
        ptp_amount=None,
        ptp_due_date=None,
        email="ali@example.com",
        email_consent=True,
        phone_mobile="+14155552671",
        phone_work=None,
        sms_opt_out=False,
        provincial_hold=False,
        collection_status="Active",
        human_review=False,
        next_action=None,
        last_action=None,
        timeline=[],
        recovered_balance=0.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _send(db_session, step, *, account=None, send_side_effect=None, send_return=None):
    """Run the worker's Communication step; return send_workflow_step call kwargs list."""
    account = account or _account()
    step_run = SimpleNamespace(result_json=None)
    ok = {"status": "sent", "code": "COM-1", "recipient": "x"}
    service = WorkflowExecutionService(db_session)
    with (
        patch.object(WorkflowExecutionService, "_payment_link", return_value="https://pay.example/abc"),
        patch.object(
            PayflowCommunicationService,
            "send_workflow_step",
            side_effect=send_side_effect,
            return_value=send_return if send_side_effect is None else None,
        ) as send,
    ):
        if send_side_effect is None and send_return is None:
            send.return_value = ok
        service._send_communication(
            SimpleNamespace(id=1, client_id=1),
            account,
            SimpleNamespace(id=1),
            step,
            step_run,
        )
    return [c.kwargs for c in send.call_args_list], step_run, account


def test_worker_sends_the_steps_own_text(db_session):
    step = {
        "kind": "Communication",
        "config": {
            "channel": "Email",
            "purpose": "Payment Reminder",
            "template_id": "em-reminder",
            "subject": SUBJECT,
            "body": BODY,
        },
    }
    calls, _, _ = _send(db_session, step)
    assert len(calls) == 1
    assert calls[0]["subject"] == "About account ACC-991"
    assert calls[0]["body"] == "Hi Ali, $120.00 is still open. Pay here: https://pay.example/abc\nMaple Energy"


def test_worker_picks_email_for_a_contact_step_when_email_is_eligible(db_session):
    """AI contact steps have no channel — worker picks Email when the customer can receive it."""
    step = {
        "kind": "Communication",
        "config": {
            "purpose": "Payment Reminder",
            "subject": SUBJECT,
            "body": BODY,
            "sms_body": SMS_BODY,
        },
    }
    calls, step_run, _ = _send(db_session, step)
    assert len(calls) == 1
    assert calls[0]["channel"] == "Email"
    assert calls[0]["subject"] == "About account ACC-991"
    assert "Reply STOP" not in calls[0]["body"]
    assert step_run.result_json.get("chosen_channel") == "Email"


def test_worker_picks_sms_and_uses_sms_body_when_email_unreachable(db_session):
    step = {
        "kind": "Communication",
        "config": {
            "purpose": "Payment Reminder",
            "subject": SUBJECT,
            "body": BODY,
            "sms_body": SMS_BODY,
        },
    }
    account = _account(email=None, email_consent=False)
    calls, step_run, _ = _send(db_session, step, account=account)
    assert len(calls) == 1
    assert calls[0]["channel"] == "SMS"
    assert calls[0]["subject"] == ""
    assert calls[0]["body"].startswith("Maple Energy: please pay https://pay.example/abc")
    assert "Reply STOP to opt out." in calls[0]["body"]
    assert step_run.result_json.get("chosen_channel") == "SMS"


def test_worker_failovers_to_sms_when_email_provider_fails(db_session):
    step = {
        "kind": "Communication",
        "config": {
            "purpose": "Payment Reminder",
            "subject": SUBJECT,
            "body": BODY,
            "sms_body": SMS_BODY,
        },
    }

    def _side_effect(**kwargs):
        if kwargs["channel"] == "Email":
            return {"status": "failed", "provider_error": "sendgrid_failed", "channel": "Email"}
        return {"status": "sent", "code": "COM-SMS", "recipient": "+1", "channel": "SMS"}

    calls, step_run, _ = _send(db_session, step, send_side_effect=_side_effect)
    assert [c["channel"] for c in calls] == ["Email", "SMS"]
    assert step_run.result_json.get("chosen_channel") == "SMS"
    assert "Reply STOP" in calls[1]["body"]


def test_worker_flags_unreachable_when_both_channels_fail(db_session):
    step = {
        "kind": "Communication",
        "config": {
            "purpose": "Payment Reminder",
            "subject": SUBJECT,
            "body": BODY,
            "sms_body": SMS_BODY,
        },
    }
    account = _account(email=None, phone_mobile=None, phone_work=None)
    calls, step_run, account = _send(
        db_session,
        step,
        account=account,
        send_return={
            "skipped": "eligibility",
            "status": "blocked",
            "restriction_reason": "No valid email on file",
        },
    )
    assert len(calls) == 2  # still attempts both for audit
    assert step_run.result_json.get("flagged_unreachable") is True
    assert account.human_review is True
    assert account.next_action == "No contactable channel"


def test_worker_without_own_text_still_sends_the_template(db_session):
    step = {
        "kind": "Communication",
        "config": {
            "channel": "Email",
            "purpose": "Payment Reminder",
            "template_id": "em-reminder",
        },
    }
    calls, _, _ = _send(db_session, step)
    assert calls[0]["subject"] == "A friendly reminder about your balance"
    assert calls[0]["body"].startswith(
        "Hi Ali,\n\nOur records show an outstanding balance of $120.00 on account ACC-991."
    )
