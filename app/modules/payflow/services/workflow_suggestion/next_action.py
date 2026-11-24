"""Adaptive next-best-action: the AI team's side of the handover (ADAPTIVE_WORKFLOWS_BE.md §5).

The backend calls ``decide_next_action(context)`` after its safety rules (paid in full → stop, unreachable →
Operations, email or SMS gone → switch channel) and carries out what it returns.

1. Fixed rules first, no AI call:
   - A known situation → the workflow's approved follow-up for it, sent in place of the next planned message
     (same time, so no extra message). Each follow-up is used once.
       opened, not clicked → "opened_not_clicked"     clicked, not paid / left the payment page → "clicked_not_paid"
       not opened in time → "email_not_opened" (by SMS first when it can)   part paid → "payment_partial"
       payment did not go through → "payment_failed"   no activity → "no_activity"
   - Payment started → the next step waits until the customer has had a day to finish.
   - Anything else → keep the plan.
   The follow-ups were approved with the workflow, so these are "rules" decisions: no approval per customer.
2. The AI (next_action_ai.py) only where the rules cannot settle it: a situation with no follow-up left to use,
   a next message that is unclear, or Operations pressing "Reassess plan". Never at send time, at most
   AI_CALLS_PER_WEEK times a week per run. It picks one of the same actions; "ai" decisions need approval
   for supervised clients (the backend asks). When the AI cannot be asked, the rules' answer stands.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.modules.payflow.services.workflow_suggestion.next_action_ai import ask_ai

CONTACT_CHANNEL = "Email + SMS"  # same as strategy_service.CONTACT_CHANNEL
# Planned messages a follow-up may take the place of: the ordinary reminders, not the promise / plan /
# contact-details ones (those are written for a customer in another situation).
REPLACEABLE_PURPOSES = frozenset({"Payment Reminder", "Firm Reminder", "Payment Link"})
# A started payment: no reminder for a day (a Stripe checkout stays open 24 hours).
PAYMENT_TIME = timedelta(hours=24)
# Steps the plan does not go past on its own when looking for the next message.
STOP_AT = frozenset({"Human Review", "Outcome", "End"})
MAX_WALK = 12
# The AI is asked at most this often per run (Operations' "Reassess plan" always asks).
AI_CALLS_PER_WEEK = 3

# The plan is right as it is: the workflow's own payment checks / Operations take it from here.
KEEP_FOR = {
    "payment_completed": "Customer paid",
    "plan_created": "Customer set up a payment plan; the workflow's payment checks take it from here",
}
# Event → the follow-up situation that answers it.
FOLLOW_UP_FOR = {
    "payment_partial": "payment_partial",
    # Operations also get an exception for it (the worker); the customer gets a "try again" message.
    "payment_failed": "payment_failed",
    "payment_abandoned": "clicked_not_paid",
    "clicked_not_paid": "clicked_not_paid",
    "opened_not_clicked": "opened_not_clicked",
    "email_not_opened": "email_not_opened",
    "no_activity": "no_activity",
}
# When several things happened since the last check, the first of these in the list decides.
PRIORITY = [
    "payment_completed",
    "plan_created",
    "payment_partial",
    "payment_failed",
    "payment_abandoned",
    "payment_started",
    "clicked_not_paid",
    "opened_not_clicked",
    "email_not_opened",
    "no_activity",
]
SITUATION_TEXT = {
    "payment_partial": "Customer paid part of the balance",
    "payment_failed": "Customer's payment did not go through",
    "payment_abandoned": "Customer left the payment page without paying",
    "clicked_not_paid": "Customer opened the payment page but has not paid",
    "opened_not_clicked": "Customer opened the email but did not click the payment link",
    "email_not_opened": "Customer has not opened the email",
    "no_activity": "No activity from the customer",
}


def decide_next_action(context: dict[str, Any]) -> dict[str, Any]:
    decision = _rules(context)
    unclear = decision.pop("_ask_ai", False)
    manual = context.get("trigger") == "manual" and decision["action"] == "keep_plan"
    if not (unclear or manual) or not _may_ask_ai(context, manual=manual):
        return decision
    answer = ask_ai(
        context,
        what_happened=decision["reason"],
        choices=[_choice(step) for step in _replaceable(_next_messages(context))],
        follow_ups=_follow_ups_left(context),
    )
    if answer is None:
        return {**decision, "reason": f"{decision['reason']}; the AI could not be asked, so the plan stays"}
    return _decision(**answer, decided_by="ai")


def _rules(context: dict[str, Any]) -> dict[str, Any]:
    events = [e for e in (context.get("new_events") or []) if isinstance(e, dict)]
    if not events:
        return _keep("Nothing new since the last check")
    event = next((e for kind in PRIORITY for e in events if e.get("type") == kind), None)
    if event is None:
        return _keep(f"No change needed for: {', '.join(sorted({str(e.get('type')) for e in events}))}")
    kind = event["type"]
    if kind in KEEP_FOR:
        return _keep(KEEP_FOR[kind])
    if kind == "payment_started":
        return _payment_started(context, event)
    return _follow_up(context, event, FOLLOW_UP_FOR[kind])


def _may_ask_ai(context: dict[str, Any], *, manual: bool) -> bool:
    """Never at send time (the send would wait); otherwise at most AI_CALLS_PER_WEEK per run."""
    trigger = context.get("trigger")
    if trigger == "message_due":
        return False
    if manual:
        return True
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    asked = [
        d
        for d in context.get("decisions") or []
        if isinstance(d, dict) and d.get("decided_by") == "ai" and (_time(d.get("at")) or week_ago) > week_ago
    ]
    return len(asked) < AI_CALLS_PER_WEEK


# --------------------------------------------------------------------------- follow-up swap


def _follow_up(context: dict[str, Any], event: dict[str, Any], situation: str) -> dict[str, Any]:
    kind = event["type"]
    why = SITUATION_TEXT[kind]
    account = context.get("account") or {}
    if account.get("payment_status") == "Payment Plan Active":
        return _keep(f"{why}, but they are on a payment plan; the workflow's payment checks take it from here")
    stale = _no_longer_true(context, event)
    if stale:
        return _keep(stale)

    workflow = context.get("workflow") or {}
    steps = _steps(context)
    follow_id = (workflow.get("follow_ups") or {}).get(situation)
    follow = steps.get(follow_id) if follow_id else None
    if not follow:
        return _keep(f"{why}; this workflow has no approved follow-up for it", ask_ai=True)
    run = context.get("run") or {}
    swaps = run.get("swaps") or {}
    if follow_id in _sent_step_ids(context):
        return _keep(f"{why}; its follow-up '{_title(follow)}' was already sent", ask_ai=True)
    if not _has_text(follow):
        return _keep(f"{why}; its follow-up '{_title(follow)}' has no text yet", ask_ai=True)
    if not _can_send(follow, account):
        return _keep(f"{why}; its follow-up '{_title(follow)}' cannot reach the customer on its channel", ask_ai=True)

    messages = _next_messages(context)
    if not messages:
        return _keep(f"{why}; there is no planned message to send the follow-up in place of")
    if len(messages) > 1:
        return _keep(f"{why}; it is unclear which message the customer gets next", ask_ai=True)
    planned = messages[0]
    if swaps.get(planned["id"]) == follow_id:
        return _keep(f"{why}; the follow-up is already lined up in place of '{_title(planned)}'")
    if follow_id in swaps.values():
        return _keep(f"{why}; its follow-up '{_title(follow)}' is already lined up")
    if _purpose(planned) not in REPLACEABLE_PURPOSES:
        return _keep(f"{why}; the next message '{_title(planned)}' ({_purpose(planned)}) stays as planned")

    channel = None
    if (
        kind == "email_not_opened"
        and _channel(follow) == ""
        and account.get("sms_ok")
        and run.get("preferred_channel") != "SMS"
    ):
        channel = "SMS"  # they do not open emails: the follow-up, and the messages after it, go by SMS first
    reason = f"{why}: send the follow-up '{_title(follow)}' in place of '{_title(planned)}'"
    if channel:
        reason += ", by SMS first"
    return _decision(
        "use_follow_up",
        reason,
        replace_step_id=planned["id"],
        follow_up_step_id=follow_id,
        channel=channel,
    )


def _no_longer_true(context: dict[str, Any], event: dict[str, Any]) -> str | None:
    """A silence timer is out of date when the customer did act on that message since."""
    kind = event["type"]
    message = _message(context, event.get("step_id"))
    if kind == "email_not_opened" and message and message.get("opened"):
        return "The email was opened since; nothing to change"
    if kind == "opened_not_clicked" and message and message.get("clicked"):
        return "The payment link was clicked since; nothing to change"
    return None


def _next_messages(context: dict[str, Any]) -> list[dict[str, Any]]:
    """The next planned message(s): the next step itself, or the messages the plan reaches first from it.

    More than one when a check has a message on each side: then it is unclear which one the customer gets.
    """
    steps = _steps(context)
    start = (context.get("next_planned") or {}).get("step_id") if isinstance(context.get("next_planned"), dict) else None
    if start not in steps:
        return []
    level, seen = [start], set()
    for _ in range(MAX_WALK):
        found = [
            steps[i]
            for i in level
            if steps[i].get("kind") == "Communication"
            and not steps[i].get("disabled")
            and not _cfg(steps[i]).get("follow_up_for")
        ]
        if found:
            return found
        after: list[str] = []
        for step_id in level:
            seen.add(step_id)
            step = steps[step_id]
            if step.get("kind") in STOP_AT:
                continue
            for link in ("next", "yes", "no"):
                target = step.get(link) or _cfg(step).get(link)
                if target in steps and target not in seen and target not in after:
                    after.append(target)
        if not after:
            return []
        level = after
    return []


def _follow_ups_left(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Approved follow-ups the AI may still choose: written, not sent, not lined up, able to reach the customer."""
    steps = _steps(context)
    account = context.get("account") or {}
    used = _sent_step_ids(context) | set(((context.get("run") or {}).get("swaps") or {}).values())
    left = []
    for situation, step_id in ((context.get("workflow") or {}).get("follow_ups") or {}).items():
        step = steps.get(step_id)
        if step and step_id not in used and _has_text(step) and _can_send(step, account):
            left.append({**_choice(step), "for": situation, "channel": _channel(step) or CONTACT_CHANNEL})
    return left


# --------------------------------------------------------------------------- payment started


def _payment_started(context: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    until = (_time(event.get("at")) or now) + PAYMENT_TIME
    planned = context.get("next_planned") if isinstance(context.get("next_planned"), dict) else {}
    planned_at = _time(planned.get("at"))
    if until <= now:
        return _keep("Customer started a payment over a day ago; nothing to change")
    if not planned.get("step_id") or planned_at is None:
        return _keep("Customer started a payment; nothing else is planned yet")
    if planned_at >= until:
        return _keep("Customer started a payment; the next step is more than a day away")
    return _decision(
        "move_next",
        "Customer started a payment: the next step waits a day so they can finish it",
        at=until.isoformat(),
    )


# --------------------------------------------------------------------------- helpers


def _decision(
    action: str,
    reason: str,
    *,
    replace_step_id: str | None = None,
    follow_up_step_id: str | None = None,
    at: str | None = None,
    channel: str | None = None,
    decided_by: str = "rules",
    confidence: int | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "reason": reason,
        "decided_by": decided_by,
        "confidence": confidence,
        "needs_approval": False,  # the backend asks for approval of "ai" decisions for supervised clients
        "replace_step_id": replace_step_id,
        "follow_up_step_id": follow_up_step_id,
        "at": at,
        "channel": channel,
    }


def _keep(reason: str, *, ask_ai: bool = False) -> dict[str, Any]:
    decision = _decision("keep_plan", reason)
    if ask_ai:
        decision["_ask_ai"] = True  # the rules cannot settle it: the AI may look (removed before returning)
    return decision


def _steps(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    workflow = context.get("workflow") if isinstance(context.get("workflow"), dict) else {}
    return {s["id"]: s for s in workflow.get("steps") or [] if isinstance(s, dict) and s.get("id")}


def _sent_step_ids(context: dict[str, Any]) -> set[str]:
    return {m.get("step_id") for m in context.get("messages_sent") or [] if isinstance(m, dict)}


def _message(context: dict[str, Any], step_id: str | None) -> dict[str, Any] | None:
    """The latest message sent for `step_id`."""
    if not step_id:
        return None
    sent = [m for m in context.get("messages_sent") or [] if isinstance(m, dict) and m.get("step_id") == step_id]
    return sent[-1] if sent else None


def _replaceable(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in steps if _purpose(step) in REPLACEABLE_PURPOSES]


def _choice(step: dict[str, Any]) -> dict[str, Any]:
    return {"step_id": step["id"], "title": _title(step), "purpose": _purpose(step)}


def _cfg(step: dict[str, Any]) -> dict[str, Any]:
    cfg = step.get("config")
    return cfg if isinstance(cfg, dict) else {}


def _channel(step: dict[str, Any]) -> str:
    """"Email", "SMS", or "" for a contact step (no fixed channel: the worker picks Email or SMS)."""
    raw = _cfg(step).get("channel") if _cfg(step).get("channel") is not None else step.get("channel")
    channel = str(raw or "").strip()
    return "" if channel == CONTACT_CHANNEL else channel


def _has_text(step: dict[str, Any]) -> bool:
    """A follow-up without text would send the general template: it is not used until it is written."""
    return bool(str(_cfg(step).get("body") or "").strip() or str(_cfg(step).get("sms_body") or "").strip())


def _can_send(step: dict[str, Any], account: dict[str, Any]) -> bool:
    channel = _channel(step)
    if channel == "Email":
        return bool(account.get("email_ok"))
    if channel == "SMS":
        return bool(account.get("sms_ok"))
    return bool(account.get("email_ok") or account.get("sms_ok"))


def _purpose(step: dict[str, Any]) -> str:
    return str(_cfg(step).get("purpose") or step.get("purpose") or "Payment Reminder")


def _title(step: dict[str, Any]) -> str:
    return str(step.get("title") or step.get("id"))


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        at = value
    else:
        try:
            at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
