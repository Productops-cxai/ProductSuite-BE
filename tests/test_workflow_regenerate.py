import copy
import re
from unittest.mock import patch

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError, ServiceUnavailableError
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
from app.modules.payflow.services.ai_eval_export_service import AiEvalExportService
from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion import learning, message_writer
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from app.shared.enums import PayflowRoleCode, PlatformRole, UserStatus
from tests.test_workflow_suggestion_api import (  # noqa: F401 (fixtures)
    fake_llm,
    fake_llm_without_payment_check,
    portfolio,
    supervisor_token,
)

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


def versions_of(db_session, admin, workflow_id):
    return PayflowStrategyService(db_session).get_strategy(admin, workflow_id)["versions"]  # newest first


def test_regenerate_records_the_rejection_on_the_original(db_session, admin, ai_workflow):
    before = versions_of(db_session, admin, ai_workflow["id"])
    new, _ = regenerate(db_session, admin, ai_workflow["id"])

    after = versions_of(db_session, admin, ai_workflow["id"])
    assert len(after) == len(before) + 1
    row = after[0]
    assert row["status"] == "Rejected"
    assert row["version"] == ai_workflow["version"]
    assert re.fullmatch(r"\d{2} \w{3} \d{4}, \d{2}:\d{2} UTC", row["date"])  # date and time
    assert row["note"] == (
        f"Rejected / regeneration requested by {admin.full_name}. "
        f"New version: #{new['id']} (v{new['version']}). Remarks: {REMARKS}"
    )
    # Read-only snapshot of the rejected version.
    assert [step["id"] for step in row["steps"]] == [step["id"] for step in ai_workflow["steps"]]
    assert row["segment"] == ai_workflow["segment"]


def test_the_rejected_history_row_is_kept_out_of_ai_learning(db_session, admin, ai_workflow):
    """The remarks already reach the AI through the reject note and "Reviewer remarks" — not a third time."""
    new, _ = regenerate(db_session, admin, ai_workflow["id"])
    row = versions_of(db_session, admin, ai_workflow["id"])[0]
    assert row["status"] == "Rejected" and not row["reviewer_note"] and row["changes"] == []

    _, calls = regenerate(db_session, admin, new["id"])  # its review history walks back to the original
    assert "regeneration requested by" not in calls[0][1]["content"]
    lessons = learning._lessons(db_session, db_session.query(PayflowStrategyModel).all())
    assert not any("regeneration requested by" in lesson["line"] for lesson in lessons)


def test_ui_flow_reject_then_regenerate(db_session, admin, ai_workflow):
    """The frontend calls reject, then regenerate: both history rows are kept; reject sets the status."""
    strategies = PayflowStrategyService(db_session)
    strategies.reject(admin, ai_workflow["id"], note=REMARKS)
    new, calls = regenerate(db_session, admin, ai_workflow["id"])

    original = strategies.get_strategy(admin, ai_workflow["id"])
    assert original["status"] == "AI Proposed"
    regenerated, rejected = original["versions"][:2]
    assert regenerated["status"] == "Rejected" and f"#{new['id']}" in regenerated["note"]
    assert rejected["reviewer_note"] == REMARKS
    assert f"- Rejected. Reason: {REMARKS}" in calls[0][1]["content"]  # the reject note still reaches the AI


def test_new_version_and_rejected_row_are_saved_together(db_session, admin, ai_workflow):
    """If the history row cannot be saved, the new workflow is not saved either."""
    count = db_session.query(PayflowStrategyModel).count()
    history = len(versions_of(db_session, admin, ai_workflow["id"]))
    with patch.object(WorkflowSuggestionService, "_record_rejection", side_effect=RuntimeError("db down")):
        with pytest.raises(RuntimeError, match="db down"):
            regenerate(db_session, admin, ai_workflow["id"])
    assert db_session.query(PayflowStrategyModel).count() == count
    assert len(versions_of(db_session, admin, ai_workflow["id"])) == history


def test_reviewers_are_notified_about_the_new_version(db_session, admin, ai_workflow):
    with patch.object(PayflowStrategyService, "notify_awaiting") as notify:
        new, _ = regenerate(db_session, admin, ai_workflow["id"])
    notify.assert_called_once_with(admin, new["id"])


def test_ai_eval_export_counts_the_regenerated_workflow_as_rejected(db_session, admin, ai_workflow):
    def rejected():
        return AiEvalExportService(db_session).preview(admin)["performance"]["hr_rejected"]

    assert rejected() == 0
    regenerate(db_session, admin, ai_workflow["id"])
    assert rejected() == 1


def test_regenerate_continues_the_version_number(db_session, admin, ai_workflow):
    assert ai_workflow["version"] == 1  # Suggest still starts at v1
    second, _ = regenerate(db_session, admin, ai_workflow["id"])
    assert second["version"] == 2
    assert second["versions"][0]["version"] == 2 and second["versions"][0]["note"] == "Proposed by PayFlow AI"

    third, _ = regenerate(db_session, admin, second["id"])  # a chain keeps counting
    assert third["version"] == 3
    rejected = versions_of(db_session, admin, second["id"])[0]
    assert rejected["version"] == 2 and f"New version: #{third['id']} (v3)" in rejected["note"]


def test_approved_v2_is_regenerated_as_v3(db_session, admin, ai_workflow):
    strategies = PayflowStrategyService(db_session)
    strategies.begin_review(admin, ai_workflow["id"])
    assert strategies.approve(admin, ai_workflow["id"])["version"] == 2  # approve moves v1 → v2

    new, _ = regenerate(db_session, admin, ai_workflow["id"])
    assert new["version"] == 3
    rejected = versions_of(db_session, admin, ai_workflow["id"])[0]
    assert rejected["version"] == 2 and f"New version: #{new['id']} (v3)" in rejected["note"]


def test_the_new_version_is_reviewed_and_activated_as_usual(db_session, admin, ai_workflow):
    """Starting at v2 is only a number: approve and activate work as for any workflow."""
    strategies = PayflowStrategyService(db_session)
    new, _ = regenerate(db_session, admin, ai_workflow["id"])
    strategies.begin_review(admin, new["id"])
    approved = strategies.approve(admin, new["id"])
    assert approved["status"] == "Approved" and approved["version"] == new["version"] + 1
    assert strategies.activate(admin, new["id"])["status"] == "Active"


def test_manual_create_still_starts_at_v1(db_session, admin, portfolio):  # noqa: F811
    """The create API passes its body as the payload: a "version" in it is ignored."""
    human = PayflowStrategyService(db_session).create_strategy(
        admin,
        {
            "name": "Manual workflow",
            "client_id": portfolio["client_id"],
            "portfolio_id": portfolio["portfolio_id"],
            "steps": load_examples()[0]["steps"],
            "version": 9,
        },
    )
    assert human["version"] == 1 and human["versions"][0]["version"] == 1


def test_regenerate_retries_with_validator_errors(db_session, admin, ai_workflow):
    new, calls = regenerate(db_session, admin, ai_workflow["id"], broken_calls=1)
    assert new["status"] == "AI Proposed"
    assert len(calls) == 2
    retry_prompt = calls[1][1]["content"]
    assert REMARKS in retry_prompt and "purpose 'Nope' is not allowed" in retry_prompt


def test_regenerate_sends_back_a_workflow_that_keeps_reminding_after_a_payment(db_session, admin, ai_workflow):
    call, calls = fake_llm_without_payment_check()
    with patch.object(suggestion_service, "call_llm", side_effect=call):
        new = WorkflowSuggestionService(db_session).regenerate_workflow(admin, ai_workflow["id"], REMARKS)
    assert new["status"] == "AI Proposed"
    assert len(calls) == 2  # the original's segment (Active) is checked too
    assert 'without the check "Payment Status Not Equals Unpaid"' in calls[1][1]["content"]


def test_regenerate_asks_to_apply_each_remark_on_the_main_path(db_session, admin, ai_workflow):
    _, calls = regenerate(db_session, admin, ai_workflow["id"])
    prompt = calls[0][1]["content"]
    assert "Apply each remark on the main path that customers go through" in prompt
    assert "a fallback branch alone does not count" in prompt
    assert "Communication steps have no channel: PayFlow contacts each customer by email or SMS" in prompt
    assert "name each remark and what you changed for it. If a remark cannot be applied" in prompt
    assert f"Reviewer remarks: {REMARKS}" in prompt


def test_regenerate_writes_the_messages_with_the_same_remarks(db_session, admin, ai_workflow):
    from tests.test_workflow_message_api import fake_message_ai  # that module imports this one's fixtures

    call, _ = fake_llm()
    writer, prompts = fake_message_ai()
    with patch.object(suggestion_service, "call_llm", side_effect=call), \
            patch.object(message_writer, "call_llm", side_effect=writer):
        new = WorkflowSuggestionService(db_session).regenerate_workflow(admin, ai_workflow["id"], REMARKS)
    assert len(prompts) == 1 and f"ignore anything about the workflow's steps): {REMARKS}" in prompts[0]
    assert all(s["config"].get("body") for s in new["steps"] if s["kind"] == "Communication")


def test_regenerate_gives_up_and_saves_nothing(db_session, admin, ai_workflow):
    count = db_session.query(PayflowStrategyModel).count()
    history = len(versions_of(db_session, admin, ai_workflow["id"]))
    with pytest.raises(ServiceUnavailableError, match="AI could not produce valid workflows"):
        regenerate(db_session, admin, ai_workflow["id"], broken_calls=99)
    assert db_session.query(PayflowStrategyModel).count() == count
    assert len(versions_of(db_session, admin, ai_workflow["id"])) == history  # no "Rejected" row either


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
    assert "timing changed from 3 Days After Previous Action to 7 Days After Previous Action" in prompt
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


def test_approved_can_be_regenerated(db_session, admin, ai_workflow):
    strategies = PayflowStrategyService(db_session)
    strategies.begin_review(admin, ai_workflow["id"])
    strategies.approve(admin, ai_workflow["id"])
    new, _ = regenerate(db_session, admin, ai_workflow["id"])
    assert new["status"] == "AI Proposed"
    assert new["id"] != ai_workflow["id"]


def test_human_created_workflow_can_be_regenerated(db_session, admin, portfolio):  # noqa: F811
    """A hand-made Draft can be handed to the AI: the result is a new, linked AI proposal."""
    human = PayflowStrategyService(db_session).create_strategy(
        admin,
        {
            "name": "Manual workflow",
            "client_id": portfolio["client_id"],
            "portfolio_id": portfolio["portfolio_id"],
            "steps": load_examples()[0]["steps"],
        },
    )
    assert human["source"] == "Human Created" and human["status"] == "Draft"

    new, _ = regenerate(db_session, admin, human["id"])

    assert new["id"] != human["id"]
    assert new["status"] == "AI Proposed" and new["source"] == "AI Generated"
    assert new["version"] == human["version"] + 1
    assert context(new)["Revision of"].startswith(f"#{human['id']} ")
    # The hand-made original is kept as it was, with the rejection on its history.
    original = PayflowStrategyService(db_session).get_strategy(admin, human["id"])
    assert original["status"] == "Draft" and original["source"] == "Human Created"
    assert original["versions"][0]["status"] == "Rejected"


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


def test_api_approved_workflow_can_be_regenerated(client, admin_token, admin, db_session, ai_workflow):
    strategies = PayflowStrategyService(db_session)
    strategies.begin_review(admin, ai_workflow["id"])
    strategies.approve(admin, ai_workflow["id"])
    call, _ = fake_llm()
    with patch.object(suggestion_service, "call_llm", side_effect=call):
        response = _post(client, admin_token, ai_workflow["id"])
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == "AI Proposed"
    assert body["id"] != ai_workflow["id"]


def test_api_parent_level_workflow_is_regenerated(client, admin_token, db_session, portfolio):  # noqa: F811
    """A workflow without a portfolio is a client-level one (parent accounts): it regenerates
    like any other, and the new version stays at client level."""
    parent = PayflowStrategyModel(
        code="parent-ai-workflow",
        name="Parent AI workflow",
        client_id=portfolio["client_id"],
        portfolio_id=None,
        status="AI Proposed",
        origin="AI Proposed",
        source="AI Generated",
        steps=load_examples()[0]["steps"],
        entry_node_id="s1",
    )
    db_session.add(parent)
    db_session.commit()
    call, calls = fake_llm()
    with patch.object(suggestion_service, "call_llm", side_effect=call):
        response = _post(client, admin_token, parent.id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] != parent.id
    assert body["portfolio_id"] is None
    assert body["client_id"] == portfolio["client_id"]
    assert len(calls) == 1


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
