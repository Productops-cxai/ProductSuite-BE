"""The AI side of email subject performance.

- insight: explains a subject / KPI card / the whole section from the tracked numbers;
- suggestions: subjects for a workflow email step, backed by this client's own history;
- performance context: the history the message writer reads when it writes a workflow's emails;
- optimisation: finds weak email steps of a workflow and stores proposals for a human decision.

Rules that hold everywhere here:
- the AI is only given numbers and message text as written on workflows ({{placeholders}} kept):
  never a customer's name, contact details or account data;
- history is one client's own, never another client's;
- "not enough data" is decided by code, not by the AI;
- nothing here changes a workflow: an accepted proposal goes through the normal workflow update
  (an Active workflow is archived and goes back to review).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (
    AppError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationAppError,
)
from app.infrastructure.database.models import (
    PayflowClientModel,
    PayflowMessageProposalModel,
    PayflowMessageVersionModel,
    PayflowStrategyModel,
    UserModel,
)
from app.modules.payflow.services.email_subject_service import (
    C,
    PayflowEmailSubjectService,
    SubjectFilters,
    min_deliveries,
)
from app.modules.payflow.services.email_tracking import (
    ORIGIN_AI,
    ORIGIN_AI_EDITED,
    ORIGIN_HUMAN,
    ORIGIN_TEMPLATE,
    REMINDER_STAGES,
    content_hash,
    normalize_subject,
    reminder_stage,
    subject_key,
)

# The AI is always reached through message_writer.call_llm (looked up at call time), so the
# test suite's one switch that keeps tests away from the real AI covers this module too.
from app.modules.payflow.services.workflow_suggestion import message_writer
from app.modules.payflow.services.workflow_suggestion.audiences import audience_label

logger = logging.getLogger("app.payflow.email_subject_ai")

RECOMMEND_RETAIN = "Retain"
RECOMMEND_REVISE = "Revise"
RECOMMEND_TEST = "Test alternative subjects"
RECOMMEND_REPLACE = "Replace"
RECOMMEND_INSUFFICIENT = "Insufficient data"

STATUS_PENDING = "Pending"
STATUS_ACCEPTED = "Accepted"
STATUS_EDITED = "Edited"
STATUS_REJECTED = "Rejected"
STATUS_SUPERSEDED = "Superseded"

ORIGIN_MEASURED = "Measured history"
WEAK_RECOMMENDATIONS = (RECOMMEND_REVISE, RECOMMEND_TEST, RECOMMEND_REPLACE)
AUTOMATIC_AUTHOR = "PayFlow AI (automatic check)"
# The automatic check does not raise the same finding for the same text again within this time.
REPEAT_AFTER_DAYS = 30
# A weak email must be weak in each period with enough of its emails: the last CONFIRM_DAYS days and before.
CONFIRM_DAYS = 30
# Reviewer decisions on earlier proposals given to the AI writers (newest first) - see decision_lessons.
MAX_DECISION_LESSONS = 8
# History for the AI that writes workflow emails (performance_context): the closest scope with at least this
# many measured subjects, the recent months first; and at most this many subjects per audience.
CONTEXT_ENOUGH_SUBJECTS = 3
CONTEXT_RECENT_DAYS = 180
CONTEXT_RECENT_LABEL = "last 6 months"
CONTEXT_PER_AUDIENCE = 3
VIEW_WORKFLOWS = "view_workflows"
MANAGE_WORKFLOWS = "create_edit_workflows"

# AI explanations already given, keyed by the exact numbers they explain: {key: (time, reply)}.
_INSIGHT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_INSIGHT_CACHE_MAX = 500

DISCLAIMER = (
    "Open rates are estimates. Results also depend on timing, the email body and the accounts "
    "contacted, not the subject alone. Suggestions stay drafts until a person approves them."
)
SEVERAL_CLIENTS_NOTE = (
    "This selection covers more than one client, so it is explained from the numbers only: the AI "
    "is given one client's data at a time. Select a client for an AI explanation."
)
_METRIC_KEYS = (
    "sent",
    "delivered",
    "opens",
    "open_rate",
    "clicks",
    "click_rate",
    "payment_link_clicks",
    "initiated",
    "completed",
    "emails_with_payment",
    "conversion",
)
_KPI_METRICS = {"opens": "opened", "clicks": "clicked", "completed": "payments", "conversion": "conversion"}


def _numbers(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in _METRIC_KEYS if key in row}


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _line(row: dict[str, Any]) -> str:
    return (
        f"{row['delivered']:,} delivered · {_pct(row['open_rate'])} estimated opens · "
        f"{_pct(row['click_rate'])} clicks · {row['completed']} completed payments · "
        f"{_pct(row['conversion'])} conversion"
    )


def _peer_medians(rows: list[dict[str, Any]]) -> dict[str, float] | None:
    """Middle values of the subjects with enough delivered emails (None when there are none)."""
    qualifying = [r for r in rows if not r["low_sample"]]
    if not qualifying:
        return None
    return {
        "subjects": len(qualifying),
        "open_rate": round(median(r["open_rate"] for r in qualifying), 4),
        "click_rate": round(median(r["click_rate"] for r in qualifying), 4),
        "conversion": round(median(r["conversion"] for r in qualifying), 4),
    }


def _rule_insight(row: dict[str, Any], peers: dict[str, float] | None) -> str:
    """The explanation used when the AI is not available: fixed wording, from the same numbers."""
    if peers is None:
        return (
            f"This subject has {_line(row)}. There are no other subjects with enough delivered "
            "emails in this selection to compare it with."
        )
    if row["open_rate"] >= peers["open_rate"] and row["click_rate"] < peers["click_rate"] * 0.8:
        return (
            f"Estimated opens are at or above the typical {_pct(peers['open_rate'])}, but clicks "
            f"({_pct(row['click_rate'])}) are below the typical {_pct(peers['click_rate'])}: the subject "
            "gets attention, the email body or its call to action is the likely weak point."
        )
    if row["conversion"] >= peers["conversion"] * 1.2 and row["conversion"] > 0:
        return (
            f"Payment conversion ({_pct(row['conversion'])}) is above the typical "
            f"{_pct(peers['conversion'])} for comparable subjects in this selection."
        )
    if row["open_rate"] < peers["open_rate"] * 0.8:
        return (
            f"Estimated opens ({_pct(row['open_rate'])}) are below the typical {_pct(peers['open_rate'])}: "
            "fewer customers get as far as reading the email. Testing another subject is reasonable."
        )
    if row["conversion"] < peers["conversion"] * 0.8:
        return (
            f"Payment conversion ({_pct(row['conversion'])}) is below the typical "
            f"{_pct(peers['conversion'])} for comparable subjects in this selection."
        )
    return (
        f"Results are close to the typical subject in this selection ({_pct(peers['conversion'])} "
        "conversion). Compare alongside reminder stage, email body and account mix."
    )


_scan_lock = threading.Lock()
_last_scan_started: float | None = None


def scan_in_background_if_due() -> bool:
    """Start the automatic email-performance check from the API when it is due.

    The worker runs it on its schedule; an installation where the worker is not running would
    never get it, so opening the proposals / under-performing lists starts it too. At most one
    run per EMAIL_OPTIMIZATION_SCAN_HOURS and per process; a finding already raised is never
    raised twice, so a run by both the worker and the API adds nothing. Never during tests.
    """
    global _last_scan_started
    hours = int(settings.EMAIL_OPTIMIZATION_SCAN_HOURS or 0)
    if hours <= 0 or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    with _scan_lock:
        now = time.monotonic()
        if _last_scan_started is not None and now - _last_scan_started < hours * 3600:
            return False
        _last_scan_started = now

    def run() -> None:
        from app.infrastructure.database.session import SessionLocal

        db = SessionLocal()
        try:
            result = PayflowEmailSubjectAiService(db).scan_low_performers()
            if result.get("proposals"):
                logger.info("email performance check (from the API) %s", result)
        except Exception:  # noqa: BLE001
            logger.exception("email performance check (from the API) failed")
        finally:
            db.close()

    threading.Thread(target=run, name="email-performance-scan-api", daemon=True).start()
    return True


_INSIGHT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["insight"],
    "properties": {
        "insight": {"type": "string"},
        "observations": {"type": "array", "items": {"type": "string"}},
    },
}
_INSIGHT_SYSTEM = (
    "You explain email subject performance for a debt-collection platform to an operations user.\n"
    "Use only the numbers you are given - never invent or estimate a number.\n"
    "Open rates are estimates. Rates use delivered emails as the denominator. Conversion = completed "
    "payment transactions / delivered emails.\n"
    "These are observed associations, not causes: reminder stage, timing, email body and account mix "
    "also drive results - say so when you compare.\n"
    "Weigh delivered volume and conversion, not raw counts. Be specific and plain: 2-4 sentences in "
    '"insight", and up to 3 short "observations". Reply with JSON only.'
)


class PayflowEmailSubjectAiService:
    def __init__(self, db: Session):
        self.db = db
        self.subjects = PayflowEmailSubjectService(db)
        self.access = self.subjects.access

    # ------------------------------------------------------------------ insights

    def insight(
        self,
        user: UserModel,
        filters: SubjectFilters,
        *,
        scope: str = "subject",
        subject: str | None = None,
        metric: str | None = None,
    ) -> dict[str, Any]:
        """`scope`: "subject" (a table row / the popup), "kpi" (`metric`: opens | clicks | completed |
        conversion) or "section" (everything in the selected filters)."""
        scope = (scope or "subject").strip().lower()
        if scope not in {"subject", "kpi", "section"}:
            raise ValidationAppError("scope must be subject, kpi or section")
        rows = self.subjects.subject_rows(filters if scope == "kpi" else replace(filters, search=None))
        if scope == "section":
            # The AI is given one client's data at a time: a selection of several clients is
            # explained from the numbers only.
            one_client = len(self.subjects.client_ids_in(replace(filters, search=None))) <= 1
            return self._section_insight(rows, use_ai=one_client)

        if scope == "kpi":
            if metric not in _KPI_METRICS:
                raise ValidationAppError(f"metric must be one of: {', '.join(_KPI_METRICS)}")
            row = self.subjects.leaders(rows)[_KPI_METRICS[metric]]
        else:
            wanted = normalize_subject(subject)
            if not wanted:
                raise ValidationAppError("subject_key is required")
            row = next((r for r in rows if r["subject_key"] == wanted), None)
        if row is None:
            return self._result(
                None,
                "Not enough qualifying data in this selection.",
                insufficient=True,
                evidence={"min_deliveries": min_deliveries()},
            )

        # One client per AI question. A subject sent for a single client is compared with that
        # client's other subjects only; one shared by several clients is explained without the AI.
        owners = self.subjects.client_ids_in(replace(filters, search=None), subject=row["subject_key"])
        use_ai = len(owners) <= 1
        if use_ai and owners and (filters.client_ids is None or len(filters.client_ids) > 1):
            filters = replace(filters, client_ids=owners)
            rows = self.subjects.subject_rows(replace(filters, search=None))
            row = next((r for r in rows if r["subject_key"] == row["subject_key"]), row)
        contexts = self.subjects._contexts(replace(filters, search=None))
        stages = contexts.get(row["subject_key"], {}).get("stages", [])
        peer_rows = [
            r
            for r in rows
            if r["subject_key"] != row["subject_key"]
            and set(contexts.get(r["subject_key"], {}).get("stages", [])) & set(stages)
        ]
        peers = _peer_medians(peer_rows)
        evidence = {
            "metrics": _numbers(row),
            "reminder_stages": stages,
            "contexts": contexts.get(row["subject_key"], {}).get("contexts", []),
            "comparable_subjects": peers,
            "min_deliveries": min_deliveries(),
            "ai_context": "one_client" if use_ai else "not_used_several_clients",
        }
        if row["low_sample"]:
            text = (
                f"Insufficient delivery volume for a reliable assessment: {row['delivered']:,} delivered, "
                f"and at least {min_deliveries():,} are needed. The numbers so far are shown as they are."
            )
            return self._result(row, text, insufficient=True, evidence=evidence)

        facts = {
            "subject": row["subject"],
            "reminder_stages": stages,
            "metrics": _numbers(row),
            "typical_comparable_subject": peers,
            "comparable_subjects": [
                {"subject": r["subject"], **_numbers(r)} for r in peer_rows if not r["low_sample"]
            ][:8],
            "question": f"Why is this the top subject for {metric}?" if scope == "kpi" else "How is this subject doing?",
        }
        reply = self._ask(facts) if use_ai else None
        if reply:
            return self._result(
                row, reply["insight"], evidence=evidence, observations=reply.get("observations"), ai=True,
                cached=reply["cached"],
            )
        text = _rule_insight(row, peers)
        return self._result(row, text if use_ai else f"{text} {SEVERAL_CLIENTS_NOTE}", evidence=evidence)

    def _section_insight(self, rows: list[dict[str, Any]], *, use_ai: bool = True) -> dict[str, Any]:
        qualifying = sorted((r for r in rows if not r["low_sample"]), key=lambda r: -r["conversion"])
        evidence = {
            "subjects": len(rows),
            "subjects_with_enough_volume": len(qualifying),
            "typical_subject": _peer_medians(rows),
            "min_deliveries": min_deliveries(),
            "ai_context": "one_client" if use_ai else "not_used_several_clients",
        }
        if not qualifying:
            text = (
                f"Not enough data yet: no subject in this selection has {min_deliveries():,} delivered "
                "emails, so subjects cannot be compared reliably."
            )
            return self._result(None, text, insufficient=True, evidence=evidence)
        best, weakest = qualifying[0], qualifying[-1]
        evidence["best"] = {"subject": best["subject"], **_numbers(best)}
        evidence["weakest"] = {"subject": weakest["subject"], **_numbers(weakest)}
        reply = self._ask(
            {
                "subjects": [{"subject": r["subject"], **_numbers(r)} for r in qualifying[:15]],
                "typical_subject": evidence["typical_subject"],
                "subjects_without_enough_volume": len(rows) - len(qualifying),
                "question": "Summarise what is working and what is not across these subjects.",
            }
        ) if use_ai else None
        if reply:
            return self._result(
                None, reply["insight"], evidence=evidence, observations=reply.get("observations"), ai=True,
                cached=reply["cached"],
            )
        text = f'Best conversion: "{best["subject"]}" ({_line(best)}).'
        if weakest is not best:
            text += f' Weakest: "{weakest["subject"]}" ({_line(weakest)}).'
        if not use_ai:
            text += f" {SEVERAL_CLIENTS_NOTE}"
        return self._result(None, text, evidence=evidence)

    def _ask(self, facts: dict[str, Any]) -> dict[str, Any] | None:
        """The AI's explanation of `facts`, or None when it is unavailable (the caller has fixed wording).

        The same numbers asked again within EMAIL_INSIGHT_CACHE_MINUTES get the stored answer: any
        change in the data changes `facts`, so a stored answer is never about old numbers."""
        ttl = max(0, int(settings.EMAIL_INSIGHT_CACHE_MINUTES or 0)) * 60
        key = hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        stored = _INSIGHT_CACHE.get(key)
        if ttl and stored and time.monotonic() - stored[0] < ttl:
            return {**stored[1], "cached": True}
        try:
            reply = message_writer.call_llm(
                [
                    {"role": "system", "content": _INSIGHT_SYSTEM},
                    {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
                ],
                _INSIGHT_SCHEMA,
                quick=True,
            )
        except AppError as exc:
            logger.info("email subject insight: AI unavailable (%s)", exc.message)
            return None
        text = str((reply or {}).get("insight") or "").strip()
        if not text:
            return None
        observations = [str(item).strip() for item in (reply.get("observations") or []) if str(item).strip()]
        answer = {"insight": text, "observations": observations[:3]}
        if ttl:
            if len(_INSIGHT_CACHE) >= _INSIGHT_CACHE_MAX:
                _INSIGHT_CACHE.clear()
            _INSIGHT_CACHE[key] = (time.monotonic(), answer)
        return {**answer, "cached": False}

    @staticmethod
    def _result(
        row: dict[str, Any] | None,
        text: str,
        *,
        insufficient: bool = False,
        evidence: dict[str, Any] | None = None,
        observations: list[str] | None = None,
        ai: bool = False,
        cached: bool = False,
    ) -> dict[str, Any]:
        return {
            "cached": cached,
            "subject_key": row["subject_key"] if row else None,
            "subject": row["subject"] if row else None,
            "insight": text,
            "observations": observations or [],
            "insufficient_data": insufficient,
            "evidence": evidence or {},
            "generated_by": "ai" if ai else "rules",
            "disclaimer": DISCLAIMER,
        }

    # ------------------------------------------------------------------ subject suggestions

    def suggestions(
        self,
        user: UserModel,
        *,
        client_id: int,
        strategy_id: int | None = None,
        reminder_stage: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """Subjects for a workflow email step, from this client's history at the same stage and purpose."""
        self._assert_can(user, client_id, VIEW_WORKFLOWS)
        stage = (reminder_stage or "").strip() or "First reminder"
        if stage not in REMINDER_STAGES:
            raise ValidationAppError(f"reminder_stage must be one of: {', '.join(REMINDER_STAGES)}")
        purpose = (purpose or "").strip() or "Payment Reminder"
        history = self.subjects.history(client_id, strategy_id=strategy_id, reminder_stage=stage, purpose=purpose)
        rows = history["rows"][:3]
        suggestions = [
            {
                "subject": row["subject"],
                "explanation": "Reuses a historically successful subject in this reminder context."
                if index == 0
                else "Another measured subject from this client's history in this reminder context.",
                "measured": True,
                "source_subject": row["subject"],
                "metrics": _numbers(row),
                "low_sample": row["low_sample"],
                **self._best_body(history["filters"], row["subject"]),
            }
            for index, row in enumerate(rows)
        ]
        new = self._new_subjects(history["rows"], stage, purpose) if rows else []
        # The proven subject first, then the AI's new variations, then other measured subjects.
        suggestions = (suggestions[:1] + new + suggestions[1:])[:3]
        return {
            "client_id": client_id,
            "strategy_id": strategy_id,
            "reminder_stage": stage,
            "purpose": purpose,
            "scope": history["scope"],
            "rows": suggestions,
            "ai_generated": bool(new),
            "disclaimer": DISCLAIMER,
        }

    def _new_subjects(self, rows: list[dict[str, Any]], stage: str, purpose: str) -> list[dict[str, Any]]:
        """Up to two new subjects written by the AI from the measured ones ([] when it is unavailable)."""
        measured = [r for r in rows if not r["low_sample"]][:6]
        if not measured:
            return []
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["subjects"],
            "properties": {
                "subjects": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["subject", "explanation", "source_subject"],
                        "properties": {
                            "subject": {"type": "string"},
                            "explanation": {"type": "string"},
                            "source_subject": {"type": "string"},
                        },
                    },
                }
            },
        }
        system = (
            "You write email subject lines for a debt-collection workflow. You are given one client's "
            "measured subjects for a reminder stage and purpose (conversion = completed payments / delivered).\n"
            "Write 2 new subjects that build on what the best ones do. Polite and clear; never threaten, "
            "shame, or mention legal action, fees or deadlines. No amounts, dates or names: the only allowed "
            "variables are {{first_name}}, {{outstanding_balance}}, {{account_reference}}, {{brand_name}}.\n"
            'For each: "explanation" (one sentence: what it changes and why, citing the numbers given) and '
            '"source_subject" (the measured subject it builds on, copied exactly). Reply with JSON only.'
        )
        facts = {
            "reminder_stage": stage,
            "purpose": purpose,
            "measured_subjects": [{"subject": r["subject"], **_numbers(r)} for r in measured],
        }
        try:
            reply = message_writer.call_llm(
                [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
                schema,
                quick=True,
            )
        except AppError as exc:
            logger.info("email subject suggestions: AI unavailable (%s)", exc.message)
            return []
        by_subject = {r["subject"]: r for r in rows}
        result: list[dict[str, Any]] = []
        for item in (reply or {}).get("subjects") or []:
            if not isinstance(item, dict):
                continue
            subject = message_writer._fit_subject(str(item.get("subject") or ""))
            source = by_subject.get(normalize_subject(item.get("source_subject")))
            if not subject or subject in by_subject or any(s["subject"] == subject for s in result) or not source:
                continue
            result.append(
                {
                    "subject": subject,
                    "explanation": (str(item.get("explanation") or "").strip() or "A variation of a measured subject.")
                    + " This exact variation has not been measured.",
                    "measured": False,
                    "source_subject": source["subject"],
                    "metrics": _numbers(source),
                    "low_sample": source["low_sample"],
                    "body": None,  # a new subject has no measured body of its own
                    "body_origin": None,
                }
            )
        return result[:2]

    def _best_body(self, filters: SubjectFilters | None, subject: str) -> dict[str, Any]:
        """The body that did best under this subject (as written, placeholders kept)."""
        if filters is None:
            return {"body": None, "body_origin": None}
        bodies = [v for v in self.subjects._variations(filters, subject) if v.get("body")]
        if not bodies:
            return {"body": None, "body_origin": None}
        best = max(bodies, key=lambda v: (not v["low_sample"], v["conversion"], v["delivered"]))
        return {"body": best["body"], "body_origin": best["origin"]}

    def draft_email(
        self, user: UserModel, strategy_id: int, step_id: str, remarks: str | None = None
    ) -> dict[str, Any]:
        """A new subject and body for one email step, written from this client's measured results.
        A draft only: nothing is saved - the user decides whether to put it on the step."""
        strategy = self._strategy(user, strategy_id)
        self._assert_can(user, strategy.client_id, MANAGE_WORKFLOWS)
        steps = [s for s in strategy.steps or [] if isinstance(s, dict)]
        step = next((s for s in steps if s.get("id") == step_id), None)
        current = self._step_email(step) if step else None
        if current is None:
            raise ValidationAppError("This step is not an email step of the workflow")
        purpose = (step.get("config") or {}).get("purpose") or step.get("purpose") or "Payment Reminder"
        stage = reminder_stage(steps, step, purpose)
        history = self.subjects.history(
            strategy.client_id, strategy_id=strategy.id, reminder_stage=stage, purpose=purpose
        )
        examples = [r for r in history["rows"] if not r["low_sample"]][:3]
        evidence = {
            "reminder_stage": stage,
            "purpose": purpose,
            "scope": history["scope"],
            "examples": [{"subject": r["subject"], "metrics": _numbers(r)} for r in examples],
            "min_deliveries": min_deliveries(),
        }
        best = "; ".join(f'"{r["subject"]}" ({_line(r)})' for r in examples)
        request = (
            f"This client's best measured subjects for the same reminder stage and purpose: {best}. "
            "Build on what those do without copying them word for word."
            if best
            else "This client has no measured emails for this reminder stage and purpose yet."
        )
        if remarks and remarks.strip():
            request += f" The user also asked: {remarks.strip()}"
        written = self._rewrite(strategy, step, request)
        base = {
            "strategy_id": strategy.id,
            "step_id": step_id,
            "existing": {"subject": current[0], "body": current[1]},
            "evidence": evidence,
            "saved": False,
            "disclaimer": DISCLAIMER,
        }
        if written:
            return {
                **base,
                "subject": written.get("subject") or current[0],
                "body": written.get("body") or current[1],
                "origin": ORIGIN_AI,
                "generated_by": "ai",
                "explanation": (
                    f"Written by the AI in this step's tone, building on this client's measured results "
                    f"({history['scope'].lower()}): {best}."
                    if best
                    else "Written by the AI in this step's tone. This client has no measured emails for "
                    "this reminder stage and purpose yet, so no performance history was used."
                ),
            }
        proven = next((r for r in examples if self._best_body(history["filters"], r["subject"])["body"]), None)
        if proven is None:
            raise ServiceUnavailableError(
                "The AI writer is not available, and this client has no measured email for this "
                "reminder stage and purpose to suggest instead"
            )
        body = self._best_body(history["filters"], proven["subject"])
        return {
            **base,
            "subject": proven["subject"],
            "body": body["body"],
            "origin": body["body_origin"],
            "generated_by": "history",
            "explanation": (
                "The AI writer is not available: this is the client's best measured email for this "
                f"reminder stage and purpose ({_line(proven)})."
            ),
        }

    # ------------------------------------------------------------------ history for the message writer

    def draft_for_new_step(
        self,
        user: UserModel,
        *,
        client_id: int,
        purpose: str | None = None,
        reminder_stage: str | None = None,
        remarks: str | None = None,
    ) -> dict[str, Any]:
        """A subject and body for an email step of a workflow that is not saved yet, written the same
        way as any workflow email (tone by reminder stage, wording checks) from this client's
        measured results. A draft only: nothing is saved."""
        from app.modules.payflow.services.workflow_suggestion.context import (
            load_client_context,
            load_payment_options,
        )

        self._assert_can(user, client_id, MANAGE_WORKFLOWS)
        stage = (reminder_stage or "").strip() or "First reminder"
        if stage not in REMINDER_STAGES:
            raise ValidationAppError(f"reminder_stage must be one of: {', '.join(REMINDER_STAGES)}")
        purpose = (purpose or "").strip() or "Payment Reminder"
        history = self.subjects.history(client_id, reminder_stage=stage, purpose=purpose)
        examples = [r for r in history["rows"] if not r["low_sample"]][:3]
        best = "; ".join(f'"{r["subject"]}" ({_line(r)})' for r in examples)
        base = {
            "strategy_id": None,
            "step_id": "draft",
            "existing": {"subject": None, "body": None},
            "evidence": {
                "reminder_stage": stage,
                "purpose": purpose,
                "scope": history["scope"],
                "examples": [{"subject": r["subject"], "metrics": _numbers(r)} for r in examples],
                "min_deliveries": min_deliveries(),
            },
            "saved": False,
            "disclaimer": DISCLAIMER,
        }

        # The step's place decides its tone: a second / final reminder comes after earlier emails.
        position = {"Second reminder": 2, "Final reminder": 3}.get(stage, 1)
        steps = []
        for number in range(1, position + 1):
            last = number == position
            steps.append(
                {
                    "id": "draft" if last else f"earlier{number}",
                    "kind": "Communication",
                    "title": f"{stage} email" if last else f"Earlier reminder {number}",
                    "origin": "Human Created",
                    "config": {
                        "channel": "Email",
                        "purpose": purpose if last else "Payment Reminder",
                        "reference_event": "Previous Action",
                        "amount": 0 if number == 1 else 3,
                        "unit": "Days",
                        "direction": "After",
                    },
                    "channel": "Email",
                    "next": None if last else ("draft" if number + 1 == position else f"earlier{number + 1}"),
                }
            )
        workflow = {"name": "New workflow (draft)", "summary": "", "entry_node_id": steps[0]["id"], "steps": steps}
        request = (
            f"This client's best measured subjects for the same reminder stage and purpose: {best}. "
            "Build on what those do without copying them word for word."
            if best
            else "This client has no measured emails for this reminder stage and purpose yet."
        )
        if remarks and remarks.strip():
            request += f" The user also asked: {remarks.strip()}"
        written = None
        try:
            client = {
                **load_client_context(self.db, client_id),
                "payment_options": load_payment_options(self.db, client_id),
                "email_performance": self.performance_context(client_id),  # incl. reviewer decisions
            }
            written = message_writer.write_messages(
                client, workflow, {"segment": {}, "stats": {}}, step_ids=["draft"], remarks=request
            ).get("draft")
        except AppError as exc:
            logger.info("email draft for a new step: AI writer unavailable (%s)", exc.message)
        if written and written.get("subject") and written.get("body"):
            return {
                **base,
                "subject": written["subject"],
                "body": written["body"],
                "origin": ORIGIN_AI,
                "generated_by": "ai",
                "explanation": (
                    f"Written by the AI for a {stage.lower()}, building on this client's measured results "
                    f"({history['scope'].lower()}): {best}."
                    if best
                    else f"Written by the AI for a {stage.lower()}. This client has no measured emails for this "
                    "reminder stage and purpose yet, so no performance history was used."
                ),
            }
        proven = next((r for r in examples if self._best_body(history["filters"], r["subject"])["body"]), None)
        if proven is None:
            raise ServiceUnavailableError(
                "The AI writer is not available, and this client has no measured email for this "
                "reminder stage and purpose to suggest instead"
            )
        body = self._best_body(history["filters"], proven["subject"])
        return {
            **base,
            "subject": proven["subject"],
            "body": body["body"],
            "origin": body["body_origin"],
            "generated_by": "history",
            "explanation": (
                "The AI writer is not available: this is the client's best measured email for this "
                f"reminder stage and purpose ({_line(proven)})."
            ),
        }

    def performance_context(
        self,
        client_id: int,
        *,
        portfolio_id: int | None = None,
        strategy_id: int | None = None,
        audiences: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """This client's measured subjects, for the AI that writes workflow emails (None = no history).

        The closest history that has enough measured subjects (CONTEXT_ENOUGH_SUBJECTS): this workflow
        (`strategy_id`), then its portfolio, then the whole client - each for the recent months
        (CONTEXT_RECENT_DAYS) first, then all time. When none has enough, the closest one with any.
        `audiences`: audience keys ("age_65_plus", ...) - each one's own results in that same history (the
        emails its own version went to), under "by_audience"."""
        recent = datetime.now(timezone.utc) - timedelta(days=CONTEXT_RECENT_DAYS)
        base = SubjectFilters(client_ids=[client_id])
        scopes = []
        if strategy_id is not None:
            scopes.append(("this workflow", replace(base, strategy_id=strategy_id)))
        if portfolio_id is not None:
            scopes.append(("this portfolio", replace(base, portfolio_ids=(portfolio_id,))))
        scopes.append(("this client", base))
        chosen = fallback = None
        for scope, filters in scopes:
            for period, window in ((CONTEXT_RECENT_LABEL, replace(filters, start=recent)), ("all time", filters)):
                found = self._measured(window)
                if len(found) >= CONTEXT_ENOUGH_SUBJECTS:
                    chosen = (scope, period, window, found)
                    break
                if found and fallback is None:
                    fallback = (scope, period, window, found)
            if chosen:
                break
        if not (chosen or fallback):
            return None
        scope, period, window, measured = chosen or fallback

        def entry(item: dict[str, Any]) -> dict[str, Any]:
            subject, stage, purpose = item["group"]
            return {
                "subject": subject,
                "reminder_stage": stage,
                "purpose": purpose,
                "delivered": item["delivered"],
                "open_rate": _pct(item["open_rate"]),
                "click_rate": _pct(item["click_rate"]),
                "completed_payments": item["completed"],
                "conversion": _pct(item["conversion"]),
            }

        middle = median(i["conversion"] for i in measured)
        best = measured[:5]
        weak = [i for i in reversed(measured) if i not in best and i["conversion"] < middle][:3]
        by_audience = {}
        for key in audiences or []:
            own = self._measured(window, extra=[C.message_audience == key])
            if own:
                by_audience[key] = [entry(i) for i in own[:CONTEXT_PER_AUDIENCE]]
        return {
            "best": [entry(i) for i in best],
            "weak": [entry(i) for i in weak],
            "min_deliveries": min_deliveries(),
            "scope": scope,
            "period": period,
            "by_audience": by_audience,
            "decisions": self.decision_lessons(client_id),
        }

    def decision_lessons(self, client_id: int) -> list[str]:
        """This client's reviewer decisions on earlier AI proposals, newest first (at most MAX_DECISION_LESSONS):
        what was accepted, how a proposal was edited, what was rejected - with the reviewer's note. Text as
        written on workflows ({{placeholders}} kept) and the reviewer's own words: no customer data."""
        rows = (
            self.db.query(PayflowMessageProposalModel)
            .filter(
                PayflowMessageProposalModel.client_id == client_id,
                PayflowMessageProposalModel.status.in_([STATUS_ACCEPTED, STATUS_EDITED, STATUS_REJECTED]),
                PayflowMessageProposalModel.proposed_subject.isnot(None),
            )
            .order_by(PayflowMessageProposalModel.decided_at.desc(), PayflowMessageProposalModel.id.desc())
            .limit(MAX_DECISION_LESSONS)
            .all()
        )
        lessons = []
        for row in rows:
            evidence = row.evidence or {}
            where = " · ".join(part for part in (evidence.get("reminder_stage"), evidence.get("purpose")) if part)
            if row.status == STATUS_ACCEPTED:
                what = f'ACCEPTED: "{row.proposed_subject}" instead of "{row.existing_subject}"'
            elif row.status == STATUS_REJECTED:
                what = f'REJECTED: "{row.proposed_subject}" (the reviewer kept "{row.existing_subject}")'
            elif row.final_subject == row.proposed_subject:
                what = f'EDITED: the reviewer kept the subject "{row.proposed_subject}" and rewrote the body'
            else:
                what = f'EDITED: the AI proposed "{row.proposed_subject}"; the reviewer used "{row.final_subject}"'
            note = f" - reviewer's note: {row.decision_note[:300]}" if row.decision_note else ""
            lessons.append(f"- {f'[{where}] ' if where else ''}{what}{note}")
        return lessons

    def _measured(self, filters: SubjectFilters, *, extra: list[Any] | None = None) -> list[dict[str, Any]]:
        """Subjects (per reminder stage and purpose) with enough delivered emails, best conversion first."""
        items = self.subjects.aggregate(filters, [C.subject_key, C.reminder_stage, C.purpose], extra=extra)
        return sorted((i for i in items if not i["low_sample"]), key=lambda i: -i["conversion"])

    # ------------------------------------------------------------------ low performers (proposals)

    def _strategy(self, user: UserModel, strategy_id: int) -> PayflowStrategyModel:
        row = self.db.get(PayflowStrategyModel, strategy_id)
        if row is None:
            raise NotFoundError("Workflow not found")
        self._assert_can(user, row.client_id, VIEW_WORKFLOWS)
        return row

    def _assert_can(self, user: UserModel, client_id: int, permission: str) -> None:
        """The user is assigned to the client and holds `permission` for it."""
        if self.access.is_operations_admin(user):
            return
        if not self.access.can_see_client(user, client_id):
            raise ForbiddenError("Client access denied")
        if not self.access.can(user, permission, client_id):
            raise ForbiddenError(f"Permission required: {permission}")

    def _assert_manage(self, user: UserModel, client_id: int) -> None:
        self._assert_can(user, client_id, MANAGE_WORKFLOWS)

    @staticmethod
    def _step_email(step: dict[str, Any]) -> tuple[str, str] | None:
        """(subject, body) the step sends by email, as written; None when it is not an email step."""
        from app.modules.payflow.services.strategy_service import MESSAGE_TEMPLATES
        from app.modules.payflow.services.workflow_contact_runtime import is_contact_step, template_for

        if step.get("kind") != "Communication" or step.get("disabled"):
            return None
        cfg = step.get("config") or {}
        contact = is_contact_step(cfg, step)
        channel = str(cfg.get("channel") or step.get("channel") or "").strip()
        if not contact and channel != "Email":
            return None
        purpose = cfg.get("purpose") or step.get("purpose") or "Payment Reminder"
        template = None
        if not contact:
            template = next((t for t in MESSAGE_TEMPLATES if t.get("id") == cfg.get("template_id")), None)
        template = template or template_for(purpose, "Email") or {}
        return (
            str(cfg.get("subject") or template.get("subject") or purpose),
            str(cfg.get("body") or template.get("body") or ""),
        )

    @staticmethod
    def _classify(row: dict[str, Any] | None, peers: dict[str, float] | None) -> tuple[str, str]:
        """(recommendation, reason) for a step's results against comparable templates."""
        if row is None or row["low_sample"]:
            delivered = row["delivered"] if row else 0
            return (
                RECOMMEND_INSUFFICIENT,
                f"Only {delivered:,} delivered emails with this text: at least {min_deliveries():,} are "
                "needed before it can be judged.",
            )
        if peers is None:
            return (
                RECOMMEND_RETAIN,
                "There are no comparable templates with enough delivered emails for this client, reminder "
                "stage and purpose, so there is nothing to show it is under-performing.",
            )
        typical = f"typical comparable template: {_pct(peers['open_rate'])} estimated opens, " \
                  f"{_pct(peers['click_rate'])} clicks, {_pct(peers['conversion'])} conversion"
        if row["conversion"] >= peers["conversion"] * 0.9:
            return RECOMMEND_RETAIN, f"Conversion {_pct(row['conversion'])} is in line with or above the {typical}."
        if row["conversion"] < peers["conversion"] * 0.5:
            return (
                RECOMMEND_REPLACE,
                f"Conversion {_pct(row['conversion'])} is less than half of the {typical}.",
            )
        if row["open_rate"] < peers["open_rate"] * 0.8:
            return (
                RECOMMEND_TEST,
                f"Estimated opens {_pct(row['open_rate'])} are well below the {typical}: the subject is the "
                "likely weak point.",
            )
        return (
            RECOMMEND_REVISE,
            f"Opens are comparable but clicks {_pct(row['click_rate'])} and conversion "
            f"{_pct(row['conversion'])} trail the {typical}: the body or its call to action is the likely weak point.",
        )

    def _assess(
        self, strategy: PayflowStrategyModel, steps: list[dict[str, Any]], step: dict[str, Any]
    ) -> dict[str, Any] | None:
        """One email step measured against the client's comparable templates (same reminder stage
        and purpose). Numbers and rules only: no AI, nothing saved. None when it is not an email step."""
        email = self._step_email(step)
        if email is None:
            return None
        subject, body = email
        cfg = step.get("config") or {}
        purpose = cfg.get("purpose") or step.get("purpose") or "Payment Reminder"
        stage = reminder_stage(steps, step, purpose)
        key = subject_key(subject)
        client_filters = SubjectFilters(client_ids=[strategy.client_id])
        own = self.subjects.aggregate(
            replace(client_filters, strategy_id=strategy.id),
            [C.subject_key],
            extra=[C.step_id == step.get("id"), C.subject_key == key],
        )
        row = own[0] if own else None
        peer_rows = [
            r
            for r in self.subjects.subject_rows(replace(client_filters, reminder_stage=stage, purpose=purpose))
            if r["subject_key"] != key
        ]
        peers = _peer_medians(peer_rows)
        recommendation, reason = self._classify(row, peers)
        if recommendation in WEAK_RECOMMENDATIONS:
            recommendation, reason = self._confirm_across_periods(
                client_filters, strategy, step, key, stage, purpose, recommendation, reason
            )
        return {
            "subject": subject,
            "body": body,
            "cfg": cfg,
            "purpose": purpose,
            "stage": stage,
            "row": row,
            "peers": peers,
            "recommendation": recommendation,
            "reason": reason,
            "examples": sorted((r for r in peer_rows if not r["low_sample"]), key=lambda r: -r["conversion"])[:3],
        }

    def _confirm_across_periods(
        self,
        client_filters: SubjectFilters,
        strategy: PayflowStrategyModel,
        step: dict[str, Any],
        key: str | None,
        stage: str,
        purpose: str,
        recommendation: str,
        reason: str,
    ) -> tuple[str, str]:
        """A weak finding holds only when the email is weak in every period that has enough of its emails:
        the last CONFIRM_DAYS days and the time before. With only one such period the finding stands as it is
        (min_deliveries already keeps single low-volume results out). Weak in one period but in line in the
        other → Retain: one bad stretch is not enough to replace a template."""
        split = datetime.now(timezone.utc) - timedelta(days=CONFIRM_DAYS)
        verdicts = []
        for label, window in ((f"in the last {CONFIRM_DAYS} days", {"start": split}), ("before that", {"end": split})):
            own = self.subjects.aggregate(
                replace(client_filters, strategy_id=strategy.id, **window),
                [C.subject_key],
                extra=[C.step_id == step.get("id"), C.subject_key == key],
            )
            row = own[0] if own else None
            if row is None or row["low_sample"]:
                continue
            peers = _peer_medians(
                [
                    r
                    for r in self.subjects.subject_rows(
                        replace(client_filters, reminder_stage=stage, purpose=purpose, **window)
                    )
                    if r["subject_key"] != key
                ]
            )
            if peers is not None:
                verdicts.append((label, self._classify(row, peers)[0]))
        if len(verdicts) < 2:
            return recommendation, reason
        fine = [label for label, verdict in verdicts if verdict not in WEAK_RECOMMENDATIONS]
        if fine:
            return (
                RECOMMEND_RETAIN,
                f"{reason} But it is not weak in every period: it was in line with comparable templates "
                f"{' and '.join(fine)}. It is kept, and checked again as new results come in.",
            )
        return recommendation, f"{reason} It was weak both in the last {CONFIRM_DAYS} days and before that."

    def analyze_workflow(self, user: UserModel, strategy_id: int) -> dict[str, Any]:
        """Compare each email step of a workflow with the client's comparable templates and store a
        proposal per step. The workflow itself is not changed."""
        strategy = self._strategy(user, strategy_id)
        self._assert_manage(user, strategy.client_id)
        created = self._analyze(strategy, created_by=user.full_name, automatic=False)
        self.db.commit()
        return {
            "strategy_id": strategy.id,
            "strategy_name": strategy.name,
            "proposals": [self._serialize(p, strategy.name) for p in created],
            "disclaimer": DISCLAIMER,
        }

    def _already_raised(self, strategy_id: int, step_id: str, subject: str, body: str, recommendation: str) -> bool:
        """The automatic check already raised this finding for this exact text recently (whatever
        was decided about it): it is not raised again."""
        since = datetime.now(timezone.utc) - timedelta(days=REPEAT_AFTER_DAYS)
        return (
            self.db.query(PayflowMessageProposalModel.id)
            .filter(
                PayflowMessageProposalModel.strategy_id == strategy_id,
                PayflowMessageProposalModel.step_id == step_id,
                PayflowMessageProposalModel.recommendation == recommendation,
                PayflowMessageProposalModel.existing_subject == subject[:512],
                PayflowMessageProposalModel.existing_body == body,
                PayflowMessageProposalModel.created_at >= since,
            )
            .first()
            is not None
        )

    def _analyze(
        self, strategy: PayflowStrategyModel, *, created_by: str, automatic: bool
    ) -> list[PayflowMessageProposalModel]:
        """`automatic`: the worker's own check - only under-performing steps get a proposal, and a
        finding already raised for the same text is not repeated."""
        steps = [s for s in strategy.steps or [] if isinstance(s, dict)]
        created: list[PayflowMessageProposalModel] = []
        for step in steps:
            found = self._assess(strategy, steps, step)
            if found is None:
                continue
            subject, body, cfg = found["subject"], found["body"], found["cfg"]
            purpose, stage, row, peers = found["purpose"], found["stage"], found["row"], found["peers"]
            recommendation, reason, examples = found["recommendation"], found["reason"], found["examples"]
            if automatic and (
                recommendation not in WEAK_RECOMMENDATIONS
                or self._already_raised(strategy.id, str(step.get("id")), subject, body, recommendation)
            ):
                continue
            proposed_subject = proposed_body = None
            proposed_by = None  # "ai" (written now) | "history" (the client's best measured subject)
            explanation = reason
            if recommendation in WEAK_RECOMMENDATIONS:
                written = self._rewrite(strategy, step, self._rewrite_request(recommendation, reason, examples))
                if written:
                    proposed_by = "ai"
                    proposed_subject = written.get("subject") or subject
                    # "Test alternative subjects" keeps the body: only the subject is in question.
                    proposed_body = body if recommendation == RECOMMEND_TEST else (written.get("body") or body)
                elif examples:
                    proposed_by = "history"
                    proposed_subject, proposed_body = examples[0]["subject"], body
                    explanation += " The AI writer was not available: the client's best measured subject is proposed."
                else:
                    explanation += " The AI writer was not available, so no new text is proposed yet."

            # One open proposal per step: an older undecided one is replaced by this analysis.
            self.db.query(PayflowMessageProposalModel).filter(
                PayflowMessageProposalModel.strategy_id == strategy.id,
                PayflowMessageProposalModel.step_id == step.get("id"),
                PayflowMessageProposalModel.status == STATUS_PENDING,
            ).update({"status": STATUS_SUPERSEDED}, synchronize_session=False)
            proposal = PayflowMessageProposalModel(
                client_id=strategy.client_id,
                strategy_id=strategy.id,
                step_id=str(step.get("id")),
                step_title=step.get("title"),
                recommendation=recommendation,
                status=STATUS_PENDING,
                existing_subject=subject[:512],
                existing_body=body,
                proposed_subject=(proposed_subject or None) and proposed_subject[:512],
                proposed_body=proposed_body,
                alternative_subjects=[{"subject": r["subject"], "metrics": _numbers(r)} for r in examples],
                explanation=explanation,
                evidence={
                    "reminder_stage": stage,
                    "purpose": purpose,
                    "step": _numbers(row) if row else None,
                    "comparable_templates": peers,
                    "scope": "Same client, reminder stage and purpose",
                    "min_deliveries": min_deliveries(),
                    "proposed_by": proposed_by,
                    "existing_origin": cfg.get("content_origin")
                    or (ORIGIN_HUMAN if (cfg.get("subject") or cfg.get("body")) else ORIGIN_TEMPLATE),
                },
                created_by=created_by,
            )
            self.db.add(proposal)
            created.append(proposal)
        self.db.flush()
        return created

    # ------------------------------------------------------------------ finding low performers

    def low_performers(
        self, user: UserModel, *, client_id: int | None = None, include_all: bool = False
    ) -> dict[str, Any]:
        """Every email step of the Active workflows the user can see, measured against comparable
        templates. By default only the under-performing ones. Read-only: no AI, nothing saved."""
        allowed = self.subjects._visible_client_ids(user, VIEW_WORKFLOWS)
        query = self.db.query(PayflowStrategyModel).filter(PayflowStrategyModel.status == "Active")
        if client_id is not None:
            self._assert_can(user, client_id, VIEW_WORKFLOWS)
            query = query.filter(PayflowStrategyModel.client_id == client_id)
        elif allowed is not None:
            query = query.filter(PayflowStrategyModel.client_id.in_(allowed))
        strategies = query.order_by(PayflowStrategyModel.name.asc()).all()
        names = {
            c.id: c.name
            for c in self.db.query(PayflowClientModel).filter(
                PayflowClientModel.id.in_({s.client_id for s in strategies} or {0})
            )
        }
        pending = {
            (p.strategy_id, p.step_id): p.id
            for p in self.db.query(PayflowMessageProposalModel).filter(
                PayflowMessageProposalModel.status == STATUS_PENDING,
                PayflowMessageProposalModel.strategy_id.in_({s.id for s in strategies} or {0}),
            )
        }
        items = []
        for strategy in strategies:
            steps = [s for s in strategy.steps or [] if isinstance(s, dict)]
            for step in steps:
                found = self._assess(strategy, steps, step)
                if found is None or (not include_all and found["recommendation"] not in WEAK_RECOMMENDATIONS):
                    continue
                items.append(
                    {
                        "client_id": strategy.client_id,
                        "client_name": names.get(strategy.client_id),
                        "strategy_id": strategy.id,
                        "strategy_name": strategy.name,
                        "step_id": str(step.get("id")),
                        "step_title": step.get("title"),
                        "subject": found["subject"],
                        "reminder_stage": found["stage"],
                        "purpose": found["purpose"],
                        "recommendation": found["recommendation"],
                        "reason": found["reason"],
                        "metrics": _numbers(found["row"]) if found["row"] else None,
                        "comparable_templates": found["peers"],
                        "pending_proposal_id": pending.get((strategy.id, str(step.get("id")))),
                    }
                )
        order = {RECOMMEND_REPLACE: 0, RECOMMEND_TEST: 1, RECOMMEND_REVISE: 2}
        items.sort(key=lambda i: (order.get(i["recommendation"], 9), i["strategy_name"] or ""))
        return {"items": items, "total": len(items), "min_deliveries": min_deliveries()}

    def scan_low_performers(self) -> dict[str, int]:
        """The worker's own check of every Active workflow: an under-performing email step gets a
        proposal for review, and the client's supervisors are told. Workflows are never changed."""
        from app.modules.payflow.services.notification_service import PayflowNotificationService
        from app.shared.enums import PayflowNotificationType

        result = {"workflows": 0, "proposals": 0}
        strategy_ids = [
            row[0]
            for row in self.db.query(PayflowStrategyModel.id).filter(PayflowStrategyModel.status == "Active").all()
        ]
        for strategy_id in strategy_ids:
            try:
                strategy = self.db.get(PayflowStrategyModel, strategy_id)
                created = self._analyze(strategy, created_by=AUTOMATIC_AUTHOR, automatic=True)
                if not created:
                    self.db.rollback()
                    continue
                notifications = PayflowNotificationService(self.db)
                notifications.notify(
                    users=notifications.recipients_for_client(strategy.client_id),
                    notification_type=PayflowNotificationType.EMAIL_PROPOSALS_READY.value,
                    title=f"Email improvements proposed · {strategy.name}",
                    body=(
                        f"{len(created)} email step(s) of this workflow under-perform comparable templates. "
                        "AI proposals are waiting for your decision; the workflow has not been changed."
                    ),
                    link=f"/payflow/workflows/{strategy.id}",
                    client_id=strategy.client_id,
                    entity_type="email_proposal",
                    entity_id=str(created[0].id),
                )
                self.db.commit()
                result["workflows"] += 1
                result["proposals"] += len(created)
            except Exception:  # noqa: BLE001 - one workflow's failure never stops the others
                self.db.rollback()
                logger.exception("email optimisation check failed for workflow %s", strategy_id)
        return result

    @staticmethod
    def _rewrite_request(recommendation: str, reason: str, examples: list[dict[str, Any]]) -> str:
        best = "; ".join(f'"{r["subject"]}" ({_line(r)})' for r in examples) or "none with enough volume"
        focus = {
            RECOMMEND_TEST: "Write a different subject; keep the body's message.",
            RECOMMEND_REVISE: "Keep the subject close to what it is; rewrite the body so the next step is clearer.",
            RECOMMEND_REPLACE: "Write a new subject and a new body.",
        }[recommendation]
        return (
            f"This email under-performs. {reason} {focus} "
            f"This client's best measured subjects for the same reminder stage and purpose: {best}. "
            "Build on what those do without copying them word for word."
        )

    def _rewrite(
        self, strategy: PayflowStrategyModel, step: dict[str, Any], remarks: str
    ) -> dict[str, Any] | None:
        """New text for the step from the workflow's own message writer (same tone, wording and
        variable checks as any AI-written message). None when the writer is unavailable."""
        from app.modules.payflow.services.strategy_service import PayflowStrategyService
        from app.modules.payflow.services.workflow_suggestion.context import (
            load_client_context,
            load_payment_options,
        )
        from app.modules.payflow.services.workflow_suggestion.service import _stored_segment

        try:
            workflow = PayflowStrategyService(self.db)._serialize(strategy)
            client = {
                **load_client_context(self.db, strategy.client_id),
                "payment_options": load_payment_options(self.db, strategy.client_id),
                # Its measured results and the reviewers' decisions on earlier proposals (feedback).
                "email_performance": self.performance_context(
                    strategy.client_id, portfolio_id=strategy.portfolio_id, strategy_id=strategy.id
                ),
            }
            written = message_writer.write_messages(
                client, workflow, _stored_segment(workflow), step_ids=[step["id"]], remarks=remarks
            )
            return written.get(step["id"])
        except AppError as exc:
            logger.info("email optimisation: AI writer unavailable for step %s (%s)", step.get("id"), exc.message)
            return None

    def _serialize(self, row: PayflowMessageProposalModel, strategy_name: str | None = None) -> dict[str, Any]:
        if strategy_name is None:
            strategy = self.db.get(PayflowStrategyModel, row.strategy_id)
            strategy_name = strategy.name if strategy else None
        return {
            "id": row.id,
            "client_id": row.client_id,
            "strategy_id": row.strategy_id,
            "strategy_name": strategy_name,
            "step_id": row.step_id,
            "step_title": row.step_title,
            "recommendation": row.recommendation,
            "status": row.status,
            "existing": {
                "subject": row.existing_subject,
                "body": row.existing_body,
                "origin": (row.evidence or {}).get("existing_origin"),
            },
            "proposed": {
                "subject": row.proposed_subject,
                "body": row.proposed_body,
                # Not the AI's writing when it was unavailable and a measured subject was proposed.
                "origin": ORIGIN_MEASURED if (row.evidence or {}).get("proposed_by") == "history" else ORIGIN_AI,
            }
            if row.proposed_subject or row.proposed_body
            else None,
            "alternative_subjects": list(row.alternative_subjects or []),
            "explanation": row.explanation,
            "evidence": dict(row.evidence or {}),
            "final": {"subject": row.final_subject, "body": row.final_body}
            if row.status in {STATUS_ACCEPTED, STATUS_EDITED}
            else None,
            "decision_note": row.decision_note,
            "created_by": row.created_by,
            "created_at": row.created_at,
            "decided_by": row.decided_by,
            "decided_at": row.decided_at,
            "result": self._since_decision(row),
        }

    def _since_decision(self, row: PayflowMessageProposalModel) -> dict[str, Any] | None:
        """For an accepted / edited proposal: how the text it put on the workflow has done since - its own
        message version, found by the same text fingerprint every sent email is recorded with. None before a
        decision. "Not sent yet" → "Measuring" (fewer than min_deliveries delivered) → "Measured"."""
        if row.status not in {STATUS_ACCEPTED, STATUS_EDITED} or not (row.final_subject or row.final_body):
            return None
        version = (
            self.db.query(PayflowMessageVersionModel)
            .filter(
                PayflowMessageVersionModel.client_id == row.client_id,
                PayflowMessageVersionModel.content_hash == content_hash("Email", row.final_subject, row.final_body),
            )
            .first()
        )
        if version is None:
            return {"status": "Not sent yet", "message_version_id": None, "metrics": None}
        items = self.subjects.aggregate(
            SubjectFilters(client_ids=[row.client_id]),
            [C.message_version_id],
            extra=[C.message_version_id == version.id],
        )
        if not items:
            return {"status": "Not sent yet", "message_version_id": version.id, "metrics": None}
        return {
            "status": "Measuring" if items[0]["low_sample"] else "Measured",
            "message_version_id": version.id,
            "metrics": _numbers(items[0]),
        }

    def list_proposals(
        self,
        user: UserModel,
        *,
        strategy_id: int | None = None,
        client_id: int | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        query = self.db.query(PayflowMessageProposalModel)
        allowed = self.subjects._visible_client_ids(user, VIEW_WORKFLOWS)
        if allowed is not None:
            query = query.filter(PayflowMessageProposalModel.client_id.in_(allowed))
        if client_id is not None:
            query = query.filter(PayflowMessageProposalModel.client_id == client_id)
        if strategy_id is not None:
            query = query.filter(PayflowMessageProposalModel.strategy_id == strategy_id)
        if status:
            query = query.filter(PayflowMessageProposalModel.status == status)
        else:
            query = query.filter(PayflowMessageProposalModel.status != STATUS_SUPERSEDED)
        rows = query.order_by(PayflowMessageProposalModel.id.desc()).limit(200).all()
        items = [self._serialize(row) for row in rows]
        return {
            "items": items,
            "total": len(items),
            "pending": sum(1 for item in items if item["status"] == STATUS_PENDING),
        }

    def decide(
        self,
        user: UserModel,
        proposal_id: int,
        *,
        action: str,
        subject: str | None = None,
        body: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Accept (the proposal as written), edit (the user's own version of it) or reject.

        Accept / edit puts the text on the workflow through the normal workflow update: an Active or
        Approved workflow is archived as a version and goes back to review before it can run again.
        """
        proposal = self.db.get(PayflowMessageProposalModel, proposal_id)
        if proposal is None:
            raise NotFoundError("Proposal not found")
        strategy = self._strategy(user, proposal.strategy_id)
        self._assert_manage(user, strategy.client_id)
        if proposal.status != STATUS_PENDING:
            raise ValidationAppError(f"This proposal was already {proposal.status.lower()}")
        action = (action or "").strip().lower()
        if action not in {"accept", "edit", "reject"}:
            raise ValidationAppError("action must be accept, edit or reject")

        now = datetime.now(timezone.utc)
        if action == "reject":
            proposal.status = STATUS_REJECTED
        else:
            if action == "accept":
                if not (proposal.proposed_subject or proposal.proposed_body):
                    raise ValidationAppError("This proposal has no new text to accept")
                final_subject, final_body = proposal.proposed_subject, proposal.proposed_body
            else:
                final_subject = normalize_subject(subject) or proposal.proposed_subject or proposal.existing_subject
                final_body = (body or "").strip() or proposal.proposed_body or proposal.existing_body
            unchanged = (final_subject, final_body) == (proposal.proposed_subject, proposal.proposed_body)
            steps = copy.deepcopy(list(strategy.steps or []))
            step = next((s for s in steps if s.get("id") == proposal.step_id), None)
            if step is None:
                raise ValidationAppError("The workflow no longer has this step")
            current = self._step_email(step)
            if current is None or current != (proposal.existing_subject, proposal.existing_body or ""):
                raise ValidationAppError(
                    "The step's text changed after this proposal was made - run the analysis again"
                )
            # The new text may not bring in a wording problem the current text does not have
            # (a threat, a typed amount / date / phone number, a wrong variable, a broken subject, ...).
            before = set(self._wording_problems(strategy, step, proposal.existing_subject, proposal.existing_body))
            new_problems = [
                problem
                for problem in self._wording_problems(strategy, step, final_subject, final_body)
                if problem not in before
            ]
            if new_problems:
                raise ValidationAppError(
                    "This text does not pass the message checks: " + "; ".join(new_problems[:5])
                )
            step["config"] = {**(step.get("config") or {}), "subject": final_subject, "body": final_body}
            from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService

            payload: dict[str, Any] = {
                "steps": steps,
                "note": f"Email text from AI proposal #{proposal.id} ({proposal.recommendation})"
                + (f": {note.strip()}" if note and note.strip() else ""),
            }
            if unchanged and (proposal.evidence or {}).get("proposed_by") != "history":
                payload["_ai_step_ids"] = [proposal.step_id]  # the AI's own words, accepted as written
            WorkflowSuggestionService(self.db).update_workflow(user, strategy.id, payload)
            if not unchanged:
                self._mark_edited(strategy.id, proposal.step_id)
            proposal = self.db.get(PayflowMessageProposalModel, proposal_id)
            proposal.status = STATUS_ACCEPTED if unchanged else STATUS_EDITED
            proposal.final_subject = final_subject
            proposal.final_body = final_body
        proposal.decision_note = (note or "").strip() or None
        proposal.decided_by = user.full_name
        proposal.decided_at = now
        self.db.commit()
        self.db.refresh(proposal)
        return self._serialize(proposal)

    def _wording_problems(
        self, strategy: PayflowStrategyModel, step: dict[str, Any], subject: str | None, body: str | None
    ) -> list[str]:
        """What the message writer's own checks say about this email text, in the step's tone
        ([] when it is fine - or when the checks cannot be run, which never blocks a decision)."""
        from app.modules.payflow.services.strategy_service import PayflowStrategyService
        from app.modules.payflow.services.workflow_suggestion.context import load_client_context
        from app.modules.payflow.services.workflow_suggestion.message_validator import check_message
        from app.modules.payflow.services.workflow_suggestion.service import _stored_segment

        try:
            workflow = PayflowStrategyService(self.db)._serialize(strategy)
            segment = _stored_segment(workflow)
            tones = message_writer.message_tones(
                workflow, segment.get("segment") or {}, (segment.get("stats") or {}).get("avg_days_past_due")
            )
            # A step the workflow's path does not reach has no decided tone: the general checks
            # (threats, typed amounts / numbers, variables) still apply, as for a Clear message.
            tone = tones.get(step.get("id")) or {"tone": "Clear", "why": "", "path": ""}
            language = load_client_context(self.db, strategy.client_id).get("language") or "English"
            brief = {
                **message_writer._brief(step, tone, segment, language.strip().lower().startswith("english")),
                "channel": "Email",
                "step_id": step.get("title") or step.get("id"),
            }
            return check_message({"subject": subject or "", "body": body or ""}, brief, [])
        except Exception:  # noqa: BLE001
            logger.exception("message checks could not be run for workflow %s", strategy.id)
            return []

    def _mark_edited(self, strategy_id: int, step_id: str) -> None:
        """A proposal the user changed before accepting is the AI's text edited by a person."""
        strategy = self.db.get(PayflowStrategyModel, strategy_id)
        steps = copy.deepcopy(list(strategy.steps or []))
        for step in steps:
            if step.get("id") == step_id:
                step["config"] = {**(step.get("config") or {}), "content_origin": ORIGIN_AI_EDITED}
        strategy.steps = steps
        self.db.flush()

    # ------------------------------------------------------------------ text history of a workflow

    def message_history(self, user: UserModel, strategy_id: int, step_id: str | None = None) -> dict[str, Any]:
        """Every distinct text this workflow has sent, newest first, each with its own results:
        an old version keeps its history after the workflow's text is changed."""
        strategy = self._strategy(user, strategy_id)
        filters = SubjectFilters(client_ids=[strategy.client_id], strategy_id=strategy.id)
        extra = [C.step_id == step_id] if step_id else []
        metrics = {
            item["group"][0]: item
            for item in self.subjects.aggregate(filters, [C.message_version_id], extra=extra)
            if item["group"][0] is not None
        }
        versions = (
            self.db.query(PayflowMessageVersionModel)
            .filter(PayflowMessageVersionModel.id.in_(set(metrics)))
            .order_by(PayflowMessageVersionModel.first_used_at.desc(), PayflowMessageVersionModel.id.desc())
            .all()
            if metrics
            else []
        )
        current = {
            s.get("id"): subject_key((self._step_email(s) or ("", ""))[0])
            for s in strategy.steps or []
            if isinstance(s, dict)
        }
        items = []
        for version in versions:
            item = metrics[version.id]
            item.pop("group", None)
            item.pop("variations", None)
            items.append(
                {
                    "id": version.id,
                    "step_id": version.step_id,
                    "strategy_version": version.strategy_version,
                    "reminder_stage": version.reminder_stage,
                    "purpose": version.purpose,
                    "language": version.language,
                    "origin": version.origin,
                    "audience": version.audience,
                    "audience_label": audience_label(version.audience),
                    "subject": version.subject_template,
                    "body": version.body_template,
                    "first_used_at": version.first_used_at,
                    "last_used_at": version.last_used_at,
                    "is_current_subject": current.get(version.step_id) == version.subject_key,
                    **item,
                }
            )
        return {"strategy_id": strategy.id, "strategy_name": strategy.name, "items": items, "total": len(items)}
