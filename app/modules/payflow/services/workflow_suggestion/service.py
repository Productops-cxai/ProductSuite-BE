"""Workflow suggestion service — the entry point the routes call."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError, ValidationAppError
from app.infrastructure.database.models import PayflowStrategyModel, UserModel
from app.modules.payflow.services.strategy_service import (
    PayflowStrategyService,
    read_strategy_versions,
)
from app.modules.payflow.services.workflow_suggestion.config import REASONING_LABELS
from app.modules.payflow.services.workflow_suggestion.context import (
    load_client_context,
    load_portfolio_accounts,
    load_rules,
)
from app.modules.payflow.services.workflow_suggestion.learning import (
    learned_from,
    load_client_learning,
    pick_examples,
    pick_lessons,
)
from app.modules.payflow.services.workflow_suggestion.llm import build_output_schema, call_llm
from app.modules.payflow.services.workflow_suggestion.prompt import build_messages, build_revision
from app.modules.payflow.services.workflow_suggestion.rules import design_limits, governing_rules
from app.modules.payflow.services.workflow_suggestion.segmenter import build_segments
from app.modules.payflow.services.workflow_suggestion.validator import validate_all
from app.shared.enums import PayflowStrategySource, PayflowStrategyStatus

# After the first AI answer, failing workflows are sent back to the AI this many times.
MAX_FIX_ATTEMPTS = 2

# Approved / Active / Inactive workflows are never regenerated.
REGENERATE_STATUSES = {PayflowStrategyStatus.AI_PROPOSED.value, PayflowStrategyStatus.UNDER_REVIEW.value}
# ai_context lines rewritten on every revision (the rest is kept from the original).
_REWRITTEN_CONTEXT = {
    "AI rationale",  # older workflows (before the structured reasoning)
    *REASONING_LABELS.values(),
    "Revision of",
    "Reviewer remarks",
    "Governed by",
    "Learned from",  # Suggest only — not a segment stat
}


class WorkflowSuggestionService:
    def __init__(self, db: Session):
        self.db = db
        # Reuse workflow access rules so suggestions follow the same permissions.
        self.strategies = PayflowStrategyService(db)

    def preview_segments(self, user: UserModel, client_id: int, portfolio_id: int) -> dict[str, Any]:
        self.strategies._assert_view(user, client_id)
        portfolio, accounts = load_portfolio_accounts(self.db, client_id, portfolio_id)
        return {
            "client_id": client_id,
            "portfolio_id": portfolio.id,
            "portfolio_name": portfolio.name,
            **build_segments(accounts),
        }

    def suggest_workflows(self, user: UserModel, client_id: int, portfolio_id: int) -> dict[str, Any]:
        self.strategies._assert_manage(user, client_id)
        client = load_client_context(self.db, client_id)
        portfolio, accounts = load_portfolio_accounts(self.db, client_id, portfolio_id)
        rules = load_rules(self.db, client_id)
        segments = build_segments(accounts)["segments"]
        if not segments:
            raise ValidationAppError("This portfolio has no accounts that need a workflow")
        limits = design_limits(client, rules)
        # This client's past reviews: example workflows + lessons (same client only).
        learning = load_client_learning(self.db, client_id, limits)

        # Every workflow is validated before anything is saved.
        workflows: dict[str, dict] = {}
        learned: dict[str, list[dict[str, str]]] = {}  # segment_id → "Learned from" ai_context line
        for batch in _batches(segments):
            # Chosen per AI call: what is "similar" depends on the segments in this call.
            examples = pick_examples(learning["pool"], batch, portfolio.id)
            lessons = pick_lessons(learning["lessons"], batch)
            workflows.update(
                self._suggest_batch(
                    client,
                    portfolio.name,
                    batch,
                    limits,
                    client_examples=[item["example"] for item in examples],
                    lessons=lessons,
                )
            )
            for segment in batch:
                learned[segment["segment_id"]] = learned_from(examples, lessons)

        governed_by = governing_rules(client, rules)
        # Save all workflows together: if one fails, none are saved.
        saved = []
        try:
            for segment in segments:
                workflow = workflows[segment["segment_id"]]
                saved.append(
                    self._save(
                        user,
                        client["id"],
                        portfolio.id,
                        workflow,
                        segment=segment["segment"],
                        cases_covered=segment["stats"]["accounts"],
                        ai_context=_ai_context(client, segment, workflow, governed_by) + learned[segment["segment_id"]],
                        commit=False,
                    )
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        # Notifications commit on their own, so they are sent only after the save.
        for workflow in saved:
            self.strategies.notify_awaiting(user, workflow["id"])
        return {
            "client_id": client_id,
            "portfolio_id": portfolio.id,
            "portfolio_name": portfolio.name,
            "workflows": saved,
        }

    def regenerate_workflow(self, user: UserModel, strategy_id: int, remarks: str) -> dict[str, Any]:
        """New "AI Proposed" version of an AI workflow, guided by reviewer remarks.

        The original workflow is not changed; the new one records revision_of_id
        (plus a "Revision of #id" line for reviewers).
        """
        original = self.strategies.get_strategy(user, strategy_id)
        if original["source"] != PayflowStrategySource.AI_GENERATED.value:
            raise ValidationAppError("Only AI-generated workflows can be regenerated")
        if original["status"] not in REGENERATE_STATUSES:
            raise ValidationAppError("Only AI Proposed or Under Review workflows can be regenerated")
        # Old workflows (before the rework) may have no portfolio; saving would fail after the AI call.
        if original["portfolio_id"] is None:
            raise ValidationAppError("This workflow has no portfolio, so it cannot be regenerated")
        self.strategies._assert_manage(user, original["client_id"])
        client = load_client_context(self.db, original["client_id"])
        rules = load_rules(self.db, original["client_id"])
        limits = design_limits(client, rules)

        # The workflow already stores its segment and stats — reuse them as the AI's context.
        kept = [item for item in original["ai_context"] if item["label"] not in _REWRITTEN_CONTEXT]
        segment = {
            "segment_id": "A",
            "label": next((i["value"] for i in kept if i["label"] == "Segment"), "Current segment"),
            "stats": {i["label"]: i["value"] for i in kept if i["label"] != "Segment"},
        }
        revision = build_revision(segment["segment_id"], original, remarks, self._review_history(original))
        workflow = self._suggest_batch(client, original["portfolio_name"] or "", [segment], limits, revision)["A"]

        ai_context = kept + _reasoning_context(workflow) + [
            {"label": "Revision of", "value": f"#{original['id']} {original['name']} (v{original['version']})"},
            {"label": "Reviewer remarks", "value": remarks},
        ]
        ai_context += [{"label": "Governed by", "value": line} for line in governing_rules(client, rules)]
        return self._save(
            user,
            original["client_id"],
            original["portfolio_id"],
            workflow,
            segment=original["segment"],
            cases_covered=original["cases_covered"],
            ai_context=ai_context,
            revision_of_id=original["id"],
        )

    def _review_history(self, workflow: dict[str, Any]) -> list[dict[str, Any]]:
        """History entries (newest first) of this workflow and of the ones it was regenerated from."""
        versions = list(workflow.get("versions") or [])
        seen = {workflow["id"]}
        parent_id = workflow.get("revision_of_id")
        while parent_id and parent_id not in seen:
            parent = self.db.get(PayflowStrategyModel, parent_id)
            # Parent deleted — or another client's: never mix clients' feedback.
            if parent is None or parent.client_id != workflow["client_id"]:
                break
            seen.add(parent.id)
            versions += read_strategy_versions(self.db, parent)
            parent_id = parent.revision_of_id
        return versions

    def _suggest_batch(
        self,
        client: dict[str, Any],
        portfolio_name: str,
        batch: list[dict[str, Any]],
        limits: dict[str, Any],
        revision: str | None = None,
        client_examples: list[dict[str, Any]] | None = None,
        lessons: list[str] | None = None,
    ) -> dict[str, dict]:
        """One valid workflow per segment, retrying only the ones that fail validation."""
        accepted: dict[str, dict] = {}
        pending: list[dict[str, Any]] = batch
        feedback: list[dict[str, Any]] = []
        for _ in range(1 + MAX_FIX_ATTEMPTS):
            messages = build_messages(
                client, portfolio_name, pending, limits, feedback, revision, client_examples, lessons
            )
            schema = build_output_schema([s["segment_id"] for s in pending], limits["channels"])
            reply = _workflows_by_segment(call_llm(messages, schema))

            feedback = []
            for segment in pending:
                workflow = reply.get(segment["segment_id"])
                errors = (
                    validate_all([workflow], [segment], limits)
                    if workflow
                    else [f"Segment {segment['segment_id']}: no workflow was suggested"]
                )
                if errors:
                    feedback.append({"segment": segment, "workflow": workflow, "errors": errors})
                else:
                    accepted[segment["segment_id"]] = workflow
            if not feedback:
                return accepted
            pending = [item["segment"] for item in feedback]

        problems = [error for item in feedback for error in item["errors"]]
        raise ServiceUnavailableError("AI could not produce valid workflows: " + "; ".join(problems[:10]))

    def _save(
        self,
        user: UserModel,
        client_id: int,
        portfolio_id: int,
        workflow: dict[str, Any],
        *,
        segment: dict[str, Any],
        cases_covered: int | None,
        ai_context: list[dict[str, str]],
        commit: bool = True,
        revision_of_id: int | None = None,
    ) -> dict[str, Any]:
        # Existing create path: AI Generated → status/origin "AI Proposed" + AI snapshot.
        return self.strategies.create_strategy(
            user,
            {
                "name": workflow["name"],
                "client_id": client_id,
                "portfolio_id": portfolio_id,
                "summary": workflow.get("summary"),
                "segment": segment,
                "cases_covered": cases_covered,
                "entry_node_id": workflow["entry_node_id"],
                "steps": workflow["steps"],
                "source": PayflowStrategySource.AI_GENERATED.value,
                "ai_context": ai_context,
                "revision_of_id": revision_of_id,
            },
            commit=commit,
        )


def _batches(segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """FREE-TIER BATCHING — a few segments per AI call to stay under Groq's free token limit.

    On a paid tier set GROQ_SEGMENTS_PER_CALL=0 (all segments in one call),
    or replace this function with `return [segments]`.
    """
    size = settings.GROQ_SEGMENTS_PER_CALL or len(segments)
    return [segments[start : start + size] for start in range(0, len(segments), size)]


def _workflows_by_segment(reply: Any) -> dict[str, dict]:
    items = reply.get("workflows") if isinstance(reply, dict) else None
    if not isinstance(items, list):
        return {}
    return {w["segment_id"]: w for w in items if isinstance(w, dict) and w.get("segment_id")}


def _ai_context(
    client: dict[str, Any],
    segment: dict[str, Any],
    workflow: dict[str, Any],
    governed_by: list[str],
) -> list[dict[str, str]]:
    stats = segment["stats"]
    dpd = stats["avg_days_past_due"]
    context = [
        {"label": "Segment", "value": segment["label"]},
        {"label": "Accounts", "value": str(stats["accounts"])},
        {"label": "Avg outstanding", "value": f"{stats['avg_outstanding']:,.2f} {client.get('currency') or 'CAD'}"},
        {"label": "Avg days past due", "value": "Unknown" if dpd is None else str(dpd)},
        {"label": "Email consent", "value": f"{stats['email_consent_pct']}%"},
    ]
    return context + _reasoning_context(workflow) + [{"label": "Governed by", "value": line} for line in governed_by]


def _reasoning_context(workflow: dict[str, Any]) -> list[dict[str, str]]:
    """The AI's reasoning as ai_context lines ("Why it fits", "Channels", …); empty fields are skipped."""
    reasoning = workflow.get("reasoning") if isinstance(workflow.get("reasoning"), dict) else {}
    return [
        {"label": label, "value": str(reasoning[field]).strip()}
        for field, label in REASONING_LABELS.items()
        if str(reasoning.get(field) or "").strip()
    ]
