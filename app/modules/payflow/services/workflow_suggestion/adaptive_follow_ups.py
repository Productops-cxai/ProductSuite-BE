"""Adaptive follow-ups on AI workflows (adaptive workflows ticket).

Suggest and Regenerate add message steps for each situation the adaptive layer answers (FOLLOW_UPS): a
first-time text, and a repeat text for when the same situation happens again (config.follow_up_level 2). They
sit off the flowchart (no links, config.follow_up_for): next_action.py sends one in place of the next planned
reminder when a customer is in that situation. Their text is written after the workflow's other messages, in calls
of their own (main text, the segment's audience versions, translations), and reviewed with the workflow, so using
one needs no approval per customer. A follow-up without text is never used; Write messages can write it later.

Only workflows with an ordinary reminder get them (the messages a follow-up may replace): a Payment Plan or
Promise-to-Pay workflow whose messages are all about the arrangement has nothing to replace.

To add a situation: an entry in FOLLOW_UPS, its name in strategy_service.FOLLOW_UP_FOR_VALUES and the rule that
picks it in next_action.py. To add a level (a third text): one more "when" in its list.
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.modules.payflow.services.strategy_service import follow_up_for, follow_up_level, is_follow_up_step
from app.modules.payflow.services.workflow_suggestion.message_tone import (
    CLEAR,
    LADDER,
    PAYMENT_PLAN,
    SUPPORTIVE,
    _start,
    message_tones,
)
from app.modules.payflow.services.workflow_suggestion.message_validator import PLAN_OPTION
from app.modules.payflow.services.workflow_suggestion.next_action import PLAN_SETUP, REPLACEABLE_PURPOSES

_AGAIN = "it happened again after the first follow-up was sent: do not repeat that message - take a new angle"
# situation → "label" (its name on screen), "trigger" (when it is used, for Operations; {hours} is the setting
# "hours"), the steps' "title", and "when" each level is sent: the first time, then the repeat (what the message
# writer is told). "needs": a payment-page option the client must offer (message_validator.PLAN_OPTION).
FOLLOW_UPS: dict[str, dict[str, Any]] = {
    "email_not_opened": {
        "label": "Not Opened",
        "trigger": "No email open within {hours} hours of sending",
        "hours": "ADAPTIVE_NOT_OPENED_HOURS",
        "title": "Follow-up: email not opened",
        "when": [
            "the customer has not opened the last email for two days (it often goes by SMS)",
            f"{_AGAIN}: short and fresh, the easiest way to pay, and an offer of help",
        ],
    },
    "opened_not_clicked": {
        "label": "Opened, Not Paid",
        "trigger": "Email opened; payment link not clicked within {hours} hours of the open (a customer who clicks "
        "moves to Payment Not Finished)",
        "hours": "ADAPTIVE_OPENED_NOT_CLICKED_HOURS",
        "title": "Follow-up: opened, not paid",
        "when": [
            "the customer opened the last email but did not go to the payment page",
            f"{_AGAIN}: the other ways to pay the payment page offers, and an offer of help",
        ],
    },
    "clicked_not_paid": {
        "label": "Payment Not Finished",
        "trigger": "Payment page visited, or a payment left unfinished; no payment within {hours} hours",
        "hours": "ADAPTIVE_CLICKED_NOT_PAID_HOURS",
        "title": "Follow-up: payment not finished",
        "when": [
            # Not "started a payment": the writer echoes it ("the payment you started"), which says we saw it.
            "the customer went to the payment page but has not paid yet; make paying easy (e.g. \"You can pay in "
            "a few minutes on your secure page\") - no word about stopping, starting or visiting it",
            f"{_AGAIN}: the other ways to pay the payment page offers (smaller steps where offered), and help",
        ],
    },
    "payment_failed": {
        "label": "Payment Failed",
        "trigger": "A payment did not go through; no contact for {hours} hours after it",
        "hours": "ADAPTIVE_PAYMENT_FAILED_WAIT_HOURS",
        "title": "Follow-up: payment failed",
        # The customer saw the payment fail, so the message may say so (unlike what they opened or clicked).
        "when": [
            "the customer tried to pay but the payment did not go through (they saw it fail); say so plainly "
            "and invite them to try again or choose another way to pay",
            f"{_AGAIN}: a payment did not go through again (they saw it fail); suggest another way to pay, and help",
        ],
    },
    "payment_partial": {
        "label": "Part Paid",
        "trigger": "Part of the balance paid; no contact for {hours} hours after it",
        "hours": "ADAPTIVE_PART_PAID_WAIT_HOURS",
        "title": "Follow-up: part paid",
        "when": [
            "the customer paid part of the balance; the rest is still open",
            f"{_AGAIN}: they paid part earlier and have not acted since; remind them of the rest, with smaller "
            "steps where offered",
        ],
    },
    "no_activity": {
        "label": "No Engagement",
        "trigger": "No open, click or payment within {hours} hours of the last message",
        "hours": "ADAPTIVE_NO_ACTIVITY_HOURS",
        "title": "Follow-up: no response",
        "when": [
            "the customer has not acted on any message for a few days",
            f"{_AGAIN}: still no response; a softer message that offers help",
        ],
    },
    PLAN_SETUP: {
        "label": "Plan Setup Not Finished",
        "trigger": "A payment plan setup not finished within {hours} hours (only for clients that offer plans)",
        "hours": "ADAPTIVE_PLAN_SETUP_HOURS",
        "title": "Follow-up: plan setup not finished",
        "needs": PLAN_OPTION,
        "when": [
            "the customer's payment plan is not set up yet; invite them to set it up (e.g. \"Setting up your "
            "payment plan takes a few minutes\") - no word about starting or beginning it",
            f"{_AGAIN}: the plan is still not set up; offer help with setting it up, or another way to pay",
        ],
    },
}
REPEAT = " (repeat)"  # the end of a repeat follow-up's title
PURPOSE = "Payment Reminder"
ID_PREFIX = "fu_"
# The message writer recognises a follow-up by this start of its path_so_far (message_prompt.py).
PATH_START = "Follow-up, not on the timeline: sent in place of the next planned reminder when"
LABEL = "Adaptive follow-ups"


def follow_up_types() -> list[dict[str, Any]]:
    """The follow-up types for the workflow screen (GET /payflow/adaptive/follow-up-types): name, when each is used
    with today's waiting time from the settings, its texts (first time, repeat) and whether it needs payment plans."""
    types = []
    for key, item in FOLLOW_UPS.items():
        hours = int(getattr(settings, item["hours"]))
        types.append({
            "follow_up_for": key,
            "label": item["label"],
            "trigger": item["trigger"].format(hours=hours),
            "waiting_hours": hours,
            "needs_payment_plans": item.get("needs") == PLAN_OPTION,
            "levels": [
                {"level": level, "name": _level_name(level), "step_title": _level_title(item, level)}
                for level in range(1, len(item["when"]) + 1)
            ],
        })
    return types


def planned_steps(steps: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The flowchart's steps, without the follow-ups (what the design AI reads and the validator checks)."""
    return [step for step in steps or [] if isinstance(step, dict) and not is_follow_up_step(step)]


def add_follow_ups(
    workflow: dict[str, Any],
    *,
    payment_options: list[str] | None = None,
    situation: str | None = None,
) -> dict[str, Any]:
    """The workflow with a follow-up step for each situation and level it does not have yet - when it has a
    message a follow-up may replace. `payment_options`: what the client's payment page offers
    (context.load_payment_options; None: unknown) - a situation that needs an option the client does not offer
    is left out, and so is "plan setup not finished" for a segment already on a payment plan (`situation`)."""
    steps = list(workflow.get("steps") or [])
    replaceable = any(
        step.get("kind") == "Communication" and _purpose(step) in REPLACEABLE_PURPOSES for step in planned_steps(steps)
    )
    if not replaceable:
        return workflow
    have = {(follow_up_for(step), follow_up_level(step)) for step in steps if is_follow_up_step(step)}
    ids = {step.get("id") for step in steps if isinstance(step, dict)}
    for key, item in FOLLOW_UPS.items():
        if not _offered(item, payment_options, situation):
            continue
        for level in range(1, len(item["when"]) + 1):
            if (key, level) in have:
                continue
            step_id = ID_PREFIX + key + ("" if level == 1 else "_repeat" if level == 2 else f"_repeat{level - 1}")
            while step_id in ids:
                step_id += "_2"
            ids.add(step_id)
            config = {"purpose": PURPOSE, "follow_up_for": key}
            if level > 1:
                config["follow_up_level"] = level
            steps.append({"id": step_id, "kind": "Communication", "title": _level_title(item, level), "config": config})
    return {**workflow, "steps": steps}


def workflow_tones(workflow: dict[str, Any], segment: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The code's tone (and path) of every message of the workflow - the flowchart's, then the follow-ups'.
    `segment`: {"segment" (fields), "stats"} - the segmenter's shape."""
    situation, avg = segment.get("segment") or {}, (segment.get("stats") or {}).get("avg_days_past_due")
    return {**message_tones(workflow, situation, avg), **follow_up_tones(workflow, situation, avg)}


def follow_up_tones(
    workflow: dict[str, Any], segment: dict[str, Any], avg_days_past_due: int | None = None
) -> dict[str, dict[str, str]]:
    """{step_id: {"tone", "why", "path"}} for the follow-ups (message_tone.message_tones covers the flowchart).

    A follow-up can come after any reminder, so it starts where the segment starts and is never firmer than
    Clear (it answers something the customer did); part paid and plan setup are Supportive (they are part way).
    """
    start, why = _start(segment, avg_days_past_due)
    level = min(start, LADDER.index(CLEAR))
    tones = {}
    for step in workflow.get("steps") or []:
        situation = follow_up_for(step)
        if not situation:
            continue
        levels = (FOLLOW_UPS.get(situation) or {}).get("when") or [situation.replace("_", " ")]
        when = levels[min(follow_up_level(step), len(levels)) - 1]
        if situation == "payment_partial":
            tone, reason = SUPPORTIVE, "follow-up after a part payment: thank them, ask for the rest"
        elif situation == PLAN_SETUP:
            tone, reason = SUPPORTIVE, "follow-up on a payment plan not set up yet: help them finish it"
        else:
            tone, reason = LADDER[level], "; ".join([*why, "follow-up: never firmer than Clear"])
        tones[step["id"]] = {"tone": tone, "why": reason, "path": f"{PATH_START} {when}"}
    return tones


def _offered(item: dict[str, Any], payment_options: list[str] | None, situation: str | None) -> bool:
    """A situation that needs a payment-page option: only when the client offers it (a plan: not to customers
    already on one)."""
    needs = item.get("needs")
    if not needs:
        return True
    if needs == PLAN_OPTION and situation == PAYMENT_PLAN:
        return False
    return needs in (payment_options or [])


def _level_title(item: dict[str, Any], level: int) -> str:
    if level == 1:
        return item["title"]
    return item["title"] + (REPEAT if level == 2 else f" (repeat {level - 1})")


def _level_name(level: int) -> str:
    return "First time" if level == 1 else "Repeat" if level == 2 else f"Repeat {level - 1}"


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
