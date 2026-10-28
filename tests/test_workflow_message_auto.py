"""Suggest and Regenerate write each workflow's email / SMS text before saving it."""

from unittest.mock import patch

from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion import message_writer
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.prompt import build_revision, compact_step
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from tests.test_workflow_message_api import fake_message_ai
from tests.test_workflow_regenerate import REMARKS, admin, ai_workflow, portfolio  # noqa: F401 (fixtures)
from tests.test_workflow_suggestion_api import fake_llm


def suggest(db_session, user, portfolio, fail=False):  # noqa: F811
    design, _ = fake_llm()
    write, prompts = fake_message_ai(fail)
    with (
        patch.object(suggestion_service, "call_llm", side_effect=design),
        patch.object(message_writer, "call_llm", side_effect=write),
    ):
        result = WorkflowSuggestionService(db_session).suggest_workflows(
            user, portfolio["client_id"], portfolio["portfolio_id"]
        )
    return result["workflows"], prompts


def regenerate(db_session, user, workflow_id, fail=False):
    design, _ = fake_llm()
    write, prompts = fake_message_ai(fail)
    with (
        patch.object(suggestion_service, "call_llm", side_effect=design),
        patch.object(message_writer, "call_llm", side_effect=write),
    ):
        return WorkflowSuggestionService(db_session).regenerate_workflow(user, workflow_id, REMARKS), prompts


def messages(workflow):
    return {step["id"]: step["config"] for step in workflow["steps"] if step["kind"] == "Communication"}


def context(workflow):
    return {item["label"]: item["value"] for item in workflow["ai_context"]}


def test_suggest_writes_every_workflows_messages(db_session, admin, portfolio):  # noqa: F811
    workflows, prompts = suggest(db_session, admin, portfolio)
    assert len(prompts) == len(workflows)  # one writer call per workflow
    for workflow in workflows:
        assert messages(workflow) and all(cfg.get("body") and cfg.get("tone") for cfg in messages(workflow).values())
        assert "Messages" not in context(workflow)
        assert workflow["status"] == "AI Proposed" and workflow["origin"] == "AI Proposed"
        snapshot = [s for s in workflow["ai_proposal_snapshot"]["steps"] if s["kind"] == "Communication"]
        assert all(step["config"].get("body") for step in snapshot)  # the AI proposal includes its text
        # Saved once, with the text: no extra "Message text written by AI" history entry.
        history = PayflowStrategyService(db_session).get_strategy(admin, workflow["id"])["versions"]
        assert len(history) == 1 and not history[0]["note"].startswith("Message text written")


def test_writer_gets_the_segments_own_numbers(db_session, admin, portfolio):  # noqa: F811
    workflows, prompts = suggest(db_session, admin, portfolio)
    assert f"Segment: {context(workflows[0])['Segment']}" in prompts[0]
    assert '"avg_days_past_due"' in prompts[0] and '"with_email_pct"' in prompts[0]  # segmenter stats


def test_suggest_still_saves_when_messages_cannot_be_written(db_session, admin, portfolio):  # noqa: F811
    workflows, prompts = suggest(db_session, admin, portfolio, fail=True)
    assert len(prompts) == 3 * len(workflows)  # first try + 2 fixes each
    for workflow in workflows:
        assert workflow["status"] == "AI Proposed"
        assert not any(cfg.get("body") for cfg in messages(workflow).values())  # templates are sent
        assert context(workflow)["Messages"].startswith("Not written, so the templates will be sent")


def test_regenerate_writes_messages_for_the_new_version(db_session, admin, ai_workflow):  # noqa: F811
    assert "Messages" in context(ai_workflow)  # made while the message AI was off
    new, prompts = regenerate(db_session, admin, ai_workflow["id"])
    assert len(prompts) == 1
    assert all(cfg.get("body") and cfg.get("tone") for cfg in messages(new).values())
    assert "Messages" not in context(new)  # the old note is not copied over
    assert f"Segment: {context(ai_workflow)['Segment']}" in prompts[0]

    original = PayflowStrategyService(db_session).get_strategy(admin, ai_workflow["id"])
    assert not any(cfg.get("body") for cfg in messages(original).values())  # original untouched


def test_regenerate_still_saves_when_messages_cannot_be_written(db_session, admin, ai_workflow):  # noqa: F811
    new, _ = regenerate(db_session, admin, ai_workflow["id"], fail=True)
    assert new["status"] == "AI Proposed" and new["revision_of_id"] == ai_workflow["id"]
    assert context(new)["Messages"].startswith("Not written")


def test_message_text_stays_out_of_workflow_design_prompts():
    """Client examples and Regenerate show workflows to the design AI — without their message text."""
    step = {
        "id": "s2",
        "kind": "Communication",
        "title": "Friendly email",
        "config": {"channel": "Email", "purpose": "Payment Reminder", "subject": "Hello", "body": "LONG TEXT", "tone": "Friendly"},
    }
    assert compact_step(step)["config"] == {"channel": "Email", "purpose": "Payment Reminder"}
    revision = build_revision("A", {"name": "W", "entry_node_id": "s2", "steps": [step]}, "remarks")
    assert "LONG TEXT" not in revision and "Friendly email" in revision
