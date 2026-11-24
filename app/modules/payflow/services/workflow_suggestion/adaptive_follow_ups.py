"""Adaptive follow-ups on AI workflows (adaptive workflows ticket, AI segment 3).

Suggest and Regenerate add one message step per situation the adaptive layer answers: not opened, opened but
not paid, payment not finished, payment failed, part paid, no response. They sit off the flowchart (no links,
config.follow_up_for): next_action.py sends one in place of the next planned reminder when a customer is in
that situation. Their text is written with the workflow's other messages (main text and translations, no
audience versions) and reviewed with the workflow, so using one needs no approval per customer.
A follow-up without text is never used; Write messages can write it later.

Only workflows with an ordinary reminder get them (the messages a follow-up may replace): a Payment Plan or
Promise-to-Pay workflow whose messages are all about the arrangement has nothing to replace.
"""

from __future__ import annotations

from typing import Any

from app.modules.payflow.services.strategy_service import follow_up_for, is_follow_up_step
from app.modules.payflow.services.workflow_suggestion.message_tone import CLEAR, LADDER, SUPPORTIVE, _start
from app.modules.payflow.services.workflow_suggestion.next_action import REPLACEABLE_PURPOSES

# situation → the step's title, and when it is sent (what the message writer is told).
FOLLOW_UPS: dict[str, dict[str, str]] = {
    "email_not_opened": {
        "title": "Follow-up: email not opened",
        "when": "the customer has not opened the last email for two days (it often goes by SMS)",
    },
    "opened_not_clicked": {
        "title": "Follow-up: opened, not paid",
        "when": "the customer opened the last email but did not go to the payment page",
    },
    "clicked_not_paid": {
        "title": "Follow-up: payment not finished",
        "when": "the customer went to the payment page or started a payment but did not finish it",
    },
    "payment_failed": {
        "title": "Follow-up: payment failed",
        # The customer saw the payment fail, so the message may say so (unlike what they opened or clicked).
        "when": "the customer tried to pay but the payment did not go through (they saw it fail); say so plainly "
        "and invite them to try again or choose another way to pay",
    },
    "payment_partial": {
        "title": "Follow-up: part paid",
        "when": "the customer paid part of the balance; the rest is still open",
    },
    "no_activity": {
        "title": "Follow-up: no response",
        "when": "the customer has not acted on any message for a week",
    },
}
PURPOSE = "Payment Reminder"
ID_PREFIX = "fu_"
# The message writer recognises a follow-up by this start of its path_so_far (message_prompt.py).
PATH_START = "Follow-up, not on the timeline: sent in place of the next planned reminder when"
LABEL = "Adaptive follow-ups"


def planned_steps(steps: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The flowchart's steps, without the follow-ups (what the design AI reads and the validator checks)."""
    return [step for step in steps or [] if isinstance(step, dict) and not is_follow_up_step(step)]


def add_follow_ups(workflow: dict[str, Any]) -> dict[str, Any]:
    """The workflow with a follow-up step for each situation it does not have one for yet - when it has a
    message a follow-up may replace."""
    steps = list(workflow.get("steps") or [])
    replaceable = any(
        step.get("kind") == "Communication" and _purpose(step) in REPLACEABLE_PURPOSES for step in planned_steps(steps)
    )
    if not replaceable:
        return workflow
    have = {follow_up_for(step) for step in steps if is_follow_up_step(step)}
    ids = {step.get("id") for step in steps if isinstance(step, dict)}
    for situation, item in FOLLOW_UPS.items():
        if situation in have:
            continue
        step_id = ID_PREFIX + situation
        while step_id in ids:
            step_id += "_2"
        ids.add(step_id)
        steps.append(
            {
                "id": step_id,
                "kind": "Communication",
                "title": item["title"],
                "config": {"purpose": PURPOSE, "follow_up_for": situation},
            }
        )
    return {**workflow, "steps": steps}


def follow_up_tones(
    workflow: dict[str, Any], segment: dict[str, Any], avg_days_past_due: int | None = None
) -> dict[str, dict[str, str]]:
    """{step_id: {"tone", "why", "path"}} for the follow-ups (message_tone.message_tones covers the flowchart).

    A follow-up can come after any reminder, so it starts where the segment starts and is never firmer than
    Clear (it answers something the customer did); part paid is Supportive (thank them, ask for the rest).
    """
    start, why = _start(segment, avg_days_past_due)
    level = min(start, LADDER.index(CLEAR))
    tones = {}
    for step in workflow.get("steps") or []:
        situation = follow_up_for(step)
        if not situation:
            continue
        when = (FOLLOW_UPS.get(situation) or {}).get("when") or situation.replace("_", " ")
        if situation == "payment_partial":
            tone, reason = SUPPORTIVE, "follow-up after a part payment: thank them, ask for the rest"
        else:
            tone, reason = LADDER[level], "; ".join([*why, "follow-up: never firmer than Clear"])
        tones[step["id"]] = {"tone": tone, "why": reason, "path": f"{PATH_START} {when}"}
    return tones


def follow_up_line(steps: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """The "Adaptive follow-ups" ai_context line: which ones are written, which are not (never sent)."""
    follow_ups = [step for step in steps or [] if is_follow_up_step(step)]
    if not follow_ups:
        return []
    written = [_title(step) for step in follow_ups if has_text(step)]
    missing = [_title(step) for step in follow_ups if not has_text(step)]
    value = "Sent in place of the next planned reminder, only to customers in that situation."
    if written:
        value += f" Written: {', '.join(written)}."
    if missing:
        value += f" Not written yet, so never sent until written (Write messages): {', '.join(missing)}."
    return [{"label": LABEL, "value": value}]


def has_text(step: dict[str, Any]) -> bool:
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    return bool(str(config.get("body") or "").strip() or str(config.get("sms_body") or "").strip())


def _purpose(step: dict[str, Any]) -> str:
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    return str(config.get("purpose") or step.get("purpose") or PURPOSE)


def _title(step: dict[str, Any]) -> str:
    return str(step.get("title") or step.get("id"))
