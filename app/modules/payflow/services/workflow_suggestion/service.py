"""Workflow suggestion service — the entry point the routes call."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ServiceUnavailableError, ValidationAppError
from app.infrastructure.database.models import PayflowStrategyModel, UserModel
from app.modules.payflow.services.strategy_service import (
    PayflowStrategyService,
    read_strategy_versions,
)
from app.modules.payflow.services.workflow_suggestion.config import REASONING_LABELS
from app.modules.payflow.services.workflow_suggestion.context import (
    load_client_context,
    load_payment_options,
    load_rules,
    load_suggest_scope,
)
from app.modules.payflow.services.workflow_suggestion.learning import (
    learned_from,
    load_client_learning,
    pick_examples,
    pick_lessons,
)
from app.modules.payflow.services.workflow_suggestion.llm import build_output_schema, call_llm
from app.modules.payflow.services.workflow_suggestion.message_writer import apply_messages, write_messages
from app.modules.payflow.services.workflow_suggestion.prompt import (
    MAX_REGENERATE_CLIENT_EXAMPLES,
    MAX_REGENERATE_LESSONS,
    build_messages,
    build_revision,
)
from app.modules.payflow.services.workflow_suggestion.rules import design_limits, governing_rules
from app.modules.payflow.services.workflow_suggestion.segmenter import build_segments
from app.modules.payflow.services.workflow_suggestion.validator import validate_all
from app.shared.enums import PayflowStrategySource, PayflowStrategyStatus

# After the first AI answer, failing workflows are sent back to the AI this many times.
MAX_FIX_ATTEMPTS = 2

# Reject first, then regenerate: Active → Under Review (same id + history);
# other statuses → AI Proposed / Draft. Draft included so Human Created can regenerate.
REGENERATE_STATUSES = {
    PayflowStrategyStatus.DRAFT.value,
    PayflowStrategyStatus.AI_PROPOSED.value,
    PayflowStrategyStatus.UNDER_REVIEW.value,
    PayflowStrategyStatus.APPROVED.value,
}
# Message text is written before approval only — approved text must not change without a review.
WRITE_MESSAGES_STATUSES = {
    PayflowStrategyStatus.DRAFT.value,
    PayflowStrategyStatus.AI_PROPOSED.value,
    PayflowStrategyStatus.UNDER_REVIEW.value,
}
# ai_context lines rewritten on every revision (the rest is kept from the original).
_REWRITTEN_CONTEXT = {
    "AI rationale",  # older workflows (before the structured reasoning)
    *REASONING_LABELS.values(),
    "Revision of",
    "Reviewer remarks",
    "Governed by",
    "Learned from",  # Suggest only — not a segment stat
    "Messages",  # set only when the message text could not be written
}


class WorkflowSuggestionService:
    def __init__(self, db: Session):
        self.db = db
        # Reuse workflow access rules so suggestions follow the same permissions.
        self.strategies = PayflowStrategyService(db)

    def preview_segments(
        self, user: UserModel, client_id: int, portfolio_id: int | None
    ) -> dict[str, Any]:
        self.strategies._assert_view(user, client_id)
        scope, accounts = load_suggest_scope(self.db, client_id, portfolio_id)
        return {
            "client_id": client_id,
            "portfolio_id": scope.id,
            "portfolio_name": scope.name,
            **build_segments(accounts),
        }

    def suggest_workflows(
        self, user: UserModel, client_id: int, portfolio_id: int | None
    ) -> dict[str, Any]:
        self.strategies._assert_manage(user, client_id)
        client = load_client_context(self.db, client_id)
        scope, accounts = load_suggest_scope(self.db, client_id, portfolio_id)
        rules = load_rules(self.db, client_id)
        segments = build_segments(accounts)["segments"]
        if not segments:
            raise ValidationAppError(
                "No accounts that need a workflow"
                if portfolio_id is None
                else "This portfolio has no accounts that need a workflow"
            )
        limits = design_limits(client, rules)
        # This client's past reviews: example workflows + lessons (same client only).
        learning = load_client_learning(self.db, client_id, limits)

        # Every workflow is validated before anything is saved.
        workflows: dict[str, dict] = {}
        learned: dict[str, list[dict[str, str]]] = {}  # segment_id → "Learned from" ai_context line
        for batch in _batches(segments):
            # Chosen per AI call: what is "similar" depends on the segments in this call.
            examples = pick_examples(learning["pool"], batch, scope.id)
            lessons = pick_lessons(learning["lessons"], batch)
            workflows.update(
                self._suggest_batch(
                    client,
                    scope.name,
                    batch,
                    limits,
                    client_examples=[item["example"] for item in examples],
                    lessons=lessons,
                )
            )
            for segment in batch:
                learned[segment["segment_id"]] = learned_from(examples, lessons)

        # Email / SMS text for every workflow (one AI call each), written before saving.
        writer_client = {**client, "payment_options": load_payment_options(self.db, client["id"])}
        messages_note: dict[str, list[dict[str, str]]] = {}  # segment_id → "Messages" line when not written
        for segment in segments:
            segment_id = segment["segment_id"]
            workflows[segment_id], messages_note[segment_id] = _with_messages(
                writer_client, workflows[segment_id], segment
            )

        governed_by = governing_rules(client, rules)
        # Save all workflows together: if one fails, none are saved.
        saved = []
        try:
            for segment in segments:
                segment_id = segment["segment_id"]
                workflow = workflows[segment_id]
                saved.append(
                    self._save(
                        user,
                        client["id"],
                        scope.id,
                        workflow,
                        segment=segment["segment"],
                        cases_covered=segment["stats"]["accounts"],
                        ai_context=_ai_context(client, segment, workflow, governed_by)
                        + learned[segment_id]
                        + messages_note[segment_id],
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
            "portfolio_id": scope.id,
            "portfolio_name": scope.name,
            "workflows": saved,
        }

    def regenerate_workflow(self, user: UserModel, strategy_id: int, remarks: str) -> dict[str, Any]:
        """New "AI Proposed" version of a workflow, guided by reviewer remarks.

        Works for AI Generated and Human Created sources. The original workflow is
        not replaced; the new one records revision_of_id (plus a "Revision of #id"
        line for reviewers).
        """
        original = self.strategies.get_strategy(user, strategy_id)
        if original["source"] not in {
            PayflowStrategySource.AI_GENERATED.value,
            PayflowStrategySource.HUMAN_CREATED.value,
        }:
            raise ValidationAppError("Only AI-generated or human-created workflows can be regenerated")
        if original["status"] not in REGENERATE_STATUSES:
            raise ValidationAppError(
                "Only Draft, AI Proposed, Under Review or Approved workflows can be regenerated"
            )
        self.strategies._assert_manage(user, original["client_id"])
        client = load_client_context(self.db, original["client_id"])
        rules = load_rules(self.db, original["client_id"])
        limits = design_limits(client, rules)
        learning = load_client_learning(self.db, original["client_id"], limits)
        scope_name = original.get("portfolio_name") or (
            f"{client.get('name') or 'Client'} (parent)"
            if original.get("portfolio_id") is None
            else ""
        )

        # AI workflows store segment/stats in ai_context; human-created may have none.
        kept = [
            item
            for item in (original.get("ai_context") or [])
            if item.get("label") not in _REWRITTEN_CONTEXT
        ]
        segment = {
            "segment_id": "A",
            "label": next((i["value"] for i in kept if i["label"] == "Segment"), "Current segment"),
            "stats": {i["label"]: i["value"] for i in kept if i["label"] != "Segment"},
            "segment": original.get("segment") or {},
        }
        examples = pick_examples(
            learning["pool"], [segment], original["portfolio_id"]
        )
        lessons = pick_lessons(learning["lessons"], [segment])
        revision = build_revision(segment["segment_id"], original, remarks, self._review_history(original))
        # Cap learning payload on regenerate — full examples + revision exceed Groq free TPM.
        workflow = self._suggest_batch(
            client,
            scope_name,
            [segment],
            limits,
            revision,
            client_examples=[
                item["example"] for item in examples[:MAX_REGENERATE_CLIENT_EXAMPLES]
            ],
            lessons=lessons[:MAX_REGENERATE_LESSONS],
        )["A"]
        # Keep the family name + continue version — do not create "… Revised" v1 clutter.
        family_name = _family_workflow_name(original.get("name") or workflow.get("name") or "Workflow")
        workflow = {**workflow, "name": family_name}
        next_version = int(original.get("version") or 1) + 1
        # Prior open AI Proposed revisions / same-name proposals are superseded.
        self._retire_prior_revisions(
            parent_id=original["id"],
            client_id=original["client_id"],
            portfolio_id=original.get("portfolio_id"),
            family_name=family_name,
        )
        # Skip a second Groq call for message bodies on regenerate — keeps the request
        # under reverse-proxy timeouts (browser otherwise reports a false CORS error).
        # Reviewers use Write messages on the new AI Proposed workflow before approval.
        messages_note = [
            {
                "label": "Messages",
                "value": "Not written on regenerate. Use Write messages before approval.",
            }
        ]

        ai_context = kept + _reasoning_context(workflow) + [
            {"label": "Revision of", "value": f"#{original['id']} {original['name']} (v{original['version']})"},
            {"label": "Reviewer remarks", "value": remarks},
        ]
        if examples:
            ai_context.append(
                {
                    "label": "Learned from",
                    "value": ", ".join(
                        str(item.get("example", {}).get("name") or item.get("name") or "prior")
                        for item in examples[:3]
                    ),
                }
            )
        ai_context += [{"label": "Governed by", "value": line} for line in governing_rules(client, rules)]
        ai_context += messages_note
        return self._save(
            user,
            original["client_id"],
            original["portfolio_id"],
            workflow,
            segment=original["segment"],
            cases_covered=original["cases_covered"],
            ai_context=ai_context,
            revision_of_id=original["id"],
            version=next_version,
        )

    def _retire_prior_revisions(
        self,
        *,
        parent_id: int,
        client_id: int,
        portfolio_id: int | None,
        family_name: str,
    ) -> None:
        """Mark earlier AI Proposed regenerations / same-name proposals Inactive (superseded)."""
        q = self.db.query(PayflowStrategyModel).filter(
            PayflowStrategyModel.client_id == client_id,
            PayflowStrategyModel.status == PayflowStrategyStatus.AI_PROPOSED.value,
            PayflowStrategyModel.id != parent_id,
        )
        if portfolio_id is None:
            q = q.filter(PayflowStrategyModel.portfolio_id.is_(None))
        else:
            q = q.filter(PayflowStrategyModel.portfolio_id == portfolio_id)
        prior = [
            row
            for row in q.all()
            if row.revision_of_id == parent_id
            or _family_workflow_name(row.name or "") == family_name
        ]
        stamp = datetime.now(timezone.utc).strftime("%d %b %Y")
        for row in prior:
            row.status = PayflowStrategyStatus.INACTIVE.value
            self.strategies._prepend_version(
                row,
                {
                    "version": int(row.version or 1),
                    "date": stamp,
                    "note": "Superseded by a newer regeneration",
                    "changes": [],
                    "status": row.status,
                    "origin": row.origin,
                },
            )
        if prior:
            self.db.flush()

    def reject_and_queue_regenerate(
        self, user: UserModel, strategy_id: int, remarks: str
    ) -> dict[str, Any]:
        """Reject now (history on same workflow); caller schedules regenerate in background.

        Returns the rejected workflow immediately so nginx/proxies do not 504 on Groq latency.
        Active → Under Review; other statuses follow reject rules.
        """
        rejected = self.strategies.reject(user, strategy_id, note=remarks)
        rejected["regeneration_pending"] = True
        return rejected

    def latest_revision(
        self, user: UserModel, strategy_id: int
    ) -> dict[str, Any] | None:
        """Newest workflow regenerated from strategy_id, or None if not ready yet."""
        parent = self.strategies.get_strategy(user, strategy_id)
        self.strategies._assert_manage(user, parent["client_id"])
        child = (
            self.db.query(PayflowStrategyModel)
            .filter(PayflowStrategyModel.revision_of_id == strategy_id)
            .order_by(PayflowStrategyModel.id.desc())
            .first()
        )
        if not child:
            return None
        return self.strategies._serialize(self.strategies._get_row(child.id))

    def write_workflow_messages(
        self, user: UserModel, strategy_id: int, step_id: str | None = None, remarks: str | None = None
    ) -> dict[str, Any]:
        """AI writes the email / SMS text of a workflow — every message, or only `step_id`."""
        workflow = self.strategies.get_strategy(user, strategy_id)
        if workflow["status"] not in WRITE_MESSAGES_STATUSES:
            raise ValidationAppError(
                "Message text can only be written for Draft, AI Proposed or Under Review workflows"
            )
        self.strategies._assert_manage(user, workflow["client_id"])
        client = {
            **load_client_context(self.db, workflow["client_id"]),
            "payment_options": load_payment_options(self.db, workflow["client_id"]),
        }
        written = write_messages(
            client,
            workflow,
            _stored_segment(workflow),
            step_ids=[step_id] if step_id else None,
            remarks=remarks,
        )
        return self.strategies.save_ai_messages(
            user, strategy_id, apply_messages(workflow["steps"], written), remarks=remarks
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
        portfolio_id: int | None,
        workflow: dict[str, Any],
        *,
        segment: dict[str, Any],
        cases_covered: int | None,
        ai_context: list[dict[str, str]],
        commit: bool = True,
        revision_of_id: int | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        # Existing create path: AI Generated → status/origin "AI Proposed" + AI snapshot.
        # portfolio_id=None → client-level workflow for parent accounts.
        payload: dict[str, Any] = {
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
        }
        if version is not None:
            payload["version"] = version
        return self.strategies.create_strategy(user, payload, commit=commit)


def _family_workflow_name(name: str) -> str:
    """Strip AI 'Revised' suffixes so regenerations stay in the same named family."""
    cleaned = (name or "").strip()
    while True:
        lower = cleaned.lower()
        if lower.endswith(" revised"):
            cleaned = cleaned[: -len(" revised")].rstrip(" -–—")
            continue
        if lower.endswith(" (revised)"):
            cleaned = cleaned[: -len(" (revised)")].rstrip(" -–—")
            continue
        break
    return cleaned.strip() or "Workflow"


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
        {"label": "Email allowed", "value": f"{stats['email_allowed_pct']}%"},
    ]
    return context + _reasoning_context(workflow) + [{"label": "Governed by", "value": line} for line in governed_by]


def _with_messages(
    client: dict[str, Any], workflow: dict[str, Any], segment: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The workflow with its email / SMS text written in.

    Writing never blocks a save: if it fails, the workflow is kept as it is (its templates
    are sent) and a "Messages" ai_context line tells reviewers to write them later.
    """
    if not any(step.get("kind") == "Communication" for step in workflow.get("steps") or []):
        return workflow, []
    try:
        written = write_messages(client, workflow, segment)
    except AppError as exc:
        note = f"Not written, so the templates will be sent. Use Write messages to try again. ({exc.message})"
        return workflow, [{"label": "Messages", "value": note}]
    return {**workflow, "steps": apply_messages(workflow["steps"], written)}, []


def _stored_segment(workflow: dict[str, Any]) -> dict[str, Any]:
    """A saved workflow's segment in the segmenter's shape; stats come from its ai_context (none if hand-made)."""
    context = {
        item["label"]: item["value"]
        for item in workflow.get("ai_context") or []
        if isinstance(item, dict) and item.get("label")
    }
    days = str(context.get("Avg days past due") or "")
    stats = {
        "accounts": context.get("Accounts"),
        "avg_outstanding": context.get("Avg outstanding"),
        "avg_days_past_due": int(days) if days.isdigit() else None,
        "email_consent": context.get("Email consent"),
    }
    return {
        "label": context.get("Segment") or "Custom segment",
        "segment": workflow.get("segment") or {},
        "stats": {key: value for key, value in stats.items() if value is not None},
    }


def _reasoning_context(workflow: dict[str, Any]) -> list[dict[str, str]]:
    """The AI's reasoning as ai_context lines ("Why it fits", "Channels", …); empty fields are skipped."""
    reasoning = workflow.get("reasoning") if isinstance(workflow.get("reasoning"), dict) else {}
    return [
        {"label": label, "value": str(reasoning[field]).strip()}
        for field, label in REASONING_LABELS.items()
        if str(reasoning.get(field) or "").strip()
    ]


def run_regenerate_workflow(strategy_id: int, remarks: str, user_id: str) -> None:
    """Background entry: own DB session so reject-regenerate can return before Groq finishes."""
    import logging
    import uuid as uuid_mod

    from app.infrastructure.database.session import SessionLocal

    logger = logging.getLogger("app.workflow_suggestion")
    db = SessionLocal()
    try:
        try:
            uid = uuid_mod.UUID(str(user_id))
        except (TypeError, ValueError):
            logger.error("Background regenerate: invalid user_id=%s", user_id)
            return
        user = db.get(UserModel, uid)
        if user is None:
            logger.error("Background regenerate: user not found id=%s", user_id)
            return
        WorkflowSuggestionService(db).regenerate_workflow(user, strategy_id, remarks)
        logger.info("Background regenerate done strategy_id=%s", strategy_id)
    except Exception:  # noqa: BLE001
        logger.exception("Background regenerate failed strategy_id=%s", strategy_id)
    finally:
        db.close()


def schedule_regenerate_workflow(
    background_tasks: Any, strategy_id: int, remarks: str, user_id: str
) -> None:
    background_tasks.add_task(run_regenerate_workflow, int(strategy_id), remarks, str(user_id))
