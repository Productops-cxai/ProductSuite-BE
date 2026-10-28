"""POST /payflow/workflows/{id}/content — the AI writes a workflow's email / SMS text and it is saved."""

import json
from unittest.mock import patch

from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion import message_writer
from app.modules.payflow.services.workflow_suggestion.learning import _lesson
from app.modules.payflow.services.workflow_suggestion.prompt import build_review_history, load_examples
from tests.test_workflow_message_validator import EMAIL
from tests.test_workflow_regenerate import admin, ai_workflow, portfolio  # noqa: F401 (fixtures)


def fake_message_ai(fail=False):
    """Answers every message the prompt asks for, in its channel and tone (fail=True: always invalid)."""
    prompts = []

    def call(messages, schema):
        prompt = messages[1]["content"]
        prompts.append(prompt)
        asked = prompt.split("Messages to write (one per step_id):\n", 1)[1].split("\n\n", 1)[0]
        briefs = [json.loads(line) for line in asked.splitlines()]
        return {"messages": [{"step_id": b["step_id"], **({"body": "pay now"} if fail else good_text(b))} for b in briefs]}

    return call, prompts


def good_text(brief):
    final = " This is your final reminder: next, our team will review your account." if brief["tone"] == "Final notice" else ""
    if brief["channel"] == "SMS":
        body = f"{{{{brand_name}}}}: {{{{outstanding_balance}}}} is open ({brief['step_id']}).{final} Pay: {{{{payment_link}}}} Reply STOP to opt out."
        return {"body": body}
    return {"subject": EMAIL["subject"], "body": EMAIL["body"] + f" ({brief['step_id']}){final}"}


def write(client, token, workflow_id, body=None, fail=False):
    call, prompts = fake_message_ai(fail)
    with patch.object(message_writer, "call_llm", side_effect=call):
        response = client.post(
            f"/payflow/workflows/{workflow_id}/content", json=body, headers={"Authorization": f"Bearer {token}"}
        )
    return response, prompts


def get(client, token, workflow_id):
    return client.get(f"/payflow/workflows/{workflow_id}", headers={"Authorization": f"Bearer {token}"}).json()


def messages(workflow):
    return {step["id"]: step["config"] for step in workflow["steps"] if step["kind"] == "Communication"}


def test_writes_every_message_and_keeps_it_an_ai_proposal(client, admin_token, ai_workflow):  # noqa: F811
    response, prompts = write(client, admin_token, ai_workflow["id"])  # no body at all
    body = response.json()
    assert response.status_code == 200, body
    assert len(prompts) == 1
    assert all(cfg.get("body") and cfg.get("tone") for cfg in messages(body).values())

    assert body["status"] == "AI Proposed"  # still the AI's proposal: no review started
    assert body["origin"] == "AI Proposed" and body["human_modified"] is False
    assert body["versions"][0]["note"].startswith("Message text written by AI for")
    assert body["versions"][0]["changes"] == []
    def text(configs):
        return {step_id: (cfg.get("subject"), cfg["body"], cfg["tone"]) for step_id, cfg in configs.items()}

    snapshot = {s["id"]: s["config"] for s in body["ai_proposal_snapshot"]["steps"] if s["kind"] == "Communication"}
    assert text(snapshot) == text(messages(body))  # the AI's words are part of the AI proposal


def test_reviewer_edit_after_ai_text_is_a_human_change(client, admin_token, ai_workflow):  # noqa: F811
    write(client, admin_token, ai_workflow["id"])
    steps = get(client, admin_token, ai_workflow["id"])["steps"]
    edited = next(step for step in steps if step["kind"] == "Communication")
    edited["config"]["body"] += " We are here to help."
    body = client.post(
        f"/payflow/workflows/{ai_workflow['id']}/update",
        json={"steps": steps},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    assert body["origin"] == "Human Modified"
    assert f"{edited['title']}: message text edited" in body["versions"][0]["changes"]


def test_rewrite_one_message_with_remarks(client, admin_token, ai_workflow):  # noqa: F811
    first = messages(write(client, admin_token, ai_workflow["id"])[0].json())
    target, *others = list(first)
    response, prompts = write(client, admin_token, ai_workflow["id"], {"step_id": target, "remarks": "softer please"})
    body = response.json()
    assert response.status_code == 200, body

    assert "Reviewer remarks (apply them to the messages you write): softer please" in prompts[0]
    assert '"current_text"' in prompts[0]  # the AI improves the current text
    after = messages(body)
    assert all(after[step_id] == first[step_id] for step_id in others)  # untouched
    assert body["versions"][0]["note"].endswith("(remarks: softer please)")


def test_ai_text_is_not_taken_for_reviewer_feedback(client, admin_token, ai_workflow):  # noqa: F811
    """Suggest lessons and Regenerate history read reviewer edits — the AI's own writing is not one."""
    write(client, admin_token, ai_workflow["id"], {"remarks": "shorter"})
    entry = get(client, admin_token, ai_workflow["id"])["versions"][0]
    assert _lesson(entry, "segment", []) is None
    assert build_review_history([entry]) == ""


def test_approved_workflow_is_refused(client, admin_token, db_session, admin, ai_workflow):  # noqa: F811
    strategies = PayflowStrategyService(db_session)
    strategies.begin_review(admin, ai_workflow["id"])
    strategies.approve(admin, ai_workflow["id"])
    response, prompts = write(client, admin_token, ai_workflow["id"])
    assert response.status_code == 400
    assert "only be written for Draft, AI Proposed or Under Review" in response.json()["detail"]
    assert prompts == []  # refused before any AI call


def test_only_message_steps_can_be_rewritten(client, admin_token, ai_workflow):  # noqa: F811
    for step_id in (ai_workflow["entry_node_id"], "nope"):
        response, _ = write(client, admin_token, ai_workflow["id"], {"step_id": step_id})
        assert response.status_code == 400
        assert "is not an email or SMS step" in response.json()["detail"]


def test_ai_failure_saves_nothing(client, admin_token, ai_workflow):  # noqa: F811
    before = get(client, admin_token, ai_workflow["id"])
    response, prompts = write(client, admin_token, ai_workflow["id"], fail=True)
    assert response.status_code == 502
    assert len(prompts) == 3  # first try + 2 fixes
    after = get(client, admin_token, ai_workflow["id"])
    assert after["steps"] == before["steps"] and len(after["versions"]) == len(before["versions"])


def test_hand_made_workflow_gets_messages_too(client, admin_token, db_session, admin, portfolio):  # noqa: F811
    human = PayflowStrategyService(db_session).create_strategy(
        admin,
        {
            "name": "Manual workflow",
            "client_id": portfolio["client_id"],
            "portfolio_id": portfolio["portfolio_id"],
            "segment": {"delinquency": "60 – 89 days past due"},
            "steps": load_examples()[0]["steps"],
        },
    )
    response, prompts = write(client, admin_token, human["id"])
    body = response.json()
    assert response.status_code == 200, body
    assert "Segment: Custom segment" in prompts[0]
    assert [cfg["tone"] for cfg in messages(body).values()] == ["Firm", "Final notice"]
    assert body["origin"] == "Human Created"
