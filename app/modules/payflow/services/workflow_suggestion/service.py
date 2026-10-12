"""Workflow suggestion service — the entry point the routes call."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError, ValidationAppError
from app.infrastructure.database.models import UserModel
from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion.context import (
    load_client_context,
    load_portfolio_accounts,
    load_rules,
)
from app.modules.payflow.services.workflow_suggestion.llm import build_output_schema, call_llm
from app.modules.payflow.services.workflow_suggestion.prompt import build_messages
from app.modules.payflow.services.workflow_suggestion.rules import design_limits, governing_rules
from app.modules.payflow.services.workflow_suggestion.segmenter import build_segments
from app.modules.payflow.services.workflow_suggestion.validator import validate_all
from app.shared.enums import PayflowStrategySource

# After the first AI answer, failing workflows are sent back to the AI this many times.
MAX_FIX_ATTEMPTS = 2


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

        # Every workflow is validated before anything is saved.
        workflows: dict[str, dict] = {}
        for batch in _batches(segments):
            workflows.update(self._suggest_batch(client, portfolio.name, batch, limits))

        governed_by = governing_rules(client, rules)
        saved = [
            self._save(user, client, portfolio.id, segment, workflows[segment["segment_id"]], governed_by)
            for segment in segments
        ]
        return {
            "client_id": client_id,
            "portfolio_id": portfolio.id,
            "portfolio_name": portfolio.name,
            "workflows": saved,
        }

    def _suggest_batch(
        self,
        client: dict[str, Any],
        portfolio_name: str,
        batch: list[dict[str, Any]],
        limits: dict[str, Any],
    ) -> dict[str, dict]:
        """One valid workflow per segment, retrying only the ones that fail validation."""
        accepted: dict[str, dict] = {}
        pending: list[dict[str, Any]] = batch
        feedback: list[dict[str, Any]] = []
        for _ in range(1 + MAX_FIX_ATTEMPTS):
            messages = build_messages(client, portfolio_name, pending, limits, feedback)
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
        client: dict[str, Any],
        portfolio_id: int,
        segment: dict[str, Any],
        workflow: dict[str, Any],
        governed_by: list[str],
    ) -> dict[str, Any]:
        # Existing create path: AI Generated → status/origin "AI Proposed" + AI snapshot.
        return self.strategies.create_strategy(
            user,
            {
                "name": workflow["name"],
                "client_id": client["id"],
                "portfolio_id": portfolio_id,
                "summary": workflow.get("summary"),
                "segment": segment["segment"],
                "cases_covered": segment["stats"]["accounts"],
                "entry_node_id": workflow["entry_node_id"],
                "steps": workflow["steps"],
                "source": PayflowStrategySource.AI_GENERATED.value,
                "ai_context": _ai_context(client, segment, workflow, governed_by),
            },
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
        {"label": "AI rationale", "value": workflow.get("rationale") or "—"},
    ]
    return context + [{"label": "Governed by", "value": line} for line in governed_by]
