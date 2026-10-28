"""Decide the tone of every message in a workflow — code rules, no AI.

The AI writes the text; these rules decide how soft or firm each message is,
so the tone is predictable, testable and explainable to reviewers:

1. The segment sets the starting tone (situation, stage, contact history; mixed stages at most Clear).
2. Each earlier message on the same path makes the next one a step firmer (up to Firm).
3. What happened since the last message adjusts it (link clicked, promise broken, ...).
4. The step's purpose: a Firm Reminder is one step firmer; some purposes ask for a special tone.
5. Late-stage segments: the last message before a hand-over is a Final notice (if not the first).

"Harsh" stops at Firm / Final notice — never threats. To change the rules, edit the tables below.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from app.modules.payflow.services.workflow_suggestion.config import DELINQUENCY_RANGES
from app.shared.enums import PayflowCollectionStatus

FRIENDLY, CLEAR, FIRM, FINAL_NOTICE = "Friendly", "Clear", "Firm", "Final notice"
# Tones off the ladder, for messages that are not a push for payment.
SUPPORTIVE, NEUTRAL = "Supportive", "Neutral"
# The firmness ladder, softest first. Final notice is reached only by rule 5.
LADDER = [FRIENDLY, CLEAR, FIRM]

PAYMENT_PLAN = PayflowCollectionStatus.PAYMENT_PLAN.value

# Rule 1 — starting tone from the segment's stage (Active accounts).
START_TONE = {
    "1 – 29 days past due": FRIENDLY,
    "30 – 59 days past due": CLEAR,
    "60 – 89 days past due": FIRM,
    "90+ days past due": FIRM,
}
# Arrangements start friendly whatever the stage — the customer already agreed to pay.
SITUATION_TONE = {
    PayflowCollectionStatus.PROMISE_TO_PAY.value: FRIENDLY,
    PAYMENT_PLAN: FRIENDLY,
}
# Rule 4 — purposes that ask for a firmer tone (one step), or for a special tone.
PURPOSE_TONE = {
    "Firm Reminder": FIRM,
    "Payment Plan Reminder": SUPPORTIVE,
    "Contact Details Update Request": NEUTRAL,
}
# Rule 5 — steps that hand the case over to people.
HAND_OVER_KINDS = {"Human Review"}
HAND_OVER_ACTIONS = {"Move Case To Escalated Treatment"}

# Rule 3 — what a Condition branch can tell: (attribute, values meaning "it happened",
# value whose absence means it happened). E.g. "Promise-to-Pay Not Equals Kept" = broken.
LINK_CLICKED = [("Payment Link", {"Clicked"}, "Not Clicked")]
PROMISE_BROKEN = [("Promise-to-Pay", {"Broken"}, "Kept")]
PLAN_PAYMENT_MISSED = [("Payment Status", {"Unpaid"}, "Payment Plan Active")]
DISPUTE_RAISED = [("Customer Response", {"Dispute Raised"}, None)]
NOT_DELIVERED = [
    ("Email Delivery", {"Failed", "Bounced"}, "Delivered"),
    ("SMS Delivery", {"Failed"}, "Delivered"),
]


def message_tones(
    workflow: dict[str, Any], segment: dict[str, Any], avg_days_past_due: int | None = None
) -> dict[str, dict[str, str]]:
    """{step_id: {"tone", "why", "path"}} for every Communication step of the workflow.

    `segment`: the workflow's segment (situation, delinquency, contact_state, ...).
    `avg_days_past_due`: used when the segment covers all stages.
    """
    steps = {step["id"]: step for step in workflow.get("steps") or [] if step.get("id")}
    start, start_why = _start(segment, avg_days_past_due)
    return {
        step_id: _tone(steps, step_id, path, segment, start, start_why)
        for step_id, path in _paths(steps, workflow.get("entry_node_id")).items()
        if steps[step_id].get("kind") == "Communication"
    }


def _start(segment: dict[str, Any], avg_days_past_due: int | None) -> tuple[int, list[str]]:
    """Rule 1: ladder level the segment starts at, and why."""
    situation = segment.get("situation")
    if situation in SITUATION_TONE:
        level, why = LADDER.index(SITUATION_TONE[situation]), [f"{situation} segment"]
    elif segment.get("delinquency") in START_TONE:
        stage = segment["delinquency"]
        level, why = LADDER.index(START_TONE[stage]), [stage]
    else:
        # "All stages" mixes early and late accounts: the average decides, but never above Clear.
        stage = _stage_of(avg_days_past_due)
        level = min(LADDER.index(START_TONE.get(stage, FRIENDLY)), LADDER.index(CLEAR))
        why = [f"mixed stages (average: {stage or 'unknown'})"]
    if segment.get("contact_state") == "contacted_no_payment":
        level = min(level + 1, LADDER.index(FIRM))
        why.append("contacted before but nothing paid")
    return level, why


def _tone(
    steps: dict[str, dict],
    step_id: str,
    path: list[tuple[str, str]],
    segment: dict[str, Any],
    start: int,
    start_why: list[str],
) -> dict[str, str]:
    why = list(start_why)
    situation, contact = segment.get("situation"), segment.get("contact_state")
    purpose = (steps[step_id].get("config") or {}).get("purpose")
    branches = _branches_since_last_message(steps, path)

    # Rule 2 — one step firmer per earlier message (a message that never arrived does not count).
    earlier = sum(1 for sid, _ in path if steps[sid].get("kind") == "Communication")
    if earlier and _happened(branches, NOT_DELIVERED):
        earlier -= 1
        why.append("the previous message did not arrive, so it does not count")
    level = min(start + earlier, LADDER.index(FIRM))
    if earlier:
        why.append(f"message {earlier + 1} on this path")
    if contact == "never_contacted" and earlier == 0:
        level = max(level - 1, 0)
        why.append("first contact: say who is writing and why")
    if contact == "partly_paid":
        level = min(level, LADDER.index(CLEAR))
        why.append("partly paid: acknowledge the progress")

    # Rule 3 — what happened since the last message.
    if _happened(branches, LINK_CLICKED):
        level = LADDER.index(CLEAR)
        why.append("payment link clicked but not paid: offer help")
    broken = _happened(branches, PROMISE_BROKEN) or (
        situation == PAYMENT_PLAN and _happened(branches, PLAN_PAYMENT_MISSED)
    )
    if broken:
        level = max(level, LADDER.index(FIRM))
        why.append("promise or plan payment missed")

    # Rule 4 — a Firm Reminder is one step firmer than the segment and position give (not a jump to Firm).
    if PURPOSE_TONE.get(purpose) == FIRM and level < LADDER.index(FIRM):
        level += 1
        why.append("purpose is Firm Reminder")
    tone = LADDER[level]
    # Rule 5 — late-stage segments: last message before people take over (never the first one).
    if tone == FIRM and start == LADDER.index(FIRM) and earlier and _hands_over_next(steps, step_id):
        tone = FINAL_NOTICE
        why.append("next step hands the case over")

    # Special tones replace the ladder when the message is not a push for payment.
    if PURPOSE_TONE.get(purpose) == NEUTRAL or _happened(branches, DISPUTE_RAISED):
        tone = NEUTRAL
        why.append("not a payment request" if PURPOSE_TONE.get(purpose) == NEUTRAL else "customer raised a dispute")
    elif tone in (FRIENDLY, CLEAR) and (
        PURPOSE_TONE.get(purpose) == SUPPORTIVE
        or situation == PAYMENT_PLAN
        or (contact == "partly_paid" and earlier == 0)
    ):
        tone = SUPPORTIVE
        why.append("encourage the arrangement / progress")
    return {"tone": tone, "why": "; ".join(why), "path": _path_text(steps, path)}


def _path_text(steps: dict[str, dict], path: list[tuple[str, str]]) -> str:
    """What happened before the message, e.g. "Case received → Firm email → Wait 2 days → Email bounced? = no"."""
    parts = []
    for step_id, link in path:
        step = steps[step_id]
        title = step.get("title") or step.get("kind")
        parts.append(f"{title} = {link}" if step.get("kind") == "Condition" else title)
    return " → ".join(parts)


def _stage_of(days_past_due: int | None) -> str | None:
    if days_past_due is None:
        return None
    return next(
        (label for label, (low, high) in DELINQUENCY_RANGES.items()
         if days_past_due >= low and (high is None or days_past_due < high)),
        None,
    )


def _paths(steps: dict[str, dict], entry_id: str | None) -> dict[str, list[tuple[str, str]]]:
    """Shortest route from the entry to every step: [(step_id, link taken), ...] before it."""
    if entry_id not in steps:
        return {}
    paths: dict[str, list[tuple[str, str]]] = {entry_id: []}
    queue = deque([entry_id])
    while queue:
        step_id = queue.popleft()
        for link in ("next", "yes", "no"):
            target = steps[step_id].get(link)
            if target in steps and target not in paths:
                paths[target] = paths[step_id] + [(step_id, link)]
                queue.append(target)
    return paths


def _branches_since_last_message(steps: dict[str, dict], path: list[tuple[str, str]]) -> list[tuple]:
    """(attribute, value, holds) of each Condition passed since the previous message."""
    branches = []
    for step_id, link in reversed(path):
        step = steps[step_id]
        if step.get("kind") == "Communication":
            break
        if step.get("kind") == "Condition":
            cfg = step.get("config") or {}
            # "Equals" on the yes branch, or "Not Equals" on the no branch → attribute is value.
            holds = (cfg.get("operator") != "Not Equals") == (link == "yes")
            branches.append((cfg.get("attribute"), cfg.get("value"), holds))
    return branches


def _happened(branches: list[tuple], events: list[tuple]) -> bool:
    for attribute, value, holds in branches:
        for event_attribute, happened_values, not_happened_value in events:
            if attribute == event_attribute and (
                value in happened_values if holds else value == not_happened_value
            ):
                return True
    return False


def _hands_over_next(steps: dict[str, dict], step_id: str) -> bool:
    """True when a path from this message reaches a hand-over before any other message."""
    stack, seen = _links(steps[step_id]), set()
    while stack:
        target_id = stack.pop()
        target = steps.get(target_id)
        if target is None or target_id in seen:
            continue
        seen.add(target_id)
        action = (target.get("config") or {}).get("action")
        if target.get("kind") in HAND_OVER_KINDS or action in HAND_OVER_ACTIONS:
            return True
        if target.get("kind") != "Communication":
            stack.extend(_links(target))
    return False


def _links(step: dict[str, Any]) -> list[str]:
    return [step[link] for link in ("next", "yes", "no") if step.get(link)]
