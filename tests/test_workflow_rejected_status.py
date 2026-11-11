"""A regenerated workflow's original becomes "Rejected": closed, linked to its new version, and listed.

The frontend's "Reject / Regenerate" button calls Regenerate first and Reject right after (Draft,
AI Proposed, Under Review, Approved) - or Reject first for Active / Inactive workflows.
"""

import re

import pytest

from app.core.exceptions import ValidationAppError
from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from tests.test_workflow_regenerate import REMARKS, admin, ai_workflow, regenerate  # noqa: F401 (fixtures)
from tests.test_workflow_suggestion_api import portfolio  # noqa: F401 (fixture)


def _rejected(db_session, admin, ai_workflow):  # noqa: F811
    """Regenerate, then reject - what the frontend does. Returns (original, new)."""
    new, _ = regenerate(db_session, admin, ai_workflow["id"])
    PayflowStrategyService(db_session).reject(admin, ai_workflow["id"], note=REMARKS)
    return PayflowStrategyService(db_session).get_strategy(admin, ai_workflow["id"]), new


def test_the_original_is_rejected_and_its_history_says_so_on_top(db_session, admin, ai_workflow):  # noqa: F811
    original, new = _rejected(db_session, admin, ai_workflow)
    assert original["status"] == "Rejected" and original["awaiting_review"] is False
    top = original["versions"][0]  # the frontend's follow-up reject did not add an "AI Proposed" row on top
    assert top["status"] == "Rejected" and f"New version: #{new['id']} (v2)" in top["note"]
    assert re.fullmatch(r"\d{2} \w{3} \d{4}, \d{2}:\d{2} UTC", top["date"])
    assert [v["status"] for v in original["versions"]].count("Rejected") == 1


def test_both_workflows_link_to_each_other(db_session, admin, ai_workflow):  # noqa: F811
    original, new = _rejected(db_session, admin, ai_workflow)
    assert new["revision_of_id"] == original["id"] and original["replaced_by_id"] == new["id"]
    assert new["replaced_by_id"] is None and new["status"] == "AI Proposed" and new["version"] == 2


def test_the_new_versions_history_says_where_it_came_from(db_session, admin, ai_workflow):  # noqa: F811
    original, new = _rejected(db_session, admin, ai_workflow)
    first = new["versions"][-1]  # oldest = its creation
    assert first["note"].startswith(
        f"Proposed by PayFlow AI as the next version of #{original['id']} {original['name']} (v1), "
        f"rejected by {admin.full_name} on "
    )
    assert first["note"].endswith(f"Remarks: {REMARKS}")


def test_rejected_workflows_are_listed_with_their_status(db_session, admin, ai_workflow):  # noqa: F811
    original, new = _rejected(db_session, admin, ai_workflow)
    listed = PayflowStrategyService(db_session).list_strategies(admin)
    assert "Rejected" in listed["statuses"] and listed["summary"]["rejected"] == 1
    by_id = {s["id"]: s for s in listed["strategies"]}
    assert by_id[original["id"]]["status"] == "Rejected" and by_id[original["id"]]["replaced_by_id"] == new["id"]
    only = PayflowStrategyService(db_session).list_strategies(admin, status="Rejected")["strategies"]
    assert [s["id"] for s in only] == [original["id"]]


def test_a_rejected_workflow_is_closed(db_session, admin, ai_workflow):  # noqa: F811
    original, new = _rejected(db_session, admin, ai_workflow)
    strategies = PayflowStrategyService(db_session)
    refused = {
        "edit": lambda: strategies.update_strategy(admin, original["id"], {"name": "Renamed"}),
        "save draft": lambda: strategies.save_draft(admin, original["id"]),
        "begin review": lambda: strategies.begin_review(admin, original["id"]),
        "submit for review": lambda: strategies.submit_review(admin, original["id"]),
        "approve": lambda: strategies.approve(admin, original["id"]),
        "activate": lambda: strategies.activate(admin, original["id"]),
        "deactivate": lambda: strategies.deactivate(admin, original["id"]),
        "AI text": lambda: strategies.save_ai_messages(admin, original["id"], original["steps"]),
        "regenerate": lambda: WorkflowSuggestionService(db_session).regenerate_workflow(admin, original["id"], REMARKS),
        "write messages": lambda: WorkflowSuggestionService(db_session).write_workflow_messages(admin, original["id"]),
    }
    for action, call in refused.items():
        with pytest.raises(ValidationAppError, match=f"rejected and replaced by #{new['id']}"):
            call()
        assert strategies.get_strategy(admin, original["id"])["status"] == "Rejected", action

    strategies.delete_strategy(admin, original["id"])  # deleting stays possible (Operations Admin)


def test_an_active_workflow_is_rejected_first_then_regenerated(db_session, admin, ai_workflow):  # noqa: F811
    """Active / Inactive: the frontend rejects first (that stops its runs), then regenerates."""
    strategies = PayflowStrategyService(db_session)
    strategies.begin_review(admin, ai_workflow["id"])
    strategies.approve(admin, ai_workflow["id"])
    strategies.activate(admin, ai_workflow["id"])
    assert strategies.reject(admin, ai_workflow["id"], note=REMARKS)["status"] == "AI Proposed"

    new, _ = regenerate(db_session, admin, ai_workflow["id"])
    original = strategies.get_strategy(admin, ai_workflow["id"])
    assert original["status"] == "Rejected" and original["replaced_by_id"] == new["id"]
    assert new["version"] == original["version"] + 1
