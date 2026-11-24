"""Adaptive next-best-action, the AI part: asked only when the rules cannot settle it (see next_action.py).

The AI gets a short summary of the run: what happened, the messages sent (titles, opened / clicked), the
events of the last 30 days, the next planned step, the approved follow-ups still unused. Never the customer's
name, email, phone or the message texts. It picks one action from a fixed list; the answer is checked here,
and an action that does not fit becomes "keep the plan" with the reason why.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.modules.payflow.services.workflow_suggestion.llm import call_llm

logger = logging.getLogger("app.payflow.adaptive")

# No "stop" (only a payment stops a run, the backend's safety rule) and no "pause" (same as to_operations).
AI_ACTIONS = ["keep_plan", "use_follow_up", "move_next", "switch_channel", "recheck_at", "to_operations"]
NO_CHANNEL = "None"
# Below this the AI's change is not made: the plan stays (the reason says what it suggested).
MIN_CONFIDENCE = 50
MOVE_HOURS = (1, 720)  # the next step: 1 hour to 30 days from now
RECHECK_HOURS = (1, 336)  # look again: 1 hour to 14 days from now
# Never move a message sooner than this after the last one sent.
GAP_AFTER_LAST_MESSAGE = timedelta(hours=24)
HISTORY_LIMIT = 30
NOT_SHOWN = frozenset({"email_delivered", "sms_delivered"})

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "reason", "confidence"],
    "properties": {
        "action": {"type": "string", "enum": AI_ACTIONS},
        "follow_up_step_id": {"type": "string"},
        "replace_step_id": {"type": "string"},
        "in_hours": {"type": "number"},
        "channel": {"type": "string", "enum": ["Email", "SMS", NO_CHANNEL]},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
}

SYSTEM_PROMPT = f"""You check one customer's collection plan in PayFlow, a collections platform, after something
happened that the fixed rules could not settle (or Operations asked you to look again). The plan is a workflow
the client approved. You may only adjust it with one action:
- "keep_plan": nothing to change. Choose it whenever you are not sure.
- "use_follow_up": send one of follow_ups_available (follow_up_step_id) in place of one of next_message_choices
  (replace_step_id). It goes out at that message's planned time, so the customer gets no extra message. Only
  when what the follow-up is "for" fits what the customer did.
- "move_next": move the next planned step to in_hours from now ({MOVE_HOURS[0]}-{MOVE_HOURS[1]}). Later when the
  customer needs time; sooner only when they are clearly engaging, and never sooner than
  {int(GAP_AFTER_LAST_MESSAGE.total_seconds() // 3600)} hours after the last message sent.
- "switch_channel": send the messages after this by "Email" or "SMS" first. Only a channel the customer can be
  reached on (email_ok / sms_ok), e.g. after several emails were not opened.
- "recheck_at": change nothing now, look again in in_hours ({RECHECK_HOURS[0]}-{RECHECK_HOURS[1]}).
- "to_operations": a person should look at the case: repeated contact without any result, a pattern the plan
  does not fit, anything unusual.
Never write new messages and never contact the customer more often than the plan does.
Opens marked machine_open are the mail app, not the customer.
reason: one or two plain sentences for Operations with the facts that decided it.
confidence: 0-100, how sure you are; below 60 when the facts are thin or point both ways.
Answer with JSON only."""


def ask_ai(
    context: dict[str, Any],
    *,
    what_happened: str,
    choices: list[dict[str, Any]],
    follow_ups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The AI's decision (action, reason, confidence, ...), or None when it could not be asked."""
    facts = _facts(context, what_happened=what_happened, choices=choices, follow_ups=follow_ups)
    try:
        answer = call_llm(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Customer's plan:\n" + json.dumps(facts, ensure_ascii=False)},
            ],
            SCHEMA,
            quick=True,
        )
    except Exception as exc:  # noqa: BLE001 - any failure: the rules' answer stands
        logger.warning("adaptive AI unavailable run_id=%s: %s", (context.get("run") or {}).get("id"), exc)
        return None
    if not isinstance(answer, dict):
        return None
    return _check(answer, context, choices=choices, follow_ups=follow_ups)


# --------------------------------------------------------------------------- the answer


def _check(
    answer: dict[str, Any],
    context: dict[str, Any],
    *,
    choices: list[dict[str, Any]],
    follow_ups: list[dict[str, Any]],
) -> dict[str, Any]:
    action = str(answer.get("action") or "").strip()
    reason = " ".join(str(answer.get("reason") or "").split())[:500]
    confidence = _confidence(answer.get("confidence"))
    decision: dict[str, Any] = {"action": action, "reason": reason, "confidence": confidence}

    if action not in AI_ACTIONS:
        return _not_used(action, "it is not an action the AI may take", confidence)
    if not reason:
        return _not_used(action, "it gave no reason", confidence)
    if action == "keep_plan":
        return decision
    if confidence is not None and confidence < MIN_CONFIDENCE:
        return {"action": "keep_plan", "reason": f"The AI was not sure ({confidence}%): {reason} The plan stays.",
                "confidence": confidence}

    now = datetime.now(timezone.utc)
    if action == "use_follow_up":
        follow_id = str(answer.get("follow_up_step_id") or "").strip()
        replace_id = str(answer.get("replace_step_id") or "").strip()
        if not replace_id and len(choices) == 1:
            replace_id = choices[0]["step_id"]
        if follow_id not in {f["step_id"] for f in follow_ups}:
            return _not_used(action, "that follow-up is not available", confidence)
        if replace_id not in {c["step_id"] for c in choices}:
            return _not_used(action, "that message is not the next one", confidence)
        return {**decision, "follow_up_step_id": follow_id, "replace_step_id": replace_id}

    if action in {"move_next", "recheck_at"}:
        low, high = MOVE_HOURS if action == "move_next" else RECHECK_HOURS
        hours = _number(answer.get("in_hours"))
        if hours is None or not low <= hours <= high:
            return _not_used(action, f"the time must be {low}-{high} hours from now", confidence)
        at = now + timedelta(hours=hours)
        if action == "move_next":
            planned = context.get("next_planned") if isinstance(context.get("next_planned"), dict) else {}
            if not planned.get("step_id"):
                return _not_used(action, "nothing is planned to move", confidence)
            planned_at = _time(planned.get("at"))
            last_sent = _last_sent(context)
            if planned_at and at < planned_at and last_sent and at < last_sent + GAP_AFTER_LAST_MESSAGE:
                return _not_used(action, "it would be too soon after the last message", confidence)
        return {**decision, "at": at.isoformat()}

    if action == "switch_channel":
        channel = str(answer.get("channel") or "").strip()
        account = context.get("account") or {}
        reachable = {"Email": account.get("email_ok"), "SMS": account.get("sms_ok")}
        if not reachable.get(channel):
            return _not_used(action, "the customer cannot be reached on that channel", confidence)
        if (context.get("run") or {}).get("preferred_channel") == channel:
            return _not_used(action, f"{channel} is already used first", confidence)
        return {**decision, "channel": channel}

    return decision  # to_operations


def _not_used(action: str, why: str, confidence: int | None) -> dict[str, Any]:
    return {"action": "keep_plan", "reason": f"The AI suggested '{action}' but {why}; the plan stays.",
            "confidence": confidence}


# --------------------------------------------------------------------------- what the AI sees


def _facts(
    context: dict[str, Any],
    *,
    what_happened: str,
    choices: list[dict[str, Any]],
    follow_ups: list[dict[str, Any]],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    workflow = context.get("workflow") if isinstance(context.get("workflow"), dict) else {}
    titles = {s["id"]: s.get("title") or s["id"] for s in workflow.get("steps") or [] if isinstance(s, dict) and s.get("id")}
    account = context.get("account") or {}
    run = context.get("run") or {}
    planned = context.get("next_planned") if isinstance(context.get("next_planned"), dict) else None

    def event(e: dict[str, Any]) -> dict[str, Any]:
        item = {"type": e.get("type"), "hours_ago": _hours(now, e.get("at"))}
        if e.get("step_id"):
            item["message"] = titles.get(e["step_id"], e["step_id"])
        if isinstance(e.get("detail"), dict) and e["detail"].get("machine_open"):
            item["machine_open"] = True
        return item

    recent = [e for e in context.get("recent_events") or [] if isinstance(e, dict) and e.get("type") not in NOT_SHOWN]
    return {
        "trigger": context.get("trigger"),
        "what_happened": what_happened,
        "new_events": [event(e) for e in context.get("new_events") or [] if isinstance(e, dict)],
        "last_30_days": [event(e) for e in recent[:HISTORY_LIMIT]],
        "messages_sent": [
            {
                "message": titles.get(m.get("step_id"), m.get("step_id")),
                "channel": m.get("channel"),
                "days_ago": _days(now, m.get("sent_at")),
                "status": m.get("status"),
                "opened": bool(m.get("opened")),
                "clicked": bool(m.get("clicked")),
            }
            for m in context.get("messages_sent") or []
            if isinstance(m, dict)
        ],
        "next_planned": (
            {
                "step_id": planned.get("step_id"),
                "title": titles.get(planned.get("step_id"), planned.get("step_id")),
                "kind": planned.get("kind"),
                "in_hours": -1 * (_hours(now, planned.get("at")) or 0),
            }
            if planned and planned.get("step_id")
            else None
        ),
        "next_message_choices": choices,
        "follow_ups_available": follow_ups,
        "customer": {
            "days_past_due": account.get("days_past_due"),
            "outstanding_balance": account.get("outstanding_balance"),
            "payment_status": account.get("payment_status"),
            "collection_status": account.get("collection_status"),
            "email_ok": bool(account.get("email_ok")),
            "sms_ok": bool(account.get("sms_ok")),
            "channel_used_first": run.get("preferred_channel"),
            "messages_last_7_days": (context.get("limits") or {}).get("messages_sent_7_days"),
        },
        "workflow_started_days_ago": _days(now, run.get("started_at")),
        "earlier_decisions": [
            {"action": d.get("action"), "status": d.get("status"), "by": d.get("decided_by"),
             "days_ago": _days(now, d.get("at"))}
            for d in context.get("decisions") or []
            if isinstance(d, dict)
        ],
    }


def _last_sent(context: dict[str, Any]) -> datetime | None:
    times = [_time(m.get("sent_at")) for m in context.get("messages_sent") or [] if isinstance(m, dict)]
    times = [t for t in times if t]
    return max(times) if times else None


def _hours(now: datetime, value: Any) -> float | None:
    at = _time(value)
    return round((now - at).total_seconds() / 3600, 1) if at else None


def _days(now: datetime, value: Any) -> float | None:
    hours = _hours(now, value)
    return round(hours / 24, 1) if hours is not None else None


def _confidence(value: Any) -> int | None:
    number = _number(value)
    return int(max(0.0, min(100.0, number))) if number is not None else None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        at = value
    else:
        try:
            at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
