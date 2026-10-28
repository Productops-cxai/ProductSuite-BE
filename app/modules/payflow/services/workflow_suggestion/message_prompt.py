"""Build the LLM prompt that writes a workflow's email / SMS text.

The tone of each message is decided by code (message_tone.py) — the AI only writes in it.
"""

from __future__ import annotations

import json
from typing import Any

from app.modules.payflow.services.strategy_service import MESSAGE_TEMPLATES
from app.modules.payflow.services.workflow_suggestion.message_validator import (
    EMAIL_WORDS,
    MAX_SMS_CHARS,
    MAX_SUBJECT_CHARS,
)
from app.modules.payflow.services.workflow_suggestion.prompt import compact_step
from app.shared.enums import PayflowCollectionStatus

# Existing templates, shown as style examples: (template id, tone it is written in).
STYLE_EXAMPLES = [("em-reminder", "Friendly"), ("sms-reminder", "Clear")]

# What the segment's situation means for the customer — so the text fits where they are.
SITUATION_NOTES = {
    PayflowCollectionStatus.ACTIVE.value: "These customers have no payment arrangement yet.",
    PayflowCollectionStatus.PROMISE_TO_PAY.value: (
        "These customers promised to pay ({{promise_amount}} by {{promise_date}}): remind them of their own promise."
    ),
    PayflowCollectionStatus.PAYMENT_PLAN.value: (
        "These customers are already on a payment plan: help them keep up their instalments - "
        "do not offer them a new plan."
    ),
}


def build_message_prompt(
    client: dict[str, Any],
    workflow: dict[str, Any],
    segment: dict[str, Any],
    briefs: list[dict[str, Any]],
    others: list[dict[str, Any]],
    feedback: list[dict[str, Any]] | None = None,
    remarks: str | None = None,
) -> list[dict[str, str]]:
    """`briefs`: the messages to write (step_id, title, channel, purpose, timing, tone, why, current text).
    `others`: the workflow's other messages, already written (context only).
    `feedback` (on a retry): [{"step_id", "message", "errors"}] from message_validator.
    `remarks` (on a rewrite): what the reviewer asked for."""
    parts = [_client_lines(client), _segment_lines(segment), _workflow_lines(workflow)]
    parts.append("Messages to write (one per step_id):\n" + "\n".join(_brief_line(b) for b in briefs))
    if others:
        parts.append(
            "Other messages in this workflow (already written - do not repeat their wording):\n"
            + "\n".join(json.dumps(other, ensure_ascii=False) for other in others)
        )
    if remarks:
        parts.append(f"Reviewer remarks (apply them to the messages you write): {remarks}")
    if feedback:
        parts.append(_feedback_lines(feedback))
    return [
        {"role": "system", "content": build_message_system_prompt()},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def build_message_system_prompt() -> str:
    examples = "\n".join(
        json.dumps({"tone": tone, "channel": t["channel"], "subject": t.get("subject"), "body": t["body"]},
                   ensure_ascii=False)
        for template_id, tone in STYLE_EXAMPLES
        for t in MESSAGE_TEMPLATES
        if t["id"] == template_id
    )
    low, high = EMAIL_WORDS
    return f"""You write the emails and SMS messages of a debt-collection workflow for PayFlow, a collections platform.
Every account in the segment receives the same text; the variables are filled in per account when the message is sent.
Reply with JSON only, matching the provided schema.

## Variables (the only ones allowed, written exactly like this)
- {{{{first_name}}}}: the customer's first name
- {{{{outstanding_balance}}}}: the current balance, with currency
- {{{{account_reference}}}}: the account number
- {{{{payment_link}}}}: the customer's own secure payment page
- {{{{brand_name}}}}: who is sending the message
- {{{{promise_amount}}}}, {{{{promise_date}}}}: what the customer promised to pay and when (Promise-to-Pay messages only)
Never type an amount, a date, a phone number or an email address - use the variables.
Never use placeholders like [Name] or <date>.

## Tones
Each message has a tone that was decided for you (with the reason). Write in exactly that tone:
- Friendly: warm and light. Assume the payment was missed by accident.
- Clear: direct and helpful. The balance is still open; give the options to sort it out.
- Firm: serious and short. The account is overdue and needs action now; one clear ask. Still polite.
- Final notice: firm. Say it is the final reminder, and that the account will then be passed to our team
  for review. Nothing else is threatened.
- Supportive: encouraging. Thank the customer for their arrangement or progress; make the next step easy.
- Neutral: calm and factual. No push for payment (contact details update, or a dispute is open).
Firm is never rude. Never threaten, shame or blame. Never mention courts, lawyers, police, credit bureaus,
fees or deadlines, and never guess at the customer's personal situation. Never hint at consequences either:
no "to avoid further action", no service being cut or interrupted - only the Final notice line above.

## Writing rules
- Plain words and short sentences (easy to read). Talk to the customer as "you".
- One clear ask per message, with {{{{payment_link}}}} (Neutral messages may leave it out).
- Fit each message to its place in the workflow: the first message says who is writing and why; a later
  message may refer back to the earlier one ("We emailed you last week") but must not repeat its wording.
- Only mention what path_so_far shows happened (e.g. never say an email bounced unless the path says so).
- Never say how many days, weeks or months the account is overdue - it differs per account and changes.
- Use the segment: e.g. a high average balance → a payment plan can spread the cost (only if the payment page
  offers plans); partly paid → thank them for what they have paid.
- Offer only what the payment page really offers (listed with the client).
- Email: a subject of at most {MAX_SUBJECT_CHARS} characters, and a body of {low}-{high} words that starts with
  "Hi {{{{first_name}}}}," and ends with {{{{brand_name}}}}.
- SMS: no subject. At most {MAX_SMS_CHARS} characters with the values filled in. Start with "{{{{brand_name}}}}:"
  and end with "Reply STOP to opt out."
- If a message shows its current text, improve that text (and apply the reviewer remarks) instead of starting over.

## Style examples (illustrative only, do not copy)
{examples}"""


def _client_lines(client: dict[str, Any]) -> str:
    name = client["name"]
    if client.get("client_type") == "first_party":
        sender = f"Messages are sent by {name} itself ({{{{brand_name}}}})."
    else:
        sender = (
            f"Messages are sent by a collection agency ({{{{brand_name}}}}) on behalf of {name}: "
            f"the first message must say it is contacting the customer on behalf of {name}."
        )
    options = client.get("payment_options")
    offers = ", ".join(options) if options else "pay online"
    return (
        f"Client: {name}. {sender}\n"
        f"Write in {client.get('language') or 'English'}. Currency: {client.get('currency') or 'CAD'}.\n"
        f"On the payment page ({{{{payment_link}}}}) the customer can: {offers}."
    )


def _segment_lines(segment: dict[str, Any]) -> str:
    stats = json.dumps(segment.get("stats") or {}, ensure_ascii=False)
    situation = (segment.get("segment") or {}).get("situation")
    note = SITUATION_NOTES.get(situation)
    return f"Segment: {segment.get('label') or 'Unknown'}\n" + (f"{note}\n" if note else "") + f"Segment stats: {stats}"


def _workflow_lines(workflow: dict[str, Any]) -> str:
    steps = "\n".join(json.dumps(compact_step(step), ensure_ascii=False) for step in workflow.get("steps") or [])
    return (
        f"Workflow: {workflow.get('name')} - {workflow.get('summary') or ''}\n"
        f"Entry step: {workflow.get('entry_node_id')}. Steps:\n{steps}"
    )


def _brief_line(brief: dict[str, Any]) -> str:
    keys = ("step_id", "title", "channel", "purpose", "timing", "path_so_far", "tone", "why_this_tone", "current_text")
    return json.dumps({key: brief[key] for key in keys if brief.get(key)}, ensure_ascii=False)


def _feedback_lines(feedback: list[dict[str, Any]]) -> str:
    parts = ["Your previous messages for these steps had problems. Fix every problem:"]
    for item in feedback:
        if item.get("message"):
            parts.append(json.dumps({"step_id": item["step_id"], **item["message"]}, ensure_ascii=False))
        parts.append("Problems:\n" + "\n".join(f"- {error}" for error in item["errors"]))
    return "\n".join(parts)
