"""Adaptive workflows: follow-ups on workflows built by hand (plan segment 8).

"Write messages" adds the adaptive follow-ups a workflow does not have yet, then writes every message - the same
as Suggest and Regenerate. `follow_ups_only` (POST /workflows/{id}/content) adds them and writes only the
follow-ups without text: a person's own texts are never touched. Only workflows with a reminder or payment-link
message get follow-ups; "plan setup not finished" only for clients that offer payment plans.
"""

import copy
from unittest.mock import patch

from app.infrastructure.database.models import PayflowClientModel
from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion import message_writer
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from tests.test_adaptive_follow_ups import FOLLOW_UP_IDS, WITHOUT_PLAN_SETUP, _asked
from tests.test_workflow_message_api import fake_message_ai, flowchart_messages, get, messages, write
from tests.test_workflow_regenerate import admin, ai_workflow, portfolio  # noqa: F401 (fixtures)

ONLY_FOLLOW_UPS = {"follow_ups_only": True}


def _hand_built(db_session, admin, portfolio, *, purpose=None):  # noqa: F811
    """A Draft workflow built by hand: the example steps, every message with a person's own text."""
    steps = copy.deepcopy(load_examples()[0]["steps"])
    for step in steps:
        if step["kind"] == "Communication":
            step["config"] = {**(step.get("config") or {}), "subject": f"My subject {step['id']}",
                              "body": f"My own words for {step['id']}.", "sms_body": f"My SMS {step['id']}."}
            if purpose:
                step["config"]["purpose"] = purpose
    return PayflowStrategyService(db_session).create_strategy(
        admin,
        {"name": "Built by hand", "client_id": portfolio["client_id"], "portfolio_id": portfolio["portfolio_id"],
         "segment": {"delinquency": "30 – 59 days past due"}, "steps": steps},
    )


def _follow_ups(workflow):
    return {step_id: config for step_id, config in messages(workflow).items() if config.get("follow_up_for")}


def _texts(configs):
    return {step_id: tuple(config.get(part) for part in ("subject", "body", "sms_body"))
            for step_id, config in configs.items()}


def _write_without_plan_offers(client, token, workflow_id, body):
    """POST /content with a writer whose text never offers a payment plan."""
    write_, prompts = fake_message_ai()

    def writer(messages_, schema):
        reply = write_(messages_, schema)
        for message in reply["messages"]:
            message["body"] = message["body"].replace("pay in full or set up a plan", "pay")
        return reply

    with patch.object(message_writer, "call_llm", side_effect=writer):
        response = client.post(f"/payflow/workflows/{workflow_id}/content", json=body,
                               headers={"Authorization": f"Bearer {token}"})
    return response, prompts


def test_follow_ups_only_adds_and_writes_the_follow_ups_and_keeps_a_persons_texts(  # noqa: F811
    client, admin_token, db_session, admin, portfolio
):
    built = _hand_built(db_session, admin, portfolio)
    own_texts = _texts(flowchart_messages(built))
    assert all(text[1].startswith("My own words") for text in own_texts.values())

    response, prompts = write(client, admin_token, built["id"], ONLY_FOLLOW_UPS)
    body = response.json()
    assert response.status_code == 200, body
    assert len(prompts) == 1 and list(_asked(prompts[0])) == FOLLOW_UP_IDS  # one call: the follow-ups only
    assert _texts(flowchart_messages(body)) == own_texts  # every hand-typed text exactly as it was
    follow_ups = _follow_ups(body)
    assert list(follow_ups) == FOLLOW_UP_IDS and all(config["body"] for config in follow_ups.values())
    assert (body["origin"], body["status"]) == (built["origin"], built["status"])
    assert body == get(client, admin_token, built["id"])  # saved


def test_follow_ups_only_writes_just_the_ones_without_text(client, admin_token, db_session, admin, portfolio):  # noqa: F811
    built = _hand_built(db_session, admin, portfolio)
    written = write(client, admin_token, built["id"], ONLY_FOLLOW_UPS)[0].json()

    steps = copy.deepcopy(written["steps"])
    for step in steps:
        if step["id"] in ("fu_no_activity", "fu_no_activity_repeat"):
            for part in ("subject", "body", "sms_body"):
                step["config"].pop(part, None)
    PayflowStrategyService(db_session).update_strategy(admin, built["id"], {"steps": steps})

    response, prompts = write(client, admin_token, built["id"], ONLY_FOLLOW_UPS)
    assert response.status_code == 200, response.json()
    assert list(_asked(prompts[0])) == ["fu_no_activity", "fu_no_activity_repeat"]
    after = _follow_ups(response.json())
    assert {k: v for k, v in after.items() if not k.startswith("fu_no_activity")} == {
        k: v for k, v in _follow_ups(written).items() if not k.startswith("fu_no_activity")}

    again, prompts = write(client, admin_token, built["id"], ONLY_FOLLOW_UPS)
    assert again.status_code == 400 and "No follow-up to write" in again.json()["detail"] and prompts == []


def test_write_messages_adds_the_follow_ups_a_workflow_is_missing_once(client, admin_token, ai_workflow):  # noqa: F811
    first = write(client, admin_token, ai_workflow["id"])[0].json()
    second = write(client, admin_token, ai_workflow["id"])[0].json()
    assert list(_follow_ups(first)) == list(_follow_ups(second)) == FOLLOW_UP_IDS  # kept, never duplicated


def test_a_workflow_with_no_reminder_gets_no_follow_ups(client, admin_token, db_session, admin, portfolio):  # noqa: F811
    built = _hand_built(db_session, admin, portfolio, purpose="Payment Plan Reminder")
    response, prompts = write(client, admin_token, built["id"], ONLY_FOLLOW_UPS)
    assert response.status_code == 400 and "no reminder or payment-link message" in response.json()["detail"]
    assert prompts == []

    everything = write(client, admin_token, built["id"])[0].json()
    assert _follow_ups(everything) == {}


def test_a_client_without_payment_plans_gets_no_plan_setup_follow_up(client, admin_token, db_session, admin, portfolio):  # noqa: F811,E501
    db_session.get(PayflowClientModel, portfolio["client_id"]).payment_settings = {"allow_payment_plan": False}
    db_session.commit()
    built = _hand_built(db_session, admin, portfolio)
    response, _ = _write_without_plan_offers(client, admin_token, built["id"], ONLY_FOLLOW_UPS)
    assert response.status_code == 200, response.json()
    assert list(_follow_ups(response.json())) == WITHOUT_PLAN_SETUP


def test_follow_ups_only_is_not_for_one_message(client, admin_token, ai_workflow):  # noqa: F811
    response, prompts = write(client, admin_token, ai_workflow["id"], {**ONLY_FOLLOW_UPS, "step_id": "s2"})
    assert response.status_code == 400 and "leave out step_id and audience" in response.json()["detail"]
    assert prompts == []
