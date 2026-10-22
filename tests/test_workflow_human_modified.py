"""An AI workflow is marked "Human Modified" only when its steps or segment really change."""

from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from tests.test_workflow_regenerate import admin, ai_workflow, portfolio  # noqa: F401 (fixtures)


def _get(client, token, workflow_id):
    return client.get(f"/payflow/workflows/{workflow_id}", headers={"Authorization": f"Bearer {token}"}).json()


def _post(client, token, path, body=None):
    return client.post(f"/payflow/workflows/{path}", json=body, headers={"Authorization": f"Bearer {token}"})


def test_save_draft_then_approve_stays_ai_proposed(client, admin_token, ai_workflow):  # noqa: F811
    """Approved without changes."""
    _post(client, admin_token, f"{ai_workflow['id']}/save-draft")
    body = _post(client, admin_token, f"{ai_workflow['id']}/approve").json()
    assert body["status"] == "Approved"
    assert body["origin"] == "AI Proposed" and body["human_modified"] is False


def test_rename_or_summary_only_is_not_a_modification(client, admin_token, ai_workflow):  # noqa: F811
    response = _post(client, admin_token, f"{ai_workflow['id']}/update", {"name": "Renamed", "summary": "Shorter"})
    body = response.json()
    assert response.status_code == 200, body
    assert body["status"] == "Under Review"  # editing still starts the review
    assert body["origin"] == "AI Proposed" and body["human_modified"] is False


def test_same_steps_sent_back_is_not_a_modification(client, admin_token, ai_workflow):  # noqa: F811
    """What a UI "Save" does: send the workflow back exactly as it was loaded."""
    loaded = _get(client, admin_token, ai_workflow["id"])
    body = _post(
        client, admin_token, f"{ai_workflow['id']}/update", {"steps": loaded["steps"], "segment": loaded["segment"]}
    ).json()
    assert body["origin"] == "AI Proposed" and body["human_modified"] is False
    assert all(step["origin"] == "AI Proposed" for step in body["steps"])


def test_step_change_is_a_modification_and_survives_approval(client, admin_token, ai_workflow):  # noqa: F811
    steps = _get(client, admin_token, ai_workflow["id"])["steps"]
    steps[2]["config"]["amount"] = 7  # s3: wait 7 days instead of 3
    steps[2]["timing"] = None  # let the server rebuild the timing label
    body = _post(client, admin_token, f"{ai_workflow['id']}/update", {"steps": steps}).json()
    assert body["origin"] == "Human Modified" and body["human_modified"] is True

    approved = _post(client, admin_token, f"{ai_workflow['id']}/approve").json()
    assert approved["status"] == "Approved" and approved["origin"] == "Human Modified"


def test_segment_change_is_a_modification(client, admin_token, ai_workflow):  # noqa: F811
    segment = {**ai_workflow["segment"], "balance_band": "$500 – $1,500"}
    body = _post(client, admin_token, f"{ai_workflow['id']}/update", {"segment": segment}).json()
    assert body["origin"] == "Human Modified"


def test_hand_made_workflow_is_unaffected(client, admin_token, db_session, admin, portfolio):  # noqa: F811
    human = PayflowStrategyService(db_session).create_strategy(
        admin,
        {
            "name": "Manual workflow",
            "client_id": portfolio["client_id"],
            "portfolio_id": portfolio["portfolio_id"],
            "steps": load_examples()[0]["steps"],
        },
    )
    steps = load_examples()[1]["steps"]  # a completely different flowchart
    body = _post(client, admin_token, f"{human['id']}/update", {"steps": steps}).json()
    assert body["origin"] == "Human Created" and body["source"] == "Human Created"
    assert body["steps"][1]["title"] == steps[1]["title"]  # the edit was saved
