"""Adaptive next-best-action: the AI team's side of the handover (ADAPTIVE_WORKFLOWS_BE.md §5).

The backend calls ``decide_next_action(context)`` after its safety rules (paid in full → stop, unreachable →
Operations, email or SMS gone → switch channel) and carries out what it returns.

1. Fixed rules first, no AI call:
   - A known situation → the workflow's approved follow-up for it, sent in place of the next planned message
     (same time, so no extra message). The first time its first-time text, the next time its repeat text
     (config.follow_up_level 2); each is used once, counted when it was actually sent (a message that failed
     or was blocked does not use it up). With every text of the situation sent, the AI may look.
       opened, not clicked → "opened_not_clicked"     clicked, not paid / left the payment page → "clicked_not_paid"
       not opened in time → "email_not_opened" (by SMS first when it can)   part paid → "payment_partial"
       payment did not go through → "payment_failed"   no activity → "no_activity"
       began setting up a payment plan, not finished (timer, or left that checkout) → "plan_setup_not_finished"
       no activity after the part payment's first follow-up → the part payment's repeat text
     After a failed or a part payment there is no contact for a while (settings ADAPTIVE_PAYMENT_FAILED_WAIT_HOURS,
     ADAPTIVE_PART_PAID_WAIT_HOURS): a next message due sooner waits until then.
   - Payment started → the next step waits until the customer has had a day to finish.
   - The customer opened or clicked since a "not opened", "opened, not paid" or "no response" follow-up was lined
     up, or set up a payment plan → it no longer fits: it is dropped and the planned message goes (drop_follow_up).
   - Anything else → keep the plan.
   The follow-ups were approved with the workflow, so these are "rules" decisions: no approval per customer.
2. The AI (next_action_ai.py) only where the rules cannot settle it: a situation with no follow-up left to use,
   a next message that is unclear, or Operations pressing "Reassess plan". Never at send time, at most
   settings.ADAPTIVE_AI_CALLS_PER_WEEK times a week per run. It picks one of the same actions; "ai" decisions
   need approval for supervised clients (the backend asks). When the AI cannot be asked, the rules' answer stands.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings
from app.modules.payflow.services.strategy_service import follow_up_level
from app.modules.payflow.services.workflow_suggestion.next_action_ai import ask_ai
from app.shared.enums import PayflowCommStatus, PayflowPaymentOptionType

CONTACT_CHANNEL = "Email + SMS"  # same as strategy_service.CONTACT_CHANNEL
# Planned messages a follow-up may take the place of: the ordinary reminders, not the promise / plan /
# contact-details ones (those are written for a customer in another situation).
REPLACEABLE_PURPOSES = frozenset({"Payment Reminder", "Firm Reminder", "Payment Link"})
# A started payment: no reminder for a day (a Stripe checkout stays open 24 hours).
PAYMENT_TIME = timedelta(hours=24)
# Steps the plan does not go past on its own when looking for the next message.
STOP_AT = frozenset({"Human Review", "Outcome", "End"})
MAX_WALK = 12

# The plan is right as it is: the workflow's own payment checks / Operations take it from here.
KEEP_FOR = {
    "payment_completed": "Customer paid",
    "plan_created": "Customer set up a payment plan; the workflow's payment checks take it from here",
}
PLAN_SETUP = "plan_setup_not_finished"
# Event → the follow-up situation that answers it (see _situation for the two exceptions).
FOLLOW_UP_FOR = {
    "payment_partial": "payment_partial",
    # Operations also get an exception for it (the worker); the customer gets a "try again" message.
    "payment_failed": "payment_failed",
    PLAN_SETUP: PLAN_SETUP,
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
    PLAN_SETUP,
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
    PLAN_SETUP: "Customer began setting up a payment plan but did not finish",
    "payment_abandoned": "Customer left the payment page without paying",
    "clicked_not_paid": "Customer opened the payment page but has not paid",
    "opened_not_clicked": "Customer opened the email but did not click the payment link",
    "email_not_opened": "Customer has not opened the email",
    "no_activity": "No activity from the customer",
}
# No contact this soon after the event (the setting's hours): time to try again, or to pay the rest.
WAIT_AFTER = {
    "payment_failed": "ADAPTIVE_PAYMENT_FAILED_WAIT_HOURS",
    "payment_partial": "ADAPTIVE_PART_PAID_WAIT_HOURS",
}
# What the customer did → the lined-up follow-ups it answers (they no longer fit, so they are dropped).
_CLICK_ANSWERS = frozenset({"email_not_opened", "opened_not_clicked", "no_activity"})
ANSWERED_BY = {
    "email_opened": frozenset({"email_not_opened", "no_activity"}),
    "email_clicked": _CLICK_ANSWERS,
    "payment_link_clicked": _CLICK_ANSWERS,
    "payment_page_opened": _CLICK_ANSWERS,
    "plan_created": _CLICK_ANSWERS | {"clicked_not_paid", PLAN_SETUP},
}
ACTED_TEXT = {
    "email_opened": "opened an email",
    "email_clicked": "clicked a link in an email",
    "payment_link_clicked": "clicked the payment link",
    "payment_page_opened": "opened the payment page",
    "plan_created": "set up a payment plan",
}
# A message that failed, was cancelled or blocked (e.g. by a compliance rule) never reached the customer.
NOT_SENT = frozenset(
    {PayflowCommStatus.FAILED.value, PayflowCommStatus.CANCELLED.value, PayflowCommStatus.SUPPRESSED.value}
)


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
        return _drop_answered_follow_up(context, events) or _keep(
            f"No change needed for: {', '.join(sorted({str(e.get('type')) for e in events}))}"
        )
    kind = event["type"]
    if kind in KEEP_FOR:
        return _drop_answered_follow_up(context, [event]) or _keep(KEEP_FOR[kind])
    if kind == "payment_started":
        return _payment_started(context, event)
    return _follow_up(context, event, _situation(context, kind))


def _situation(context: dict[str, Any], kind: str) -> str:
    """The follow-up situation that answers `kind`. Leaving a checkout that was setting up a payment plan is
    "plan setup not finished"; silence after a part payment gets the part payment's repeat text (when the
    workflow has those follow-ups - else the usual ones)."""
    situation = FOLLOW_UP_FOR[kind]
    has_plan_setup = bool(_follow_up_levels(context, PLAN_SETUP))
    if situation == PLAN_SETUP and not has_plan_setup:
        return "clicked_not_paid"
    if situation == "clicked_not_paid" and has_plan_setup and _setting_up_plan(context):
        return PLAN_SETUP
    if situation == "no_activity" and _repeat_left(context, "payment_partial"):
        return "payment_partial"
    return situation


def _may_ask_ai(context: dict[str, Any], *, manual: bool) -> bool:
    """Never at send time (the send would wait); otherwise at most ADAPTIVE_AI_CALLS_PER_WEEK per run
    (Operations' "Reassess plan" always asks)."""
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
    return len(asked) < settings.ADAPTIVE_AI_CALLS_PER_WEEK


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

    levels = _follow_up_levels(context, situation)
    if not levels:
        return _keep(f"{why}; this workflow has no approved follow-up for it", ask_ai=True)
    run = context.get("run") or {}
    swaps = run.get("swaps") or {}
    # The first time its first-time text, the next time its repeat text, ... - each once, when actually sent.
    sent = _sent_step_ids(context)
    follow = next((step for step in levels if step["id"] not in sent), None)
    if follow is None:
        titles = ", ".join(f"'{_title(step)}'" for step in levels)
        return _keep(f"{why}; every follow-up for it was already sent ({titles})", ask_ai=True)
    follow_id = follow["id"]
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
    if not _in_customers_language(follow, planned, account):
        return _keep(f"{why}; its follow-up '{_title(follow)}' is not written in the customer's language, so "
                     f"'{_title(planned)}' goes as planned")

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
    wait = _contact_wait(context, event)
    if wait:
        reason += f", not sooner than {wait[1]} hours after it"
    return _decision(
        "use_follow_up",
        reason,
        replace_step_id=planned["id"],
        follow_up_step_id=follow_id,
        channel=channel,
        at=wait[0].isoformat() if wait else None,
    )


def _in_customers_language(follow: dict[str, Any], planned: dict[str, Any], account: dict[str, Any]) -> bool:
    """The follow-up reaches the customer in the language the planned message would (its translations): not a
    follow-up in the main language in place of a message written in theirs."""
    language = str(account.get("language") or "").strip().upper()

    def written_in(step: dict[str, Any]) -> bool:
        return language in (_cfg(step).get("translations") or {})

    return not language or written_in(follow) or not written_in(planned)


def _contact_wait(context: dict[str, Any], event: dict[str, Any]) -> tuple[datetime, int] | None:
    """After a failed or a part payment: (end of the no-contact wait, its hours) when the next step is due
    sooner - the follow-up then goes at the end of the wait. None: no wait for this event, or nothing that soon."""
    setting = WAIT_AFTER.get(event["type"])
    if not setting:
        return None
    hours = int(getattr(settings, setting))
    until = (_time(event.get("at")) or datetime.now(timezone.utc)) + timedelta(hours=hours)
    planned = context.get("next_planned") if isinstance(context.get("next_planned"), dict) else {}
    planned_at = _time(planned.get("at"))
    if planned_at is None or planned_at >= until:
        return None
    return until, hours


def _drop_answered_follow_up(context: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A lined-up follow-up the customer answered since (an open or a click): it is dropped, so the planned
    message goes. A mail app's own open is not the customer."""
    acted = [
        e for e in events
        if e.get("type") in ANSWERED_BY and not (isinstance(e.get("detail"), dict) and e["detail"].get("machine_open"))
    ]
    if not acted:
        return None
    steps = _steps(context)
    swaps = (context.get("run") or {}).get("swaps") or {}
    for planned_id, follow_id in sorted(swaps.items()):
        follow = steps.get(follow_id) or {}
        answered = next((e for e in acted if _cfg(follow).get("follow_up_for") in ANSWERED_BY[e["type"]]), None)
        if answered is None:
            continue
        planned = steps.get(planned_id) or {"id": planned_id}
        return _decision(
            "drop_follow_up",
            f"Customer {ACTED_TEXT[answered['type']]} since the follow-up '{_title(follow)}' was lined up: it no "
            f"longer fits, so '{_title(planned)}' goes as planned",
            replace_step_id=planned_id,
            follow_up_step_id=follow_id,
        )
    return None


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
    """The next planned message(s) of the run (see messages_after)."""
    planned = context.get("next_planned") if isinstance(context.get("next_planned"), dict) else {}
    return messages_after(_steps(context), planned.get("step_id"))


def messages_after(steps: dict[str, dict[str, Any]], start: str | None) -> list[dict[str, Any]]:
    """The planned message(s) a run reaches first from step `start` ({step_id: step}): `start` itself when it is a
    message, else the first messages after it, past waits and checks (never a follow-up).

    More than one when a check has a message on each side: then it is unclear which one the customer gets.
    """
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
    """Approved follow-ups the AI may still choose: per situation with nothing lined up, its next text not sent
    yet (first time, then repeat) - when it is written and can reach the customer."""
    account = context.get("account") or {}
    sent = _sent_step_ids(context)
    lined_up = set(((context.get("run") or {}).get("swaps") or {}).values())
    left = []
    for situation in dict.fromkeys(_cfg(step).get("follow_up_for") for step in _follow_up_steps(context)):
        levels = _follow_up_levels(context, situation)
        if any(step["id"] in lined_up for step in levels):
            continue
        step = next((step for step in levels if step["id"] not in sent), None)
        if step and _has_text(step) and _can_send(step, account):
            left.append({**_choice(step), "for": situation, "level": _level_name(step),
                         "channel": _channel(step) or CONTACT_CHANNEL})
    return left


def _follow_up_steps(context: dict[str, Any]) -> list[dict[str, Any]]:
    """The workflow's adaptive follow-ups (message steps off the flowchart with config.follow_up_for)."""
    return [
        step for step in _steps(context).values()
        if step.get("kind") == "Communication" and _cfg(step).get("follow_up_for") in FOLLOW_UP_FOR.values()
    ]


def _follow_up_levels(context: dict[str, Any], situation: str) -> list[dict[str, Any]]:
    """`situation`'s follow-ups, first-time text first, then its repeat texts."""
    return sorted(
        (step for step in _follow_up_steps(context) if _cfg(step).get("follow_up_for") == situation),
        key=follow_up_level,
    )


def _level_name(step: dict[str, Any]) -> str:
    level = follow_up_level(step)
    return "first time" if level == 1 else "repeat" if level == 2 else f"repeat {level - 1}"


def _repeat_left(context: dict[str, Any], situation: str) -> bool:
    """`situation`'s first-time follow-up was sent, and a repeat text of it was not."""
    levels = _follow_up_levels(context, situation)
    sent = _sent_step_ids(context)
    return bool(levels) and levels[0]["id"] in sent and any(step["id"] not in sent for step in levels[1:])


def _setting_up_plan(context: dict[str, Any]) -> bool:
    """The customer's latest checkout was setting up a payment plan, and no plan was set up since."""
    events = [
        e for e in [*(context.get("recent_events") or []), *(context.get("new_events") or [])] if isinstance(e, dict)
    ]
    starts = [e for e in events if e.get("type") == "payment_started" and _time(e.get("at"))]
    if not starts:
        return False
    latest = max(starts, key=lambda e: _time(e["at"]))
    detail = latest.get("detail") if isinstance(latest.get("detail"), dict) else {}
    if detail.get("option_type") != PayflowPaymentOptionType.PAYMENT_PLAN.value:
        return False
    started_at = _time(latest["at"])
    return not any(e.get("type") == "plan_created" and (_time(e.get("at")) or started_at) >= started_at for e in events)


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
    """Steps whose message reached the customer (or is on its way) - not the ones that failed or were blocked."""
    return {
        m.get("step_id")
        for m in context.get("messages_sent") or []
        if isinstance(m, dict) and m.get("status") not in NOT_SENT
    }


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
