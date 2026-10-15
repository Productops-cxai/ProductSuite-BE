"""Optional reviewer notes on approve / edit — saved as `reviewer_note` in the workflow's version history."""

import copy

import pytest

from app.infrastructure.database.models import UserModel
from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from tests.test_workflow_suggestion_api import portfolio  # noqa: F401 (fixture)


@pytest.fixture()
def workflow(db_session, portfolio):  # noqa: F811
    """A hand-made Draft workflow (example 1: s3 waits 3 days)."""
    admin = db_session.query(UserModel).filter(UserModel.email == "admin@payflow.ai").one()
    return PayflowStrategyService(db_session).create_strategy(
        admin,
        {
            "name": "Manual workflow",
            "client_id": portfolio["client_id"],
            "portfolio_id": portfolio["portfolio_id"],
            "steps": load_examples()[0]["steps"],
        },
    )


def _post(client, token, path, body=None):
    return client.post(
        f"/payflow/workflows/{path}", json=body, headers={"Authorization": f"Bearer {token}"}
    )


def test_approve_with_note(client, admin_token, workflow):
    _post(client, admin_token, f"{workflow['id']}/submit-review")
    response = _post(client, admin_token, f"{workflow['id']}/approve", {"note": "  Tone fits this segment  "})
    assert response.status_code == 200, response.json()
    latest = response.json()["versions"][0]
    assert latest["note"].startswith("Approved by")
    assert latest["reviewer_note"] == "Tone fits this segment"


def test_approve_without_body_still_works(client, admin_token, workflow):
    """The FE calls approve with no body today."""
    _post(client, admin_token, f"{workflow['id']}/submit-review")
    response = _post(client, admin_token, f"{workflow['id']}/approve")
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "Approved"
    assert response.json()["versions"][0]["reviewer_note"] is None


def test_reject_note_is_also_saved_as_reviewer_note(client, admin_token, workflow):
    _post(client, admin_token, f"{workflow['id']}/submit-review")
    latest = _post(client, admin_token, f"{workflow['id']}/reject", {"note": "Too aggressive"}).json()["versions"][0]
    assert latest["note"] == "Too aggressive"  # unchanged behaviour
    assert latest["reviewer_note"] == "Too aggressive"

    _post(client, admin_token, f"{workflow['id']}/submit-review")
    latest = _post(client, admin_token, f"{workflow['id']}/reject", {}).json()["versions"][0]  # FE sends {}
    assert latest["note"].startswith("Rejected / regeneration requested by")  # unchanged default
    assert latest["reviewer_note"] is None


def test_edit_note_is_saved_with_the_changes(client, admin_token, workflow):
    steps = copy.deepcopy(load_examples()[0]["steps"])
    steps[2]["config"]["amount"] = 7  # s3: wait 7 days instead of 3
    response = _post(client, admin_token, f"{workflow['id']}/update", {"steps": steps, "note": "SMS was too early"})
    assert response.status_code == 200, response.json()
    latest = response.json()["versions"][0]
    assert latest["reviewer_note"] == "SMS was too early"
    assert any("timing changed" in change for change in latest["changes"])


def test_edit_with_only_a_note_is_kept(client, admin_token, workflow):
    before = len(workflow["versions"])
    response = _post(client, admin_token, f"{workflow['id']}/update", {"summary": "Clearer summary", "note": "Reworded"})
    assert response.status_code == 200, response.json()
    versions = response.json()["versions"]
    assert len(versions) == before + 1
    assert versions[0]["reviewer_note"] == "Reworded" and versions[0]["changes"] == []


def test_edit_without_note_or_step_changes_adds_no_history(client, admin_token, workflow):
    """Unchanged behaviour: no step change and no (or a blank) note → no history entry."""
    before = len(workflow["versions"])
    response = _post(client, admin_token, f"{workflow['id']}/update", {"summary": "Clearer summary", "note": "   "})
    assert response.status_code == 200, response.json()
    assert len(response.json()["versions"]) == before
