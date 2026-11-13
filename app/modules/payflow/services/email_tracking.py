"""What is recorded on each communication so email subjects can be measured later.

- the subject and body as written on the workflow step (placeholders kept, no customer values),
- which distinct text it was (PayflowMessageVersionModel: the history of what was sent),
- its workflow step, workflow version, portfolio, language and reminder stage.

Tracking never blocks a send: callers wrap `apply_tracking` so a failure only loses the tracking.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.infrastructure.database.models import PayflowCommunicationModel, PayflowMessageVersionModel

logger = logging.getLogger("app.comms")

REMINDER_STAGES = [
    "First reminder",
    "Second reminder",
    "Final reminder",
    "Payment plan",
    "Promise to pay",
]
# A message purpose that is its own stage, whatever the step's place in the workflow.
STAGE_BY_PURPOSE = {
    "Payment Plan Reminder": "Payment plan",
    "Promise-to-Pay Reminder": "Promise to pay",
    "Firm Reminder": "Final reminder",
}

ORIGIN_AI = "AI Generated"
ORIGIN_AI_EDITED = "AI Generated, Human Edited"
ORIGIN_HUMAN = "Human Created"
ORIGIN_TEMPLATE = "System Template"

MESSAGE_TEXT_KEYS = ("subject", "body", "sms_body")


def normalize_subject(subject: str | None) -> str:
    """Trimmed, with runs of whitespace collapsed: the form subjects are grouped and compared by."""
    return " ".join(str(subject or "").split())


def subject_key(subject: str | None) -> str | None:
    return normalize_subject(subject)[:512] or None


def reminder_stage(steps: list[dict[str, Any]] | None, step: dict[str, Any], purpose: str | None) -> str:
    """The step's reminder stage: the one set on it, else from its purpose, else from its place
    among the workflow's message steps (first, a later one, the last of three or more)."""
    cfg = step.get("config") or {}
    explicit = str(cfg.get("reminder_stage") or cfg.get("reminderStage") or "").strip()
    if explicit in REMINDER_STAGES:
        return explicit
    by_purpose = STAGE_BY_PURPOSE.get(str(purpose or cfg.get("purpose") or "").strip())
    if by_purpose:
        return by_purpose
    messages = [
        s.get("id")
        for s in steps or []
        if isinstance(s, dict) and s.get("kind") == "Communication" and not s.get("disabled")
    ]
    if step.get("id") not in messages or messages.index(step.get("id")) == 0:
        return "First reminder"
    position = messages.index(step.get("id"))
    return "Final reminder" if len(messages) >= 3 and position == len(messages) - 1 else "Second reminder"


def stamp_content_origin(
    before: list[dict[str, Any]], after: list[dict[str, Any]], *, ai_step_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    """Mark who wrote each message step's text (config.content_origin) when a person saves steps.

    Text unchanged → the stored mark stays. Text changed → "AI Generated, Human Edited" when it was
    the AI's, else "Human Created". `ai_step_ids`: steps whose new text is the AI's own proposal,
    accepted as written.
    """
    stored = {step.get("id"): step for step in before}
    result = []
    for step in after:
        if step.get("kind") != "Communication":
            result.append(step)
            continue
        cfg = dict(step.get("config") or {})
        old_cfg = (stored.get(step.get("id")) or {}).get("config") or {}
        old_origin = old_cfg.get("content_origin")
        new_text = tuple(cfg.get(key) or "" for key in MESSAGE_TEXT_KEYS)
        old_text = tuple(old_cfg.get(key) or "" for key in MESSAGE_TEXT_KEYS)
        if step.get("id") in (ai_step_ids or set()):
            cfg["content_origin"] = ORIGIN_AI
        elif new_text == old_text:
            if old_origin:
                cfg["content_origin"] = old_origin
            else:
                cfg.pop("content_origin", None)
        elif not any(new_text):
            cfg.pop("content_origin", None)  # back to its template
        elif str(old_origin or "").startswith(ORIGIN_AI) and any(old_text):
            cfg["content_origin"] = ORIGIN_AI_EDITED
        else:
            cfg["content_origin"] = ORIGIN_HUMAN
        result.append({**step, "config": cfg})
    return result


def content_hash(channel: str, subject_template: str | None, body_template: str | None) -> str:
    text = "\n".join(
        [str(channel or "").strip().lower(), normalize_subject(subject_template), " ".join(str(body_template or "").split())]
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_message_version(
    db: Session,
    *,
    client_id: int,
    channel: str,
    subject_template: str | None,
    body_template: str | None,
    language: str | None = None,
    purpose: str | None = None,
    stage: str | None = None,
    origin: str | None = None,
    strategy_id: int | None = None,
    step_id: str | None = None,
    strategy_version: int | None = None,
    template_id: str | None = None,
    audience: str | None = None,
) -> PayflowMessageVersionModel:
    """The stored row for this exact text (per client), created the first time it is sent.
    `audience`: which of the step's texts it is ("main", or an audience version such as "age_65_plus")."""
    digest = content_hash(channel, subject_template, body_template)
    now = datetime.now(timezone.utc)

    def find() -> PayflowMessageVersionModel | None:
        return (
            db.query(PayflowMessageVersionModel)
            .filter(
                PayflowMessageVersionModel.client_id == client_id,
                PayflowMessageVersionModel.content_hash == digest,
            )
            .first()
        )

    row = find()
    if row is None:
        row = PayflowMessageVersionModel(
            client_id=client_id,
            content_hash=digest,
            channel=channel,
            language=language,
            subject_template=(subject_template or None) and str(subject_template)[:512],
            subject_key=subject_key(subject_template),
            body_template=str(body_template or ""),
            purpose=purpose,
            reminder_stage=stage,
            origin=origin,
            strategy_id=strategy_id,
            step_id=step_id,
            strategy_version=strategy_version,
            template_id=template_id,
            audience=audience,
            first_used_at=now,
        )
        try:
            # Savepoint: another worker may store the same text at the same moment.
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            row = find()
            if row is None:
                raise
    row.last_used_at = now
    return row


def apply_tracking(db: Session, comm: PayflowCommunicationModel, tracking: dict[str, Any] | None) -> None:
    """Fill the communication's tracking columns. `tracking` keys (all optional): subject_template,
    body_template, step_id, strategy_version, run_id, portfolio_id, reminder_stage, language,
    template_id, origin, is_manual, message_audience (which of the step's texts: "main" or an audience
    version such as "age_65_plus" - the subject / body templates are that text's)."""
    tracking = tracking or {}
    is_email = (comm.channel or "").strip().lower() != "sms"
    subject_template = tracking.get("subject_template") if is_email else None
    if is_email and not subject_template:
        subject_template = comm.subject  # no template known (e.g. a manual email): the subject as sent
    comm.subject_template = (subject_template or None) and str(subject_template)[:512]
    comm.subject_key = subject_key(subject_template)
    comm.step_id = tracking.get("step_id")
    comm.strategy_version = tracking.get("strategy_version")
    comm.run_id = tracking.get("run_id")
    comm.portfolio_id = tracking.get("portfolio_id")
    comm.reminder_stage = tracking.get("reminder_stage")
    comm.language = tracking.get("language")
    comm.is_manual = bool(tracking.get("is_manual"))
    comm.message_audience = tracking.get("message_audience")
    body_template = tracking.get("body_template")
    if body_template is None:
        return  # text typed for one customer: nothing reusable to keep as a version
    version = record_message_version(
        db,
        client_id=comm.client_id,
        channel=comm.channel,
        subject_template=subject_template,
        body_template=body_template,
        language=comm.language,
        purpose=comm.purpose,
        stage=comm.reminder_stage,
        origin=tracking.get("origin"),
        strategy_id=comm.strategy_id,
        step_id=comm.step_id,
        strategy_version=comm.strategy_version,
        template_id=tracking.get("template_id"),
        audience=comm.message_audience,
    )
    comm.message_version_id = version.id
