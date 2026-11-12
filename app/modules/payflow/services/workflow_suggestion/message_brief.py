"""Plan how each message is written for each audience - the first of the two AI calls that write a
workflow's messages (the second, message_writer.py, writes the text from the plan).

For every message and every audience (audiences.py: the main text, and each audience's own version) the AI
returns a brief: approach, tone, length, subject style, which payment option to lead with, the call to
action - and why that should get these customers to pay. It sees totals only, never one person.

Code sets the limits and checks every brief; problems go back to the AI. Firmness comes from what the
customer did (message_tone.py): the AI may keep the code's tone or make it one step softer, never firmer.
A brief that stays invalid is left out, and when the AI cannot be reached there are no briefs: the
messages are then written as before. Planning never blocks writing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.exceptions import AppError
from app.modules.payflow.services.strategy_service import _timing_label
from app.modules.payflow.services.workflow_suggestion.config import CONTACT_CHANNEL
from app.modules.payflow.services.workflow_suggestion.llm import _enum, _object, call_llm
from app.modules.payflow.services.workflow_suggestion.message_prompt import SITUATION_NOTES, _workflow_lines
from app.modules.payflow.services.workflow_suggestion.message_tone import (
    FINAL_NOTICE,
    LADDER,
    NEUTRAL,
    SUPPORTIVE,
    message_tones,
)
from app.modules.payflow.services.workflow_suggestion.message_validator import _TYPED_MONEY
from app.shared.enums import PayflowCollectionStatus

logger = logging.getLogger("app.payflow.message_brief")

MAIN = "main"  # the audience key of the main text
MAX_FIX_ATTEMPTS = 2  # after the first answer, invalid briefs are sent back this many times
# Email length per brief, in words (within message_validator.EMAIL_WORDS). The SMS version is always short.
LENGTHS = {"short": (40, 80), "medium": (80, 130), "long": (130, 180)}
NO_LEAD = "none"
PLAN_OPTION = "set up a payment plan"  # context.load_payment_options label
TEXT_FIELDS = ("approach", "subject_style", "call_to_action", "why")
WRITER_FIELDS = ("approach", "subject_style", "call_to_action")  # the writer follows these; why is for reviewers
MAX_TEXT_CHARS = 400  # each text field is a sentence or two
ALL_TONES = [*LADDER, FINAL_NOTICE, SUPPORTIVE, NEUTRAL]

# What is different where the customers live - facts only, for the countries PayFlow serves.
# A new country: add its notes here (and its accounts need a country_code).
COUNTRY_NOTES = {
    "Canada": (
        "amounts are in Canadian dollars. Customers in Quebec mostly read French, elsewhere mostly English "
        "(each customer gets their own language). People expect a polite, plain and direct message with clear "
        "options. Each province sets collection rules: never threaten, harass or mislead (PayFlow's compliance "
        "rules handle when messages may be sent)."
    ),
}
UNKNOWN_COUNTRY_NOTE = "no notes for this country - keep it neutral, polite and plain."


def allowed_tones(code_tone: str) -> list[str]:
    """The code's tone first, then one step softer on the Friendly → Clear → Firm ladder. Final notice,
    Supportive and Neutral are fixed: they say what the message is (the last one before people review the
    case, encouragement of an arrangement, not a payment request)."""
    if code_tone in LADDER:
        level = LADDER.index(code_tone)
        return [code_tone, *([LADDER[level - 1]] if level else [])]
    return [code_tone]


def plan_messages(
    client: dict[str, Any],
    workflow: dict[str, Any],
    segment: dict[str, Any],
    found: dict[str, Any],
    *,
    step_ids: list[str] | None = None,
) -> dict[str, dict[str, dict[str, str]]]:
    """{step_id: {audience: brief}} for the workflow's messages (default all; `step_ids`: only these).

    `client`: load_client_context + "payment_options". `segment`: {"label", "segment", "stats"}.
    `found`: audiences.find_audiences(...). Briefs that stay invalid are left out; {} when the AI fails.
    """
    steps = {step["id"]: step for step in workflow.get("steps") or [] if step.get("id")}
    stats = segment.get("stats") or {}
    tones = message_tones(workflow, segment.get("segment") or {}, stats.get("avg_days_past_due"))
    messages = [step_id for step_id in tones if step_ids is None or step_id in step_ids]
    audiences = [MAIN, *(audience["key"] for audience in found.get("audiences") or [])]
    pending = [(step_id, audience) for step_id in messages for audience in audiences]
    options = list(client.get("payment_options") or [])
    situation = (segment.get("segment") or {}).get("situation")

    plans: dict[str, dict[str, dict[str, str]]] = {}
    feedback: list[dict[str, Any]] = []
    for _ in range(1 + MAX_FIX_ATTEMPTS):
        if not pending:
            break
        asked = {step_id for step_id, _ in pending}
        lines = [_message_line(steps[step_id], tones[step_id]) for step_id in messages if step_id in asked]
        prompt = build_brief_prompt(client, workflow, segment, found, lines, pending, feedback)
        schema = build_brief_schema(sorted({p[0] for p in pending}), sorted({p[1] for p in pending}), options)
        try:
            reply = _briefs_by_pair(call_llm(prompt, schema))
        except AppError as exc:
            logger.warning("Message briefs not available for workflow %s: %s", workflow.get("name"), exc)
            break
        feedback = []
        for step_id, audience in pending:
            brief = _clean(reply.get((step_id, audience)))
            label = f"{step_id} ({audience})"
            errors = check_brief(label, brief, allowed_tones(tones[step_id]["tone"]), options, situation)
            if errors:
                feedback.append({"step_id": step_id, "audience": audience, "brief": brief, "errors": errors})
            else:
                plans.setdefault(step_id, {})[audience] = brief
        pending = [(item["step_id"], item["audience"]) for item in feedback]
    if pending:
        logger.warning("Message briefs left out for workflow %s: %s", workflow.get("name"), pending)
    return plans


def check_brief(
    label: str, brief: dict[str, str] | None, allowed: list[str], options: list[str], situation: str | None
) -> list[str]:
    """Problems with one brief - [] when it can be used. `allowed`: allowed_tones(code tone)."""
    if not brief:
        return [f"{label}: no brief was written"]
    errors = []
    if brief.get("tone") not in allowed:
        errors.append(
            f"{label}: tone must be {' or '.join(allowed)} - it comes from what the customer did - not "
            f"{brief.get('tone') or 'empty'}"
        )
    if brief.get("length") not in LENGTHS:
        errors.append(f"{label}: length must be one of {', '.join(LENGTHS)}")
    lead = brief.get("lead_option")
    if allowed == [NEUTRAL]:
        if lead != NO_LEAD:
            errors.append(f'{label}: this message is not a payment request - lead_option must be "{NO_LEAD}"')
    elif options and lead not in options:
        errors.append(f"{label}: lead_option must be one the payment page offers: {', '.join(options)}")
    elif situation == PayflowCollectionStatus.PAYMENT_PLAN.value and lead == PLAN_OPTION:
        errors.append(f"{label}: these customers are already on a payment plan - never lead with a new plan")
    errors += [f"{label}: {field} is empty" for field in TEXT_FIELDS if not brief.get(field)]
    errors += [
        f'{label}: {field} types an amount - the numbers are averages, not one customer\'s: say "their balance"'
        for field in WRITER_FIELDS
        if _TYPED_MONEY.search(brief.get(field) or "")
    ]
    return errors


def build_brief_schema(step_ids: list[str], audiences: list[str], options: list[str]) -> dict[str, Any]:
    """Keys in the order the prompt explains them (Together AI writes keys in schema order)."""
    text = {"type": "string"}
    fields = {
        "step_id": _enum(step_ids),
        "audience": _enum(audiences),
        "approach": text,
        "tone": _enum(ALL_TONES),
        "length": _enum(list(LENGTHS)),
        "subject_style": text,
        "lead_option": _enum([*options, NO_LEAD]),
        "call_to_action": text,
        "why": text,
    }
    brief = _object(fields, required=list(fields))
    return _object({"briefs": {"type": "array", "items": brief}}, required=["briefs"])


def build_brief_prompt(
    client: dict[str, Any],
    workflow: dict[str, Any],
    segment: dict[str, Any],
    found: dict[str, Any],
    message_lines: list[str],
    pending: list[tuple[str, str]],
    feedback: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    parts = [
        _client_line(client),
        _workflow_lines(workflow),
        _segment_line(segment),
        _audience_lines(found),
        _country_lines(found),
        "Messages (in order along the workflow):\n" + "\n".join(message_lines),
        "Write one brief for each message and audience: "
        + "; ".join(
            f"{step_id} → {', '.join(audience for s, audience in pending if s == step_id)}"
            for step_id in dict.fromkeys(step_id for step_id, _ in pending)
        ),
    ]
    if feedback:
        lines = ["Your previous briefs for these had problems. Fix every problem:"]
        for item in feedback:
            if item.get("brief"):
                lines.append(json.dumps({"step_id": item["step_id"], "audience": item["audience"], **item["brief"]},
                                        ensure_ascii=False))
            lines.append("Problems:\n" + "\n".join(f"- {error}" for error in item["errors"]))
        parts.append("\n".join(lines))
    return [
        {"role": "system", "content": build_brief_system_prompt()},
        {"role": "user", "content": "\n\n".join(part for part in parts if part)},
    ]


def build_brief_system_prompt() -> str:
    low_short, high_short = LENGTHS["short"]
    low_medium, high_medium = LENGTHS["medium"]
    low_long, high_long = LENGTHS["long"]
    return f"""You plan the emails and SMS messages of a debt-collection workflow for PayFlow, a collections platform.
Goal: for each audience, the message most likely to get the customer to pay - in full, in part or with a payment
plan - while treating them with respect. A writer writes the text from your plan; every customer of an audience
gets the same text, filled with their own details. Reply with JSON only, matching the provided schema.

## Your plan: one brief per message (step_id) and audience
- "main" is the main text: written for the accounts described as main, and sent to anyone without a version of
  their own. Every other audience gets its own version of every message.
- approach: how to write for this audience - voice, warmth, formality, structure. One or two sentences.
- tone: one of the message's allowed_tones. The first is the tone the rules chose from what the customer did; a
  second one, when listed, is one step softer.
- length of the email: short ({low_short}-{high_short} words), medium ({low_medium}-{high_medium}) or long \
({low_long}-{high_long}). The SMS version is always short.
- subject_style: what the subject line should do and sound like - not the subject itself.
- lead_option: the payment option to put first - one the payment page offers - or "{NO_LEAD}" when the message is
  not a payment request.
- call_to_action: the one action, and how the text leads to it.
- why: one or two sentences - why this should get these customers to pay, from their numbers.

## How customers see the message
- Email: the text, then a box showing their balance, then a "Pay Now" button that opens their own payment page.
  The text leads to that button; a shorter text brings it into view sooner.
- SMS: the payment link is in the text itself.
- The payment page is the only action: never ask them to reply, call or write.
- channel tells what each message is: "Email", "SMS", or "{CONTACT_CHANNEL}" (an email and an SMS version - PayFlow
  sends whichever reaches the customer). An SMS has no subject: its subject_style is "none".

## What gets customers to pay
- One clear ask that is easy to act on.
- Lead with what most of the audience can do now: balances mostly $1,500 and over -> a payment plan or paying part
  now first (paying in full can still be mentioned); balances mostly under $500 -> paying in full, it takes a
  minute. Only options the payment page offers.
- Be specific and calm: their balance, their own secure payment page, what to do next.
- Respect works better than pressure: no blame; acknowledge any payment or arrangement first.
- Easy to read on a phone: plain words, short sentences, short paragraphs.
- Subjects: clear, specific to their account, calm, and in the message's tone - a Friendly or Clear subject is a
  reminder, not a demand (no "action needed", "important" or "urgent"). No words in capitals only, no "!!!";
  "final" only in a Final notice.
- The audience numbers are shares and averages for the whole audience, not facts about one customer: use them to
  decide, and quote them in why - but never put an amount in approach, subject_style or call_to_action (the writer
  follows those): say "their balance".
- Each message on a path takes a new angle instead of repeating the one before (for example: easy online
  payment -> the plan option -> what happens next). The path_so_far shows what came before.
- Promise to Pay: remind them of their own promise. Already on a payment plan: help them keep up - never offer a
  new plan. Partly paid: thank them first.

## Fit the audience: the style - never the facts or the firmness
Choose the approach from the audience's numbers (age, region, country, languages, balance, days late):
- 18 - 34: short and direct, made for a phone; a lighter, modern voice is fine.
- 35 - 64: clear and practical; get to the point and show the options.
- 65 and over: respectful, a little more formal, warm and reassuring; one step at a time, and say that the
  payment page shows every option; never rushed.
- Mixed or unknown ages: plain and clear for everyone.
Follow the country notes for the countries the customers live in.

## Hard rules
- Tone: only the message's allowed_tones. They come from what the customer did (how late, what happened); never
  make a message firmer because of who the customer is.
- No guilt, fear, shame or pressure; no threats or hinted consequences; no "act now" urgency.
- Never mention or hint at the customer's age, where they live or which group they are in (no "as a senior",
  "young people like you"), and never guess at their personal life, health, money troubles or family.
- 65 and over: extra care - no urgency and nothing complicated.
- A light voice never makes light of the debt: no jokes, no slang about money, no emojis.
- There are no results from earlier messages yet: base every choice on the numbers and the practice above, and
  never claim results."""


def _client_line(client: dict[str, Any]) -> str:
    name = client.get("name") or "the client"
    if client.get("client_type") == "first_party":
        sender = f"{name} sends its own messages ({{{{brand_name}}}})"
    else:
        sender = f"a collection agency ({{{{brand_name}}}}) writes on behalf of {name}"
    options = client.get("payment_options")
    return (
        f"Client: {name} - {sender}. Messages are written in {client.get('language') or 'English'}; customers who "
        f"read another language get a translation. Currency: {client.get('currency') or 'CAD'}.\n"
        f"Payment page: the customer can {', '.join(options) if options else 'pay online'}."
    )


def _segment_line(segment: dict[str, Any]) -> str:
    note = SITUATION_NOTES.get((segment.get("segment") or {}).get("situation"))
    return f"Segment: {segment.get('label') or 'Unknown'}" + (f"\n{note}" if note else "")


def _audience_lines(found: dict[str, Any]) -> str:
    main = found.get("main") or {}
    lines = [
        "Audiences (totals only - shares in % of the accounts):",
        f"- {MAIN} (the main text): {main.get('accounts', 0)} accounts; also sent to anyone without a version of "
        f"their own. {_profile_json(main.get('profile'))}",
    ]
    for audience in found.get("audiences") or []:
        lines.append(
            f"- {audience['key']} ({audience['label']}): {audience['accounts']} accounts. "
            f"{_profile_json(audience.get('profile'))}"
        )
    return "\n".join(lines)


def _country_lines(found: dict[str, Any]) -> str:
    countries = [name for name in ((found.get("group") or {}).get("countries") or {}) if name != "Unknown"]
    if not countries:
        return ""
    return "Country notes:\n" + "\n".join(
        f"- {name}: {COUNTRY_NOTES.get(name, UNKNOWN_COUNTRY_NOTE)}" for name in countries
    )


def _profile_json(profile: dict[str, Any] | None) -> str:
    shown = {key: value for key, value in (profile or {}).items() if key != "accounts" and value is not None}
    return json.dumps(shown, ensure_ascii=False)


def _message_line(step: dict[str, Any], tone: dict[str, str]) -> str:
    cfg = step.get("config") or {}
    line = {
        "step_id": step["id"],
        "title": step.get("title"),
        "channel": cfg.get("channel") or step.get("channel") or CONTACT_CHANNEL,
        "purpose": cfg.get("purpose") or step.get("purpose"),
        "timing": _timing_label(step),
        "path_so_far": tone.get("path"),
        "allowed_tones": allowed_tones(tone["tone"]),
        "why_this_tone": tone.get("why"),
    }
    return json.dumps({key: value for key, value in line.items() if value}, ensure_ascii=False)


def _briefs_by_pair(reply: Any) -> dict[tuple[str, str], dict]:
    items = reply.get("briefs") if isinstance(reply, dict) else None
    result: dict[tuple[str, str], dict] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("step_id") and item.get("audience"):
            result.setdefault((item["step_id"], item["audience"]), item)
    return result


def _clean(brief: Any) -> dict[str, str] | None:
    if not isinstance(brief, dict):
        return None
    keys = ("approach", "tone", "length", "subject_style", "lead_option", "call_to_action", "why")
    return {key: " ".join(str(brief.get(key) or "").split())[:MAX_TEXT_CHARS] for key in keys}
