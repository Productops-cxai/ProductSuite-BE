"""Workflow suggestion service — the entry point the routes call."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ServiceUnavailableError, ValidationAppError
from app.infrastructure.database.models import PayflowStrategyModel, UserModel
from app.modules.payflow.services.email_tracking import ORIGIN_HUMAN
from app.modules.payflow.services.strategy_service import (
    PayflowStrategyService,
    _message_text,
    is_follow_up_step,
    read_strategy_versions,
)
from app.modules.payflow.services.segmentation_config_service import default_segmentation_config
from app.modules.payflow.services.workflow_suggestion.audiences import (
    audience_keys,
    audience_label,
    audience_profile,
    find_audiences,
    members_by_segment,
    text_audiences,
)
from app.modules.payflow.services.workflow_suggestion.adaptive_follow_ups import (
    LABEL as FOLLOW_UPS_LABEL,
    add_follow_ups,
    follow_up_line,
    has_text,
)
from app.modules.payflow.services.workflow_suggestion.config import CHANNEL_FIELDS, REASONING_LABELS
from app.modules.payflow.services.workflow_suggestion.context import (
    load_client_context,
    load_payment_options,
    load_rules,
    load_segment_members,
    load_suggest_scope,
)
from app.modules.payflow.services.workflow_suggestion.gates import uncovered_accounts
from app.modules.payflow.services.workflow_suggestion.languages import (
    accounts_languages,
    extra_languages,
    language_name,
    languages_line,
    languages_line_for_steps,
    segment_stats_languages,
)
from app.modules.payflow.services.workflow_suggestion.learning import (
    learned_from,
    load_client_learning,
    pick_examples,
    pick_lessons,
)
from app.modules.payflow.services.workflow_suggestion.llm import build_output_schema, call_llm
from app.modules.payflow.services.workflow_suggestion.message_brief import MAIN, plan_messages
from app.modules.payflow.services.workflow_suggestion.message_writer import (
    apply_messages,
    check_texts,
    translate_messages,
    write_messages,
)
from app.modules.payflow.services.workflow_suggestion.required_wording import (
    LABEL as WORDING_LABEL,
    required_wording,
    required_wording_line,
    workflow_wording,
)
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

# Active / Inactive are rejected first (→ AI Proposed or Draft), then regenerated.
# Draft is included so Human Created workflows can regenerate after reject.
REGENERATE_STATUSES = {
    PayflowStrategyStatus.DRAFT.value,
    PayflowStrategyStatus.AI_PROPOSED.value,
    PayflowStrategyStatus.UNDER_REVIEW.value,
    PayflowStrategyStatus.APPROVED.value,
}
# Regenerate sets the original to this status and writes a history row with it. A Rejected workflow is
# closed and never runs (only Active workflows run; versions pin to "Active" / "Approved" history rows).
REJECTED_HISTORY_STATUS = PayflowStrategyStatus.REJECTED.value
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
    "Languages",  # the languages the messages were written in
    "Reviewer text kept",  # Regenerate: messages whose reviewer-written text was improved, not replaced
    "Audiences",  # who the messages were written for (main text and audience versions)
    FOLLOW_UPS_LABEL,  # which adaptive follow-ups have text
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
        self,
        user: UserModel,
        client_id: int,
        portfolio_id: int | None,
        *,
        only_uncovered: bool = False,
    ) -> dict[str, Any]:
        """`only_uncovered`: design workflows only for the accounts that fit no existing workflow
        (the automatic run on a scope that already has workflows) - never a second workflow for a
        segment that has one."""
        self.strategies._assert_manage(user, client_id)
        client = load_client_context(self.db, client_id)
        scope, accounts = load_suggest_scope(self.db, client_id, portfolio_id)
        if only_uncovered:
            accounts = uncovered_accounts(self.db, client_id, portfolio_id, accounts)
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

        # Email / SMS text for every workflow (one AI call each, plus one per other language of its
        # segment's accounts), written before saving.
        writer_client = {**client, "payment_options": load_payment_options(self.db, client["id"])}
        members = members_by_segment(accounts, segments)  # each segment's own accounts → its audiences
        segmentation = default_segmentation_config()  # the fixed age groups - never per-client settings
        messages_note: dict[str, list[dict[str, str]]] = {}  # segment_id → "Messages" / "Languages" lines
        for segment in segments:
            segment_id = segment["segment_id"]
            found = find_audiences(members[segment_id], segmentation=segmentation)
            performance = _email_performance(self.db, client["id"], portfolio_id=scope.id, found=found)
            # Adaptive follow-ups, written after the other messages.
            with_follow_ups = add_follow_ups(
                workflows[segment_id],
                payment_options=writer_client["payment_options"],
                situation=(segment.get("segment") or {}).get("situation"),
            )
            workflows[segment_id], messages_note[segment_id] = _with_messages(
                {**writer_client, "email_performance": performance,
                 "required_wording": required_wording(self.db, client["id"], members[segment_id])},
                with_follow_ups,
                segment,
                languages=extra_languages(client.get("language"), segment_stats_languages(segment["stats"])),
                found=found,
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
                        segmentation_snapshot=segmentation,
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

        Works for AI Generated and Human Created sources. The new one records revision_of_id
        (plus a "Revision of #id" line for reviewers), continues the version number (v2 → v3)
        and its first history row says which workflow it was made from. The original becomes
        Rejected (closed; replaced_by_id points to the new one) with a "Rejected" history row
        (who, when, remarks, new version) - all saved together with the new workflow.
        """
        original = self.strategies.get_strategy(user, strategy_id)
        _assert_not_rejected(original, "regenerate the new version")
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
        # Adaptive follow-ups (same ids as the original's, so a reviewer's follow-up text is kept below too).
        payment_options = load_payment_options(self.db, original["client_id"])
        workflow = add_follow_ups(
            workflow, payment_options=payment_options, situation=(original.get("segment") or {}).get("situation")
        )
        # Messages a reviewer wrote or edited on the original keep that text as written (and are translated);
        # the others get fresh email / SMS text, following the same remarks.
        workflow, carried = _seed_reviewer_text(workflow, original)
        members = load_segment_members(self.db, original)
        segmentation = default_segmentation_config()  # the fixed age groups - never per-client settings
        found = find_audiences(members, segmentation=segmentation)
        workflow, messages_note = _with_messages(
            {
                **client,
                "payment_options": payment_options,
                # The original's own results first: the new version replaces it.
                "email_performance": _email_performance(
                    self.db, original["client_id"], portfolio_id=original.get("portfolio_id"),
                    strategy_id=original["id"], found=found,
                ),
                "required_wording": required_wording(self.db, original["client_id"], members),
            },
            workflow,
            _stored_segment(original),
            remarks=remarks,
            languages=_other_languages(client, members),
            keep=list(carried),
            found=found,
        )

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
        if carried:
            ai_context.append({"label": "Reviewer text kept", "value": ", ".join(carried.values())})
        rejected_by = user.full_name or user.email
        rejected_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        # The new workflow and the original's rejection are saved together: both or nothing.
        try:
            new = self._save(
                user,
                original["client_id"],
                original["portfolio_id"],
                workflow,
                segment=original["segment"],
                cases_covered=original["cases_covered"],
                ai_context=ai_context,
                revision_of_id=original["id"],
                commit=False,
                version=int(original["version"] or 1) + 1,  # continues the number: v2 → v3
                history_note=(
                    f"Proposed by PayFlow AI as the next version of #{original['id']} {original['name']} "
                    f"(v{original['version']}), rejected by {rejected_by} on {rejected_at}. Remarks: {remarks}"
                ),
                segmentation_snapshot=segmentation,
            )
            self._record_rejection(original["id"], new, remarks, rejected_by=rejected_by, rejected_at=rejected_at)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        # Notifications commit on their own, so they are sent / cleared only after the save.
        self.strategies.notify_awaiting(user, new["id"])
        self.strategies._mark_notifications_read(self.db.get(PayflowStrategyModel, original["id"]))
        return self.strategies.get_strategy(user, new["id"])

    def _record_rejection(
        self, original_id: int, new: dict[str, Any], remarks: str, *, rejected_by: str, rejected_at: str
    ) -> None:
        """The original becomes Rejected (closed: replaced by `new`), with a history row saying who rejected
        it, when (date and time), why, and its new version.

        Kept out of AI learning on purpose (no reviewer_note, no changes): the remarks already reach
        the AI through the reject note and the new workflow's "Reviewer remarks" line.
        """
        row = self.db.get(PayflowStrategyModel, original_id)
        row.status = REJECTED_HISTORY_STATUS
        self.strategies._prepend_version(
            row,
            {
                "version": int(row.version or 1),
                "date": rejected_at,
                "note": (
                    f"Rejected / regeneration requested by {rejected_by}. "
                    f"New version: #{new['id']} (v{new['version']}). Remarks: {remarks}"
                ),
                "status": REJECTED_HISTORY_STATUS,
                "changes": [],
            },
        )

    def write_workflow_messages(
        self,
        user: UserModel,
        strategy_id: int,
        step_id: str | None = None,
        remarks: str | None = None,
        *,
        audience: str | None = None,
        follow_ups_only: bool = False,
    ) -> dict[str, Any]:
        """AI writes the email / SMS text of a workflow — every message, or only `step_id`.

        `audience` (with `step_id`): only that one text of the message - MAIN ("main") or one of its audience
        versions ("age_65_plus") - and its translations. The message's other texts and the other steps stay as they are.
        Every message, or `follow_ups_only`: the workflow's adaptive follow-ups are added first when it has none yet
        (a workflow built by hand). `follow_ups_only` then writes only the follow-ups without text - a person's own
        texts in the workflow are never touched.
        """
        if follow_ups_only and (step_id or audience):
            raise ValidationAppError(
                "follow_ups_only writes the follow-ups without text - leave out step_id and audience"
            )
        workflow = self.strategies.get_strategy(user, strategy_id)
        _assert_not_rejected(workflow, "write the messages of the new version")
        if workflow["status"] not in WRITE_MESSAGES_STATUSES:
            raise ValidationAppError(
                "Message text can only be written for Draft, AI Proposed or Under Review workflows"
            )
        self.strategies._assert_manage(user, workflow["client_id"])
        one_text_step = _one_text_step(workflow, step_id, audience) if audience else None
        members = load_segment_members(self.db, workflow)
        found = find_audiences(members, segmentation=default_segmentation_config())  # the fixed age groups
        client = {
            **load_client_context(self.db, workflow["client_id"]),
            "payment_options": load_payment_options(self.db, workflow["client_id"]),
            "email_performance": _email_performance(
                self.db, workflow["client_id"], portfolio_id=workflow.get("portfolio_id"),
                strategy_id=workflow["id"], found=found,
            ),
            "required_wording": required_wording(self.db, workflow["client_id"], members),
        }
        if one_text_step is not None:
            return self._write_one_text(user, workflow, client, members, found, one_text_step, audience, remarks)
        step_ids = [step_id] if step_id else None
        if not step_id:
            workflow = add_follow_ups(
                workflow,
                payment_options=client["payment_options"],
                situation=(workflow.get("segment") or {}).get("situation"),
            )
        if follow_ups_only:
            step_ids = [step["id"] for step in workflow["steps"] if is_follow_up_step(step) and not has_text(step)]
            if not step_ids:
                raise ValidationAppError(
                    "No follow-up to write: they are all written, or this workflow has no reminder or payment-link "
                    "message for a follow-up to replace"
                )
        written, audiences_note = _write_for_audiences(
            client,
            workflow,
            _stored_segment(workflow),
            found,
            step_ids=step_ids,
            remarks=remarks,
        )
        languages = _other_languages(client, members)
        written, _ = _with_translations(client, workflow, written, languages)
        steps = apply_messages(workflow["steps"], written)
        ai_context = _refreshed_context(workflow.get("ai_context"), client.get("language"), languages, steps)
        if step_ids is None:  # every message rewritten: who they are written for is new too
            ai_context = [item for item in ai_context if item.get("label") != "Audiences"] + audiences_note
        ai_context = [item for item in ai_context if item.get("label") != PERFORMANCE_LABEL] + _performance_note(client)
        ai_context = _with_wording_line(ai_context, client)
        return self.strategies.save_ai_messages(user, strategy_id, steps, remarks=remarks, ai_context=ai_context)

    def _write_one_text(
        self,
        user: UserModel,
        workflow: dict[str, Any],
        client: dict[str, Any],
        members: list,
        found: dict[str, Any],
        step: dict[str, Any],
        key: str,
        remarks: str | None,
    ) -> dict[str, Any]:
        """Write messages for one text of one message (write_workflow_messages with `audience`): written, translated
        into the workflow's other languages, put back next to the message's other texts, and noted in the history."""
        step_id, versions = step["id"], (step.get("config") or {}).get("versions")
        text = _write_one(client, workflow, _stored_segment(workflow), members, found, step, key, remarks)
        languages = _other_languages(client, members)
        if key == MAIN:
            written, _ = _with_translations(client, workflow, {step_id: text}, languages)
            translated = written[step_id].get("translations") or {}
            failed = [f"main text in {language_name(code)}" for code in languages if code not in translated]
            steps = apply_messages(workflow["steps"], {step_id: {**written[step_id], "versions": versions}})
            what, fallback = "main text", "those customers get it untranslated"
        else:
            done, failed = _translate_versions(client, workflow, {(step_id, key): text}, languages)
            steps = _with_version(workflow["steps"], step_id, key, done[(step_id, key)])
            what, fallback = f"{text['label']} version", "those customers get the main text"
        note = f"Message text written by AI for {user.full_name}: {what} of {step.get('title') or step_id}"
        if remarks:
            note += f" (remarks: {remarks})"
        if failed:
            note += f"; not translated: {', '.join(failed)} - {fallback}"
        ai_context = _refreshed_context(workflow.get("ai_context"), client.get("language"), languages, steps)
        ai_context = [item for item in ai_context if item.get("label") != PERFORMANCE_LABEL] + _performance_note(client)
        ai_context = _with_wording_line(ai_context, client)
        return self.strategies.save_ai_messages(user, workflow["id"], steps, note=note, ai_context=ai_context)

    def workflow_audiences(self, user: UserModel, strategy_id: int) -> dict[str, Any]:
        """Who gets each of the workflow's texts - the main text and each audience version - from today's accounts:
        the context the AI writes them for (audiences.text_audiences). No AI call; nothing is saved."""
        workflow = self.strategies.get_strategy(user, strategy_id)
        members = load_segment_members(self.db, workflow)
        return {
            "workflow_id": workflow["id"],
            "accounts": len(members),
            "situation": (workflow.get("segment") or {}).get("situation"),
            # Counted with the settings the workflow was written with (the fixed age groups when none were saved).
            "texts": text_audiences(
                members, workflow.get("steps") or [], segmentation=workflow.get("segmentation_snapshot")
            ),
        }

    def update_workflow(self, user: UserModel, strategy_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """The existing workflow update, with every edited text translated again (_update_and_translate).

        Every text the save changed - a main text or an audience version - is also checked like the AI's own text
        (message_writer.check_texts): the problems come back in "text_warnings". They never block the save.
        """
        row = self.db.get(PayflowStrategyModel, strategy_id)
        before = {step.get("id"): step for step in copy.deepcopy((row.steps if row else None) or [])}
        result = self._update_and_translate(user, strategy_id, payload, before)
        return {**result, "text_warnings": self._text_warnings(before, result)}

    def _text_warnings(self, before: dict[str, dict], workflow: dict[str, Any]) -> list[dict[str, str]]:
        """Problems with the texts a save changed ([] when none changed or all are fine) - including a missing
        wording the client's compliance rules require (the send check would block that message)."""
        texts = _edited_texts(before, workflow.get("steps") or [])
        if not texts:
            return []
        language = load_client_context(self.db, workflow["client_id"]).get("language") or "English"
        return check_texts(workflow, _stored_segment(workflow), texts,
                           english=str(language).strip().lower().startswith("english"),
                           wording=workflow_wording(self.db, workflow))

    def _update_and_translate(
        self, user: UserModel, strategy_id: int, payload: dict[str, Any], before: dict[str, dict]
    ) -> dict[str, Any]:
        """The existing workflow update, then every message whose main text it changed is translated again into
        the other languages of the workflow's segment (one AI call per language) - and so is every audience
        version whose own text it changed. `before`: the stored steps by id.

        update_strategy drops an edited text's outdated other-language versions; the main text and each audience
        version are edited on their own, and nothing else is removed. A translation that fails never undoes the
        edit: those customers get the edited text in the main language, and the workflow's history says so.
        """
        updated = self.strategies.update_strategy(user, strategy_id, payload)

        edited = {
            step["id"]: step
            for step in updated["steps"]
            if step.get("kind") == "Communication"
            and any(_message_text(step))
            and _message_text(step) != _message_text(before.get(step["id"]) or {})
            and not (step.get("config") or {}).get("translations")  # versions sent with the edit are kept
        }
        edited_versions = _edited_versions(before, updated["steps"])
        if not edited and not edited_versions:
            return updated
        client = load_client_context(self.db, updated["client_id"])
        members = load_segment_members(self.db, updated)
        languages = _other_languages(client, members)
        if not languages:
            return updated
        # The translations keep the compliance rules' wording as written (the send check looks for it).
        client["required_wording"] = required_wording(self.db, updated["client_id"], members)

        written = {
            step_id: {key: value for key, value in (step.get("config") or {}).items()
                      if key in ("subject", "body", "sms_body", "tone") and value}
            for step_id, step in edited.items()
        }
        written, _ = _with_translations(client, updated, written, languages)
        versions, failed_versions = _translate_versions(client, updated, edited_versions, languages)
        steps = []
        for step in updated["steps"]:
            config = dict(step.get("config") or {})
            if (written.get(step.get("id")) or {}).get("translations"):
                config["translations"] = written[step["id"]]["translations"]
            for (step_id, key), version in versions.items():
                if step_id == step.get("id"):
                    config["versions"] = {**(config.get("versions") or {}), key: version}
            steps.append({**step, "config": config})
        done = [code for code in languages if all(code in (m.get("translations") or {}) for m in written.values())]
        failed = [code for code in languages if code not in done]
        titles = [step.get("title") or step_id for step_id, step in edited.items()]
        titles += [f"{version.get('label') or key} version of {step_id}"
                   for (step_id, key), version in edited_versions.items()]
        note = f"Edited messages ({', '.join(titles)}) translated by AI into: {_names(done) or 'none'}"
        if failed:
            note += f"; not translated into {_names(failed)} - these customers get the edited text"
        if failed_versions:
            note += f"; not translated: {', '.join(failed_versions)} - those customers get the main text"
        return self.strategies.save_ai_messages(
            user,
            strategy_id,
            steps,
            note=note,
            ai_context=_refreshed_context(updated.get("ai_context"), client.get("language"), languages, steps),
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
            schema = build_output_schema([s["segment_id"] for s in pending])
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
        version: int = 1,
        history_note: str | None = None,
        segmentation_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Existing create path: AI Generated → status/origin "AI Proposed" + AI snapshot.
        # portfolio_id=None → client-level workflow for parent accounts.
        # version: 1 on Suggest; Regenerate continues the original's number (history_note: made from what).
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
                "segmentation_snapshot": segmentation_snapshot,
            },
            commit=commit,
            version=version,
            history_note=history_note,
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
    return {w["segment_id"]: _without_channels(w) for w in items if isinstance(w, dict) and w.get("segment_id")}


def _without_channels(workflow: dict[str, Any]) -> dict[str, Any]:
    """Communication steps contact the customer: any channel / template the AI set anyway is dropped,
    because the worker picks email or SMS for each customer."""
    if not isinstance(workflow.get("steps"), list):
        return workflow
    steps = []
    for step in workflow["steps"]:
        if isinstance(step, dict) and step.get("kind") == "Communication":
            config = step.get("config") if isinstance(step.get("config"), dict) else {}
            step = {key: value for key, value in step.items() if key not in CHANNEL_FIELDS}
            step["config"] = {key: value for key, value in config.items() if key not in CHANNEL_FIELDS}
        steps.append(step)
    return {**workflow, "steps": steps}


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
    client: dict[str, Any],
    workflow: dict[str, Any],
    segment: dict[str, Any],
    *,
    remarks: str | None = None,
    languages: list[str] | None = None,
    keep: list[str] | None = None,
    found: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The workflow with its email / SMS text written in (`remarks`: the reviewer's, on Regenerate) - the main
    text and a version for each audience in `found` (audiences.find_audiences; None: one text for everyone) -
    plus a version in each of `languages` (the other languages of its segment's accounts).

    `keep`: message steps whose text (already on the step: a reviewer's) is kept as written - only
    translated, and sent to everyone (no audience versions); the AI sees it as the workflow's other messages.
    Writing never blocks a save: if it fails, the workflow is kept as it is (its templates
    are sent) and a "Messages" ai_context line tells reviewers to write them later.
    """
    messages = [step for step in workflow.get("steps") or [] if step.get("kind") == "Communication"]
    if not messages:
        return workflow, []
    keep = [step_id for step_id in keep or [] if any(step.get("id") == step_id for step in messages)]
    to_write = [step["id"] for step in messages if step["id"] not in keep]
    written: dict[str, dict[str, Any]] = {}
    audiences_note: list[dict[str, str]] = []
    try:
        step_ids = to_write if keep else None  # nothing kept: write every message, as before
        if to_write:
            written, audiences_note = _write_for_audiences(
                client, workflow, segment, found or {}, step_ids=step_ids, remarks=remarks
            )
    except AppError as exc:
        note = f"Not written, so the templates will be sent. Use Write messages to try again. ({exc.message})"
        return workflow, [{"label": "Messages", "value": note}] + follow_up_line(workflow.get("steps"))
    for step in messages:
        if step["id"] in keep:
            written[step["id"]] = {key: value for key, value in (step.get("config") or {}).items()
                                   if key in ("subject", "body", "sms_body", "tone", "content_origin") and value}
    written, languages_note = _with_translations(client, workflow, written, languages or [])
    steps = apply_messages(workflow["steps"], written)
    notes = (languages_note + audiences_note + required_wording_line(client.get("required_wording"))
             + (_performance_note(client) if to_write else []) + follow_up_line(steps))
    return {**workflow, "steps": steps}, notes


def _write_for_audiences(
    client: dict[str, Any],
    workflow: dict[str, Any],
    segment: dict[str, Any],
    found: dict[str, Any],
    *,
    step_ids: list[str] | None = None,
    remarks: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """{step_id: main text + its "brief" + its audience "versions"}, and the "Audiences" ai_context line.

    AI call 1 (plan_messages) plans every message of the flowchart for the main text and for each audience in
    `found`; then the adaptive follow-ups are planned, one call per text (main, then each audience) so a call
    stays small whatever the number of audiences, and a failure leaves only the follow-ups without a plan (or a
    version). write_messages then writes the main text, then each audience's version, from those plans. Raises
    AppError when the main text cannot be written. An audience version that fails is left out - those customers
    get the main text. Without plans (the AI could not plan) the main text is written as before, with no versions.
    """
    steps = {step.get("id"): step for step in workflow.get("steps") or []}
    asked = [step_id for step_id, step in steps.items()
             if step.get("kind") == "Communication" and (step_ids is None or step_id in step_ids)]
    flowchart = [step_id for step_id in asked if not is_follow_up_step(steps[step_id])]
    follow_ups = [step_id for step_id in asked if is_follow_up_step(steps[step_id])]
    plans = plan_messages(client, workflow, segment, found, step_ids=flowchart) if flowchart else {}
    for key in [MAIN, *(audience["key"] for audience in found.get("audiences") or [])] if follow_ups else []:
        for step_id, by_audience in plan_messages(client, workflow, segment, found, step_ids=follow_ups,
                                                  only=key).items():
            plans.setdefault(step_id, {}).update(by_audience)
    main_plans = _plans_for(plans, MAIN)
    main = {"key": MAIN, "label": "Main text", **(found.get("main") or {})}
    written = write_messages(
        client, workflow, segment, step_ids=step_ids, remarks=remarks, plans=main_plans,
        audience=main if plans else None,
    )
    for step_id, brief in main_plans.items():
        if step_id in written:
            written[step_id] = {**written[step_id], "brief": brief}

    done, failed = [], []
    for audience in found.get("audiences") or []:
        audience_plans = {step_id: brief for step_id, brief in _plans_for(plans, audience["key"]).items()
                          if step_id in written}
        if not audience_plans:
            continue
        try:
            texts = write_messages(client, workflow, segment, step_ids=list(audience_plans), remarks=remarks,
                                   plans=audience_plans, audience=audience)
        except AppError:
            failed.append(audience)
            continue
        for step_id, text in texts.items():
            version = {
                "label": audience["label"],
                **text,
                "brief": audience_plans[step_id],
                "segment": {
                    "dimension": audience.get("dimension") or "age",
                    "criteria": audience.get("criteria") or {},
                },
            }
            written[step_id] = {**written[step_id],
                                "versions": {**(written[step_id].get("versions") or {}), audience["key"]: version}}
        done.append(audience)
    return written, _audiences_line(found, done, failed, planned=bool(plans))


def _plans_for(plans: dict[str, dict[str, dict[str, str]]], audience: str) -> dict[str, dict[str, str]]:
    """{step_id: brief} of one audience, from plan_messages' {step_id: {audience: brief}}."""
    return {step_id: by_audience[audience] for step_id, by_audience in plans.items() if audience in by_audience}


def _with_wording_line(ai_context: list[dict[str, str]], client: dict[str, Any]) -> list[dict[str, str]]:
    """ai_context with the "Required wording" line of the wording the messages were just written with."""
    kept = [item for item in ai_context if item.get("label") != WORDING_LABEL]
    return kept + required_wording_line(client.get("required_wording"))


def _one_text_step(workflow: dict[str, Any], step_id: str | None, key: str) -> dict[str, Any]:
    """The message step whose `key` text (MAIN or one of its audience versions) is rewritten on its own -
    ValidationAppError when there is no such text to rewrite."""
    if not step_id:
        raise ValidationAppError("Choose the message (step_id) whose text to rewrite")
    step = next((s for s in workflow.get("steps") or [] if s.get("id") == step_id), None)
    if step is None or step.get("kind") != "Communication":
        raise ValidationAppError(f"Step {step_id} is not an email or SMS step of this workflow")
    if key != MAIN and key not in ((step.get("config") or {}).get("versions") or {}):
        raise ValidationAppError(
            f"Message {step_id} has no {audience_label(key)} version - rewrite its main text or one of its versions"
        )
    return step


def _write_one(
    client: dict[str, Any],
    workflow: dict[str, Any],
    segment: dict[str, Any],
    members: list,
    found: dict[str, Any],
    step: dict[str, Any],
    key: str,
    remarks: str | None,
) -> dict[str, Any]:
    """One text of one message, planned and written on its own: the main text (MAIN) or one audience's version
    ({"label", "subject", "body", "sms_body", "tone", "brief"}). The AI is shown that text as the message's
    current text, so it improves it. Raises AppError when it cannot be written."""
    step_id = step["id"]
    if key == MAIN:
        audience = {"key": MAIN, "label": "Main text", **(found.get("main") or {})}
        found, current = {**found, "audiences": []}, workflow
    else:
        version = step["config"]["versions"][key]
        audience = _audience_for(found, members, key, version.get("label"))
        found, current = {**found, "audiences": [audience]}, _with_step_text(workflow, step_id, version)
    plans = _plans_for(plan_messages(client, current, segment, found, step_ids=[step_id], only=key), key)
    if key != MAIN and not plans:
        raise ServiceUnavailableError(f"AI could not plan the {audience['label']} version - please try again")
    written = write_messages(client, current, segment, step_ids=[step_id], remarks=remarks, plans=plans,
                             audience=audience if plans else None)
    text = {name: value for name, value in written[step_id].items() if value}
    if key == MAIN:
        return {**text, **({"brief": plans[step_id]} if plans else {})}
    return {
        "label": audience["label"],
        **text,
        "brief": plans[step_id],
        "segment": {
            "dimension": audience.get("dimension") or (
                "country" if key.startswith("country_") else "region" if key.startswith("region_") else "age"
            ),
            "criteria": audience.get("criteria") or {},
        },
    }


def _audience_for(found: dict[str, Any], members: list, key: str, label: str | None = None) -> dict[str, Any]:
    """Audience `key` as find_audiences lists it; built from the members of that age group when it is no longer
    big enough to be listed (it was when its version was written), or is one of the older wider groups."""
    listed = next((audience for audience in found.get("audiences") or [] if audience["key"] == key), None)
    if listed:
        return listed
    accounts = [account for account in members if key in audience_keys(account)]
    return {"key": key, "label": label or audience_label(key), "accounts": len(accounts),
            "profile": audience_profile(accounts)}


def _with_step_text(workflow: dict[str, Any], step_id: str, text: dict[str, Any]) -> dict[str, Any]:
    """The workflow with `text` (an audience version) as the step's current text - the text the AI improves."""
    own = {key: text.get(key) for key in ("subject", "body", "sms_body")}
    steps = [
        {**step, "config": {**(step.get("config") or {}), **own}} if step.get("id") == step_id else step
        for step in workflow.get("steps") or []
    ]
    return {**workflow, "steps": steps}


def _with_version(steps: list[dict], step_id: str, key: str, version: dict[str, Any]) -> list[dict[str, Any]]:
    """Copy of the steps with `version` as the step's `key` audience version - its other texts unchanged."""
    result = []
    for step in steps:
        if step.get("id") == step_id:
            config = dict(step.get("config") or {})
            config["versions"] = {**(config.get("versions") or {}), key: version}
            step = {**step, "config": config}
        result.append(step)
    return result


def _audiences_line(
    found: dict[str, Any], done: list[dict], failed: list[dict], *, planned: bool
) -> list[dict[str, str]]:
    """The "Audiences" ai_context line: who the messages were written for."""
    main_accounts = (found.get("main") or {}).get("accounts", 0)
    if not planned:
        value = "One text for everyone - the AI could not plan the messages per audience (Write messages tries again)"
    elif not found.get("audiences"):
        value = f"One text for all {main_accounts} accounts - no age group is big enough for its own version"
    else:
        parts = [f"Own version for {', '.join(_who(audience) for audience in done)}"] if done else []
        parts.append(f"main text for the other {main_accounts} accounts")
        if failed:
            labels = ", ".join(audience["label"] for audience in failed)
            parts.append(f"not written for {labels} - they get the main text")
        value = "; ".join(parts)
    return [{"label": "Audiences", "value": value}]


def _who(audience: dict[str, Any]) -> str:
    return f"{audience['label']} ({audience['accounts']} accounts)"


def _other_languages(client: dict[str, Any], members: list) -> list[str]:
    """The languages of a workflow's accounts other than the client's own (whose text is the main text)."""
    return extra_languages(client.get("language"), accounts_languages(members, lambda account: True))


PERFORMANCE_LABEL = "Email performance"


def _email_performance(
    db: Session,
    client_id: int,
    *,
    portfolio_id: int | None = None,
    strategy_id: int | None = None,
    found: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """This client's measured email subjects, for the planner and the writer (message_brief, message_prompt):
    the closest history with enough results - the workflow's own (`strategy_id`), its portfolio's, the client's -
    plus each audience's own results (`found`: audiences.find_audiences).
    None: no history yet - or it could not be read, which never blocks writing."""
    try:
        from app.modules.payflow.services.email_subject_ai import PayflowEmailSubjectAiService

        return PayflowEmailSubjectAiService(db).performance_context(
            client_id,
            portfolio_id=portfolio_id,
            strategy_id=strategy_id,
            audiences=[audience["key"] for audience in (found or {}).get("audiences") or []],
        )
    except Exception:  # noqa: BLE001
        return None


def _performance_note(client: dict[str, Any]) -> list[dict[str, str]]:
    """The ai_context line that tells reviewers the emails were written with measured results."""
    performance = client.get("email_performance")
    if not performance or not performance.get("best"):
        return []
    best = performance["best"][0]
    source = ", ".join(part for part in (performance.get("scope"), performance.get("period")) if part)
    audiences = ", ".join(audience_label(key) for key in performance.get("by_audience") or {})
    return [
        {
            "label": PERFORMANCE_LABEL,
            "value": (
                f"Emails written using this client's measured results{f' ({source})' if source else ''}: "
                f"{len(performance['best'])} strong and {len(performance.get('weak') or [])} weak subjects"
                f"{f'; own results for {audiences}' if audiences else ''}. Best so far: \"{best['subject']}\" - "
                f"{best['conversion']} conversion on {best['delivered']:,} delivered."
            ),
        }
    ]


def _with_translations(
    client: dict[str, Any], workflow: dict[str, Any], written: dict[str, dict[str, Any]], languages: list[str]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """`written` with a version in each of `languages` (account language codes), and the "Languages" line.

    One AI call per language for the main text, and one per language for each audience's versions. A
    language that cannot be translated is left out - its customers get the main text - and never blocks a save.
    The adaptive follow-ups are translated in a call of their own: when they fail, the language still counts for
    the other messages, and its customers get the planned messages instead of a follow-up (next_action.py).
    """
    steps = {step.get("id"): step for step in workflow.get("steps") or []}
    follow_ups = {step_id: m for step_id, m in written.items() if is_follow_up_step(steps.get(step_id))}
    flowchart = {step_id: m for step_id, m in written.items() if step_id not in follow_ups}
    groups = [group for group in (flowchart, follow_ups) if group]
    done: list[str] = []
    failed: list[str] = []
    follow_ups_missing: list[str] = []
    for code in languages if written else []:
        try:
            versions = translate_messages(client, workflow, groups[0], code)
        except AppError:
            failed.append(code)
            continue
        if len(groups) > 1:
            try:
                versions.update(translate_messages(client, workflow, groups[1], code))
            except AppError:
                follow_ups_missing.append(code)
        written = {
            step_id: (
                {**message, "translations": {**(message.get("translations") or {}), code: versions[step_id]}}
                if step_id in versions
                else message
            )
            for step_id, message in written.items()
        }
        done.append(code)
    note = languages_line(client.get("language"), done, failed, follow_ups_missing)

    audience_versions = {
        (step_id, key): version
        for step_id, message in written.items()
        for key, version in (message.get("versions") or {}).items()
    }
    translated, failed_versions = _translate_versions(client, workflow, audience_versions, languages)
    for (step_id, key), version in translated.items():
        written[step_id] = {**written[step_id], "versions": {**written[step_id]["versions"], key: version}}
    if failed_versions:
        extra = f"not written: {', '.join(failed_versions)} - those customers get the main text in their language"
        note = ([{**note[0], "value": f"{note[0]['value']}; {extra}"}] if note
                else [{"label": "Languages", "value": extra[0].upper() + extra[1:]}])
    return written, note


def _translate_versions(
    client: dict[str, Any], workflow: dict[str, Any], versions: dict[tuple[str, str], dict], languages: list[str]
) -> tuple[dict[tuple[str, str], dict], list[str]]:
    """{(step_id, audience): version with its other-language versions}, and what could not be translated
    ("Age 65 and over in French"). One AI call per audience and language: a customer gets all of their
    audience's messages in the same language, or the main text. The audience's follow-up versions are
    translated in a call of their own ("Age 65 and over follow-ups in French" when it fails)."""
    steps = {step.get("id"): step for step in workflow.get("steps") or []}
    result = {pair: dict(version) for pair, version in versions.items()}
    failed: list[str] = []
    for key in dict.fromkeys(audience for _, audience in versions):
        texts = {step_id: version for (step_id, audience), version in result.items() if audience == key}
        label = next(iter(texts.values())).get("label") or key
        follow_ups = {step_id: text for step_id, text in texts.items() if is_follow_up_step(steps.get(step_id))}
        flowchart = {step_id: text for step_id, text in texts.items() if step_id not in follow_ups}
        groups = [(group, name) for group, name in ((flowchart, label), (follow_ups, f"{label} follow-ups")) if group]
        for code in languages:
            for group, name in groups:
                try:
                    translated = translate_messages(client, workflow, group, code)
                except AppError:
                    failed.append(f"{name} in {language_name(code)}")
                    continue
                for step_id in group:
                    version = result[(step_id, key)]
                    version["translations"] = {**(version.get("translations") or {}), code: translated[step_id]}
    return result, failed


def _edited_texts(before: dict[str, dict], steps: list[dict]) -> list[tuple[dict, str, dict]]:
    """[(step, MAIN or an audience key, text)] for every main text and audience version a save changed or added
    (a cleared one is not listed: the step's template is sent)."""
    names = ("subject", "body", "sms_body")
    edited = []
    for step in steps:
        if step.get("kind") != "Communication":
            continue
        old = before.get(step.get("id")) or {}
        cfg = step.get("config") or {}
        if any(_message_text(step)) and _message_text(step) != _message_text(old):
            edited.append((step, MAIN, cfg))
        old_versions = (old.get("config") or {}).get("versions") or {}
        for key, version in (cfg.get("versions") or {}).items():
            was = old_versions.get(key) or {}
            if any(version.get(name) for name in names) and any(
                (version.get(name) or "") != (was.get(name) or "") for name in names
            ):
                edited.append((step, key, version))
    return edited


def _edited_versions(before: dict[str, dict], steps: list[dict]) -> dict[tuple[str, str], dict]:
    """Audience versions whose own text a person edited - update_strategy dropped their other-language versions."""
    edited = {}
    for step in steps:
        if step.get("kind") != "Communication":
            continue
        old = ((before.get(step.get("id")) or {}).get("config") or {}).get("versions") or {}
        for key, version in ((step.get("config") or {}).get("versions") or {}).items():
            was = old.get(key) or {}
            if not version.get("translations") and any(
                version.get(name) != was.get(name) for name in ("subject", "body", "sms_body")
            ):
                edited[(step["id"], key)] = version
    return edited


def _seed_reviewer_text(
    workflow: dict[str, Any], original: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """The new workflow with the original's reviewer-written message text (and tone) on matching steps (same id
    and purpose). Also returns {step_id: title} of those steps: their text is kept as written."""
    reviewed = _reviewer_text(original)
    steps, carried = [], {}
    for step in workflow.get("steps") or []:
        old = reviewed.get(step.get("id"))
        if old is not None and step.get("kind") == "Communication" and _purpose(old) == _purpose(step):
            text = {key: value for key, value in (old.get("config") or {}).items()
                    if key in ("subject", "body", "sms_body", "tone") and value}
            # Who wrote it (email_tracking): a person - the original's own mark when it has one.
            text["content_origin"] = (old.get("config") or {}).get("content_origin") or ORIGIN_HUMAN
            step = {**step, "config": {**(step.get("config") or {}), **text}}
            carried[step["id"]] = step.get("title") or step["id"]
        steps.append(step)
    return {**workflow, "steps": steps}, carried


def _reviewer_text(original: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The original's message steps whose text a person wrote or edited: it differs from the AI's proposal
    (or there is no AI proposal: a hand-made workflow)."""
    proposal = {
        step.get("id"): step
        for step in ((original.get("ai_proposal_snapshot") or {}).get("steps") or [])
        if isinstance(step, dict)
    }
    return {
        step["id"]: step
        for step in original.get("steps") or []
        if step.get("kind") == "Communication"
        and any(_message_text(step))
        and (step["id"] not in proposal or _message_text(proposal[step["id"]]) != _message_text(step))
    }


def _purpose(step: dict[str, Any]) -> str | None:
    return (step.get("config") or {}).get("purpose") or step.get("purpose")


def _refreshed_context(
    ai_context: list[dict[str, str]] | None, client_language: Any, languages: list[str], steps: list[dict]
) -> list[dict[str, str]]:
    """ai_context with a "Languages" line that matches the steps, and without the "Messages: not written"
    line once every message has text."""
    all_written = all(
        any((step.get("config") or {}).get(key) for key in ("subject", "body", "sms_body"))
        for step in steps
        if step.get("kind") == "Communication"
    )
    stale = {"Languages", FOLLOW_UPS_LABEL} | ({"Messages"} if all_written else set())
    kept = [item for item in ai_context or [] if isinstance(item, dict) and item.get("label") not in stale]
    return kept + languages_line_for_steps(client_language, languages, steps) + follow_up_line(steps)


def _names(codes: list[str]) -> str:
    return ", ".join(language_name(code) for code in codes)


def _assert_not_rejected(workflow: dict[str, Any], instead: str) -> None:
    """A Rejected workflow was replaced by a regenerated version: point to that version instead."""
    if workflow.get("status") != REJECTED_HISTORY_STATUS:
        return
    replaced_by = f" by #{workflow['replaced_by_id']}" if workflow.get("replaced_by_id") else ""
    raise ValidationAppError(f"This workflow was rejected and replaced{replaced_by} - {instead}")


def _stored_segment(workflow: dict[str, Any]) -> dict[str, Any]:
    """A saved workflow's segment in the segmenter's shape; stats come from its ai_context (none if hand-made)."""
    context = {
        item["label"]: item["value"]
        for item in workflow.get("ai_context") or []
        if isinstance(item, dict) and item.get("label")
    }
    days = str(context.get("Avg days past due") or "")
    accounts_raw = str(context.get("Accounts") or "").replace(",", "").strip()
    outstanding_raw = str(context.get("Avg outstanding") or "").replace(",", "").strip()
    # "1,234.50 CAD" → leading number
    outstanding_token = outstanding_raw.split()[0] if outstanding_raw else ""
    stats: dict[str, Any] = {
        "accounts": int(accounts_raw) if accounts_raw.isdigit() else None,
        "avg_outstanding": float(outstanding_token) if _is_float(outstanding_token) else None,
        "avg_days_past_due": int(days) if days.isdigit() else None,
        "email_consent_pct": _pct_number(context.get("Email consent")),
        "email_allowed_pct": _pct_number(context.get("Email allowed")),
    }
    return {
        "label": context.get("Segment") or "Custom segment",
        "segment": workflow.get("segment") or {},
        "stats": {key: value for key, value in stats.items() if value is not None},
    }


def _pct_number(raw: Any) -> int | None:
    text = str(raw or "").strip().rstrip("%").replace(",", "")
    return int(text) if text.isdigit() else None


def _is_float(raw: str) -> bool:
    try:
        float(raw)
        return True
    except (TypeError, ValueError):
        return False


def _reasoning_context(workflow: dict[str, Any]) -> list[dict[str, str]]:
    """The AI's reasoning as ai_context lines ("Why it fits", "Channels", …); empty fields are skipped."""
    reasoning = workflow.get("reasoning") if isinstance(workflow.get("reasoning"), dict) else {}
    return [
        {"label": label, "value": str(reasoning[field]).strip()}
        for field, label in REASONING_LABELS.items()
        if str(reasoning.get(field) or "").strip()
    ]
