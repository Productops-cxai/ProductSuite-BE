"""Communication steps can hold their own message text (subject / body / tone).

The API keeps and returns it, edits are tracked like any other step change, and
the worker sends it instead of the template (variables filled per account).
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.modules.payflow.services.communication_service import PayflowCommunicationService
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from tests.test_workflow_regenerate import admin, ai_workflow, portfolio  # noqa: F401 (fixtures)

SUBJECT = "About account {{account_reference}}"
BODY = "Hi {{first_name}}, {{outstanding_balance}} is still open. Pay here: {{payment_link}}\n{{brand_name}}"


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


def _send(db_session, step):
    """Run the worker's Communication step for one account; return what it tried to send."""
    client = SimpleNamespace(
        client_type="first_party", email_from=None, sender_name=None, brand_name="Maple Energy", name="Maple"
    )
    account = SimpleNamespace(
        id=1,
        client_id=1,
        client=client,
        customer_first_name="Ali",
        customer_name="Ali Khan",
        outstanding_balance=120,
        account_reference="ACC-991",
        ptp_amount=None,
        ptp_due_date=None,
    )
    service = WorkflowExecutionService(db_session)
    with (
        patch.object(WorkflowExecutionService, "_payment_link", return_value="https://pay.example/abc"),
        patch.object(PayflowCommunicationService, "send_workflow_step", return_value={}) as send,
    ):
        service._send_communication(
            SimpleNamespace(id=1, client_id=1), account, SimpleNamespace(id=1), step, SimpleNamespace(result_json=None)
        )
    return send.call_args.kwargs


def test_worker_sends_the_steps_own_text(db_session):
    step = {
        "kind": "Communication",
        "config": {"channel": "Email", "purpose": "Payment Reminder", "template_id": "em-reminder",
                   "subject": SUBJECT, "body": BODY},
    }
    sent = _send(db_session, step)
    assert sent["subject"] == "About account ACC-991"
    assert sent["body"] == "Hi Ali, $120.00 is still open. Pay here: https://pay.example/abc\nMaple Energy"


def test_worker_without_own_text_still_sends_the_template(db_session):
    step = {"kind": "Communication", "config": {"channel": "Email", "purpose": "Payment Reminder",
                                                "template_id": "em-reminder"}}
    sent = _send(db_session, step)
    assert sent["subject"] == "A friendly reminder about your balance"
    assert sent["body"].startswith("Hi Ali,\n\nOur records show an outstanding balance of $120.00 on account ACC-991.")
