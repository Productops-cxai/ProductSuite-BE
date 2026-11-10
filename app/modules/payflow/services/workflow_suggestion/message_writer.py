"""Write the email / SMS text of a workflow's Communication steps — one AI call per workflow.

Code decides each message's tone (message_tone.py), the AI writes the text, code checks it
(message_validator.py) and sends problems back for a fix. Nothing is saved here.
A step without a channel (AI workflows: the worker picks email or SMS per customer) gets both
an email (subject + body) and an SMS (sms_body).
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import ServiceUnavailableError, ValidationAppError
from app.modules.payflow.services.strategy_service import _timing_label
from app.modules.payflow.services.workflow_suggestion.config import CONTACT_CHANNEL
from app.modules.payflow.services.workflow_suggestion.llm import build_message_schema, call_llm
from app.modules.payflow.services.workflow_suggestion.message_prompt import build_message_prompt
from app.modules.payflow.services.workflow_suggestion.message_tone import message_tones
from app.modules.payflow.services.workflow_suggestion.message_validator import (
    MAX_SUBJECT_CHARS,
    check_message,
    render_sample,
)
from app.shared.enums import PayflowCollectionStatus

# After the first AI answer, failing messages are sent back to the AI this many times.
MAX_FIX_ATTEMPTS = 2


def write_messages(
    client: dict[str, Any],
    workflow: dict[str, Any],
    segment: dict[str, Any],
    *,
    step_ids: list[str] | None = None,
    remarks: str | None = None,
) -> dict[str, dict[str, Any]]:
    """{step_id: {"subject", "body", "sms_body", "tone"}} for the workflow's Communication steps.

    `segment`: {"label", "segment" (fields), "stats"} — the segmenter's shape.
    `step_ids`: write only these messages (the others are context); default all.
    """
    steps = {step["id"]: step for step in workflow.get("steps") or [] if step.get("id")}
    stats = segment.get("stats") or {}
    tones = message_tones(workflow, segment.get("segment") or {}, stats.get("avg_days_past_due"))
    unknown = [step_id for step_id in step_ids or [] if step_id not in tones]
    if unknown:
        raise ValidationAppError(f"Step {unknown[0]} is not an email or SMS step of this workflow")
    pending = [step_id for step_id in tones if step_ids is None or step_id in step_ids]
    if not pending:
        raise ValidationAppError("This workflow has no email or SMS steps")

    english = (client.get("language") or "English").strip().lower().startswith("english")
    written: dict[str, dict[str, Any]] = {}
    feedback: list[dict[str, Any]] = []
    for _ in range(1 + MAX_FIX_ATTEMPTS):
        briefs = [_brief(steps[step_id], tones[step_id], segment, english) for step_id in pending]
        others = _other_messages(steps, tones, written, skip=pending)
        prompt = build_message_prompt(client, workflow, segment, briefs, others, feedback, remarks)
        reply = _messages_by_step(call_llm(prompt, build_message_schema(pending)))

        feedback = []
        bodies = [text for other in others for text in (other["body"], other.get("sms_body")) if text]
        for brief in briefs:
            message = _clean(reply.get(brief["step_id"]), brief["channel"])
            errors = _check(message, brief, bodies) if message else [f"{brief['step_id']}: no message was written"]
            if errors:
                feedback.append({"step_id": brief["step_id"], "message": message, "errors": errors})
            else:
                written[brief["step_id"]] = {**message, "tone": brief["tone"]}
                bodies += [text for text in (message["body"], message.get("sms_body")) if text]
        if not feedback:
            return written
        pending = [item["step_id"] for item in feedback]

    problems = [error for item in feedback for error in item["errors"]]
    raise ServiceUnavailableError("AI could not write valid messages: " + "; ".join(problems[:10]))


def apply_messages(steps: list[dict[str, Any]], written: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of the steps with the written text in their config (other steps unchanged)."""
    result = []
    for step in steps:
        message = written.get(step.get("id"))
        if message:
            text = {key: value for key, value in message.items() if value}
            step = {**step, "config": {**(step.get("config") or {}), **text}}
        result.append(step)
    return result


def _brief(step: dict[str, Any], tone: dict[str, str], segment: dict[str, Any], english: bool) -> dict[str, Any]:
    cfg = step.get("config") or {}
    situation = (segment.get("segment") or {}).get("situation")
    current = {key: cfg[key] for key in ("subject", "body", "sms_body") if cfg.get(key)}
    return {
        "step_id": step["id"],
        "title": step.get("title"),
        "channel": cfg.get("channel") or step.get("channel") or CONTACT_CHANNEL,
        "purpose": cfg.get("purpose") or step.get("purpose"),
        "timing": _timing_label(step),
        "path_so_far": tone["path"],
        "tone": tone["tone"],
        "why_this_tone": tone["why"],
        "current_text": current or None,
        # Promise variables are empty for accounts without a promise.
        "promise_ok": cfg.get("purpose") == "Promise-to-Pay Reminder"
        or situation == PayflowCollectionStatus.PROMISE_TO_PAY.value,
        "english": english,  # some checks look for English words
    }


def _other_messages(
    steps: dict[str, dict], tones: dict[str, dict], written: dict[str, dict], skip: list[str]
) -> list[dict[str, Any]]:
    """The workflow's messages that are not being written now (just written, or already on the step)."""
    others = []
    for step_id in tones:
        if step_id in skip:
            continue
        text = written.get(step_id) or (steps[step_id].get("config") or {})
        if text.get("body"):
            channel = (steps[step_id].get("config") or {}).get("channel") or CONTACT_CHANNEL
            other = {"step_id": step_id, "channel": channel, "subject": text.get("subject"), "body": text["body"]}
            if text.get("sms_body"):
                other["sms_body"] = text["sms_body"]
            others.append(other)
    return others


def _check(message: dict[str, Any], brief: dict[str, Any], bodies: list[str]) -> list[str]:
    """A contact step's email and SMS are each checked by their own channel's rules."""
    if brief["channel"] != CONTACT_CHANNEL:
        return check_message(message, brief, bodies)
    email = {"subject": message.get("subject"), "body": message["body"]}
    sms = {"body": message.get("sms_body")}
    sms_brief = {**brief, "channel": "SMS", "step_id": f"{brief['step_id']} (SMS version, sms_body)"}
    return check_message(email, {**brief, "channel": "Email"}, bodies) + check_message(sms, sms_brief, bodies)


def _clean(message: Any, channel: str) -> dict[str, Any] | None:
    if not isinstance(message, dict) or not str(message.get("body") or "").strip():
        return None
    subject = str(message.get("subject") or "").strip()
    cleaned = {
        "subject": _fit_subject(subject) if channel != "SMS" and subject else None,  # SMS has no subject
        "body": str(message["body"]).strip(),
    }
    if channel == CONTACT_CHANNEL:
        cleaned["sms_body"] = str(message.get("sms_body") or "").strip() or None
    return cleaned


def _fit_subject(subject: str) -> str:
    """One line, and at most MAX_SUBJECT_CHARS once sample values are filled in."""
    text = " ".join(subject.split())
    if len(render_sample(text)) <= MAX_SUBJECT_CHARS:
        return text
    words = text.split(" ")
    while len(words) > 1 and len(render_sample(" ".join(words))) > MAX_SUBJECT_CHARS:
        words.pop()
    text = " ".join(words).rstrip(" -–|,;:")
    # Drop a trailing partial {{variable if word-trim left one open.
    if "{{" in text and "}}" not in text[text.rfind("{{") :]:
        text = text[: text.rfind("{{")].rstrip(" -–|,;:")
    return text


def _messages_by_step(reply: Any) -> dict[str, dict]:
    items = reply.get("messages") if isinstance(reply, dict) else None
    if not isinstance(items, list):
        return {}
    return {m["step_id"]: m for m in items if isinstance(m, dict) and m.get("step_id")}
