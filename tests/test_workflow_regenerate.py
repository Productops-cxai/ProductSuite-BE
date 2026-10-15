import copy
from unittest.mock import patch

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError, ServiceUnavailableError, ValidationAppError
from app.core.security import hash_password
from app.infrastructure.database.models import (
    OrganizationModel,
    PayflowRoleModel,
    PayflowStrategyModel,
    PayflowUserMembershipModel,
    ProductModel,
    RoleModel,
    UserModel,
    UserProductModel,
)
from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from app.shared.enums import PayflowRoleCode, PlatformRole, UserStatus
from tests.test_workflow_suggestion_api import fake_llm, portfolio, supervisor_token  # noqa: F401 (fixtures)

REMARKS = "Less aggressive: no SMS in the first week"


@pytest.fixture()
def admin(db_session):
    return db_session.query(UserModel).filter(UserModel.email == "admin@payflow.ai").one()


@pytest.fixture()
def ai_workflow(db_session, admin, portfolio):  # noqa: F811
    call, _ = fake_llm()
    with patch.object(suggestion_service, "call_llm", side_effect=call):
        result = WorkflowSuggestionService(db_session).suggest_workflows(
            admin, portfolio["client_id"], portfolio["portfolio_id"]
        )
    return result["workflows"][0]


def regenerate(db_session, user, workflow_id, broken_calls=0):
    call, calls = fake_llm(broken_calls=broken_calls)
    with patch.object(suggestion_service, "call_llm", side_effect=call):
        result = WorkflowSuggestionService(db_session).regenerate_workflow(user, workflow_id, REMARKS)
    return result, calls


def context(workflow):
    return {item["label"]: item["value"] for item in workflow["ai_context"]}


def test_regenerate_saves_a_new_linked_version(db_session, admin, ai_workflow):
    new, calls = regenerate(db_session, admin, ai_workflow["id"])

    assert new["id"] != ai_workflow["id"]
    assert new["status"] == "AI Proposed" and new["source"] == "AI Generated"
    assert new["segment"] == ai_workflow["segment"]
    assert new["cases_covered"] == ai_workflow["cases_covered"]
    ctx = context(new)
    assert ctx["Revision of"].startswith(f"#{ai_workflow['id']} ")
    assert ctx["Reviewer remarks"] == REMARKS
    assert ctx["Segment"] == context(ai_workflow)["Segment"]

    # One AI call; the prompt carries the current workflow and the remarks.
    assert len(calls) == 1
    prompt = calls[0][1]["content"]
    assert REMARKS in prompt and ai_workflow["name"] in prompt and "Revise the current workflow" in prompt


def test_original_is_not_changed(db_session, admin, ai_workflow):
    before = copy.deepcopy(PayflowStrategyService(db_session).get_strategy(admin, ai_workflow["id"]))
    regenerate(db_session, admin, ai_workflow["id"])
    after = PayflowStrategyService(db_session).get_strategy(admin, ai_workflow["id"])
    for key in ("status", "version", "steps", "ai_context", "segment"):
        assert after[key] == before[key], key


def test_regenerate_retries_with_validator_errors(db_session, admin, ai_workflow):
    new, calls = regenerate(db_session, admin, ai_workflow["id"], broken_calls=1)
    assert new["status"] == "AI Proposed"
    assert len(calls) == 2
    retry_prompt = calls[1][1]["content"]
    assert REMARKS in retry_prompt and "template 'em-nope' does not exist" in retry_prompt


def test_regenerate_gives_up_and_saves_nothing(db_session, admin, ai_workflow):
    count = db_session.query(PayflowStrategyModel).count()
    with pytest.raises(ServiceUnavailableError, match="AI could not produce valid workflows"):
        regenerate(db_session, admin, ai_workflow["id"], broken_calls=99)
    assert db_session.query(PayflowStrategyModel).count() == count


def test_regenerate_sends_review_history_to_the_ai(db_session, admin, ai_workflow):
    """Edits (what changed + why) and reject notes reach the AI prompt."""
    strategies = PayflowStrategyService(db_session)
    steps = copy.deepcopy(ai_workflow["steps"])
    steps[2]["config"]["amount"] = 7  # s3: wait 7 days instead of 3
    steps[2]["timing"] = None
    strategies.update_strategy(admin, ai_workflow["id"], {"steps": steps, "note": "SMS was too early"})
    strategies.reject(admin, ai_workflow["id"], note="Too aggressive for this segment")

    _, calls = regenerate(db_session, admin, ai_workflow["id"])
    prompt = calls[0][1]["content"]
    assert "Review history (oldest first)" in prompt
    assert "timing changed from 3 Days After Previous Email to 7 Days After Previous Email" in prompt
    assert "Reason: SMS was too early" in prompt
    assert "- Rejected. Reason: Too aggressive for this segment" in prompt
    assert prompt.index("Review history") < prompt.index("Reviewer remarks")


def test_regenerate_follows_the_chain_back_for_feedback(db_session, admin, ai_workflow):
    """#1 rejected with a note → regenerate → #2 → regenerate #2: the AI still sees #1's note."""
    PayflowStrategyService(db_session).reject(admin, ai_workflow["id"], note="Too aggressive for this segment")
    second, _ = regenerate(db_session, admin, ai_workflow["id"])
    assert second["revision_of_id"] == ai_workflow["id"]

    third, calls = regenerate(db_session, admin, second["id"])
    assert third["revision_of_id"] == second["id"]
    assert "- Rejected. Reason: Too aggressive for this segment" in calls[0][1]["content"]


def test_regenerate_works_when_the_parent_was_deleted(db_session, admin, ai_workflow):
    second, _ = regenerate(db_session, admin, ai_workflow["id"])
    PayflowStrategyService(db_session).delete_strategy(admin, ai_workflow["id"])
    third, calls = regenerate(db_session, admin, second["id"])
    assert third["revision_of_id"] == second["id"] and len(calls) == 1


def test_feedback_from_another_client_is_never_used(db_session, admin, portfolio, ai_workflow):  # noqa: F811
    other = PayflowStrategyModel(
        code="other-client-workflow",
        name="Other client workflow",
        client_id=portfolio["other_id"],
        status="AI Proposed",
        origin="AI Proposed",
        source="AI Generated",
        versions=[{"version": 1, "date": "x", "note": "Other secret", "changes": [], "reviewer_note": "Other secret"}],
    )
    db_session.add(other)
    db_session.commit()
    db_session.get(PayflowStrategyModel, ai_workflow["id"]).revision_of_id = other.id
    db_session.commit()

    _, calls = regenerate(db_session, admin, ai_workflow["id"])
    assert "Other secret" not in calls[0][1]["content"]


def test_under_review_can_be_regenerated(db_session, admin, ai_workflow):
    PayflowStrategyService(db_session).begin_review(admin, ai_workflow["id"])
    new, _ = regenerate(db_session, admin, ai_workflow["id"])
    assert new["status"] == "AI Proposed"


def test_approved_cannot_be_regenerated(db_session, admin, ai_workflow):
    strategies = PayflowStrategyService(db_session)
    strategies.begin_review(admin, ai_workflow["id"])
    strategies.approve(admin, ai_workflow["id"])
    with pytest.raises(ValidationAppError, match="Only AI Proposed or Under Review"):
        regenerate(db_session, admin, ai_workflow["id"])


def test_human_workflow_cannot_be_regenerated(db_session, admin, portfolio):  # noqa: F811
    human = PayflowStrategyService(db_session).create_strategy(
        admin,
        {
            "name": "Manual workflow",
            "client_id": portfolio["client_id"],
            "portfolio_id": portfolio["portfolio_id"],
            "steps": load_examples()[0]["steps"],
        },
    )
    with pytest.raises(ValidationAppError, match="Only AI-generated"):
        regenerate(db_session, admin, human["id"])


def test_unknown_workflow(db_session, admin):
    with pytest.raises(NotFoundError):
        regenerate(db_session, admin, 9999)


def test_needs_access_to_the_client(db_session, ai_workflow):
    """A PayFlow supervisor not assigned to the client cannot regenerate."""
    org = db_session.query(OrganizationModel).first()
    role = db_session.query(RoleModel).filter(RoleModel.code == PlatformRole.PLATFORM_USER.value).first()
    user = UserModel(
        full_name="Sam Supervisor",
        email="sam@example.com",
        password_hash=hash_password("User@12345"),
        organization_id=org.id,
        role_id=role.id,
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    db_session.flush()
    payflow = db_session.query(ProductModel).filter(ProductModel.code == "PAYFLOW").first()
    supervisor = (
        db_session.query(PayflowRoleModel).filter(PayflowRoleModel.code == PayflowRoleCode.SUPERVISOR.value).first()
    )
    db_session.add_all(
        [
            UserProductModel(user_id=user.id, product_id=payflow.id),
            PayflowUserMembershipModel(user_id=user.id, payflow_role_id=supervisor.id),
        ]
    )
    db_session.commit()
    with pytest.raises(ForbiddenError):
        regenerate(db_session, user, ai_workflow["id"])


# --------------------------------------------------------------------------- API


def _post(client, token, workflow_id, remarks=REMARKS):
    return client.post(
        f"/payflow/workflows/{workflow_id}/regenerate",
        json={"remarks": remarks},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_api_regenerate(client, admin_token, ai_workflow):
    call, calls = fake_llm()
    with patch.object(suggestion_service, "call_llm", side_effect=call):
        response = _post(client, admin_token, ai_workflow["id"])
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["id"] != ai_workflow["id"] and body["status"] == "AI Proposed"
    assert context(body)["Revision of"].startswith(f"#{ai_workflow['id']} ")
    assert body["revision_of_id"] == ai_workflow["id"]
    assert len(calls) == 1


def test_api_remarks_required(client, admin_token, ai_workflow):
    assert _post(client, admin_token, ai_workflow["id"], remarks="   ").status_code == 422


def test_api_approved_workflow_is_refused(client, admin_token, admin, db_session, ai_workflow):
    strategies = PayflowStrategyService(db_session)
    strategies.begin_review(admin, ai_workflow["id"])
    strategies.approve(admin, ai_workflow["id"])
    response = _post(client, admin_token, ai_workflow["id"])
    assert response.status_code == 400
    assert response.json()["detail"] == "Only AI Proposed or Under Review workflows can be regenerated"


def test_api_workflow_without_portfolio_is_refused(client, admin_token, db_session, portfolio):  # noqa: F811
    """Old workflows (before the rework) may have no portfolio — refused before the AI is called."""
    old = PayflowStrategyModel(
        code="old-ai-workflow",
        name="Old AI workflow",
        client_id=portfolio["client_id"],
        portfolio_id=None,
        status="AI Proposed",
        origin="AI Proposed",
        source="AI Generated",
        steps=load_examples()[0]["steps"],
        entry_node_id="s1",
    )
    db_session.add(old)
    db_session.commit()
    count = db_session.query(PayflowStrategyModel).count()
    with patch.object(suggestion_service, "call_llm") as call:
        response = _post(client, admin_token, old.id)
    assert response.status_code == 400
    assert response.json()["detail"] == "This workflow has no portfolio, so it cannot be regenerated"
    call.assert_not_called()
    assert db_session.query(PayflowStrategyModel).count() == count


def test_api_needs_workflow_permission(client, supervisor_token, ai_workflow):  # noqa: F811
    with patch.object(suggestion_service, "call_llm") as call:
        response = _post(client, supervisor_token, ai_workflow["id"])
    assert response.status_code == 403
    call.assert_not_called()


def test_api_unknown_workflow(client, admin_token):
    assert _post(client, admin_token, 9999).status_code == 404


def test_api_ai_down(client, admin_token, ai_workflow):
    down = ServiceUnavailableError("AI service could not be reached")
    with patch.object(suggestion_service, "call_llm", side_effect=down):
        response = _post(client, admin_token, ai_workflow["id"])
    assert response.status_code == 502
    assert response.json()["detail"] == "AI service could not be reached"


def test_regenerate_saves_new_reasoning_with_changes_made(db_session, admin, ai_workflow):
    def call(messages, schema):
        workflow = copy.deepcopy(load_examples()[0])
        workflow.pop("for_segment")
        workflow["segment_id"] = "A"
        workflow["reasoning"]["changes_made"] = "Moved the SMS to day 8 as asked."
        return {"workflows": [workflow]}

    with patch.object(suggestion_service, "call_llm", side_effect=call):
        new = WorkflowSuggestionService(db_session).regenerate_workflow(admin, ai_workflow["id"], REMARKS)
    labels = [item["label"] for item in new["ai_context"]]
    assert context(new)["Changes made"] == "Moved the SMS to day 8 as asked."
    for label in ("Why it fits", "Channels", "Timing", "Escalation"):
        assert labels.count(label) == 1, label  # replaced, not copied from the original
