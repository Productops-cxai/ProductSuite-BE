"""Check AI-written message text before saving — returns plain error messages.

Same idea as validator.py: problems go back to the AI so it can fix them.
"""

from __future__ import annotations

import re
from typing import Any

from app.modules.payflow.services.workflow_suggestion.message_tone import FINAL_NOTICE, NEUTRAL

# The variables the worker fills per account (workflow_execution_service._render).
VARIABLES = {
    "first_name",
    "outstanding_balance",
    "account_reference",
    "payment_link",
    "brand_name",
    "promise_amount",
    "promise_date",
}
# Empty for accounts without a promise ("$0.00", ""), so only allowed where a promise exists.
PROMISE_VARIABLES = {"promise_amount", "promise_date"}

# Lengths are measured as the customer sees them, with realistic values filled in.
SAMPLE_VALUES = {
    "first_name": "Alexandra",
    "outstanding_balance": "$12,450.00",
    "account_reference": "ACC-1002456",
    "payment_link": "https://app.payflow.ai/pay/" + "x" * 43,
    "brand_name": "Maple Energy Collections",
    "promise_amount": "$1,250.00",
    "promise_date": "2026-11-15",
}
MAX_SUBJECT_CHARS = 60
MAX_SMS_CHARS = 320  # two SMS parts
EMAIL_WORDS = (30, 200)

_VARIABLE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_PLACEHOLDER = re.compile(r"\[[^\]\n]{1,30}\]|<[A-Za-z _]{1,30}>")
_TYPED_MONEY = re.compile(r"[$€£]\s*\d|\b\d[\d,.]*\s*(?:CAD|USD|dollars?)\b", re.IGNORECASE)
_TYPED_DATE = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december"
    r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b",  # not "24/7"
    re.IGNORECASE,
)
# Threats, legal pressure and hinted consequences — never allowed, whatever the tone.
_BANNED = re.compile(
    r"\b(?:lawsuit|sue|sued|court|legal action|lawyer|attorney|police|arrest|jail|prison|criminal|warrant|"
    r"garnish\w*|seiz\w*|repossess\w*|bailiff|credit (?:bureau|score|report\w*)|"
    r"further (?:action|steps|measures)|\w*interrupt\w*|disconnect\w*|cut off|shut off|suspen\w*)\b",
    re.IGNORECASE,
)
_FINAL_WORDS = re.compile(r"\bfinal\b|\blast (?:reminder|notice|chance)\b", re.IGNORECASE)
_REVIEW_WORDS = re.compile(r"\breview", re.IGNORECASE)
# "30-59 days past due", "over 90 days": differs per account and changes over time.
_DURATION = re.compile(r"\b\d+\s*(?:[-–‑]\s*\d+\s*)?\+?\s*(?:days?|weeks?|months?)\b", re.IGNORECASE)


def render_sample(text: str) -> str:
    """The text as a customer would see it, with sample values for the variables."""
    return _VARIABLE.sub(lambda match: SAMPLE_VALUES.get(match.group(1), match.group(0)), text or "")


def check_message(message: dict[str, Any], brief: dict[str, Any], other_bodies: list[str]) -> list[str]:
    """Problems with one written message — [] when it can be saved.

    `brief`: step_id, channel, tone, promise_ok (promise variables allowed), english (text is English).
    `other_bodies`: the workflow's other message bodies (no copy-paste between messages).
    """
    where = brief["step_id"]
    subject, body = message.get("subject") or "", message.get("body") or ""
    if not body.strip():
        return [f"{where}: the message has no text"]
    errors: list[str] = []
    text = f"{subject}\n{body}"

    used = set(_VARIABLE.findall(text))
    for name in sorted(used - VARIABLES):
        errors.append(f"{where}: {{{{{name}}}}} is not an allowed variable")
    if not brief.get("promise_ok"):
        for name in sorted(used & PROMISE_VARIABLES):
            errors.append(f"{where}: {{{{{name}}}}} is only allowed in Promise-to-Pay messages")
    leftover = _VARIABLE.sub("", text)
    if "{" in leftover or "}" in leftover or _PLACEHOLDER.search(leftover):
        errors.append(f"{where}: has a broken variable or a placeholder - use only the allowed {{{{variables}}}}")
    if "brand_name" not in used:
        errors.append(f"{where}: must include {{{{brand_name}}}} so the customer knows who is writing")
    if "payment_link" not in used and brief["tone"] != NEUTRAL:
        errors.append(f"{where}: must include {{{{payment_link}}}}")

    if _TYPED_MONEY.search(leftover) or _TYPED_DATE.search(leftover):
        errors.append(f"{where}: types an amount or a date - use {{{{outstanding_balance}}}} / promise variables")
    if _DURATION.search(leftover):
        errors.append(f"{where}: mentions a number of days / weeks - it differs per account, leave it out")
    banned = _BANNED.search(text)
    if banned:
        errors.append(f"{where}: '{banned.group(0)}' sounds like a threat - remove it")
    if brief["tone"] != FINAL_NOTICE and _FINAL_WORDS.search(text):
        errors.append(f"{where}: says 'final' / 'last' but its tone is {brief['tone']}")
    if brief["tone"] == FINAL_NOTICE and not _FINAL_WORDS.search(text):
        errors.append(f"{where}: a Final notice must say it is the final reminder")
    # The one consequence allowed — and it must be stated (checked for English text only).
    if brief["tone"] == FINAL_NOTICE and brief.get("english", True) and not _REVIEW_WORDS.search(text):
        errors.append(f"{where}: a Final notice must say the account will then be passed to our team for review")

    if brief["channel"] == "Email":
        errors += _check_email(where, subject, body)
    else:
        errors += _check_sms(where, body)
    if body.strip() in {other.strip() for other in other_bodies}:
        errors.append(f"{where}: is the same text as another message - each message must be different")
    return errors


def _check_email(where: str, subject: str, body: str) -> list[str]:
    errors = []
    if not subject.strip():
        errors.append(f"{where}: an email needs a subject")
    elif "\n" in subject.strip() or len(render_sample(subject)) > MAX_SUBJECT_CHARS:
        errors.append(f"{where}: subject must be one line of at most {MAX_SUBJECT_CHARS} characters")
    words = len(render_sample(body).split())
    low, high = EMAIL_WORDS
    if not low <= words <= high:
        errors.append(f"{where}: email body has {words} words - write {low} to {high}")
    return errors


def _check_sms(where: str, body: str) -> list[str]:
    errors = []
    length = len(render_sample(body))
    if length > MAX_SMS_CHARS:
        errors.append(f"{where}: SMS is {length} characters with values filled in - at most {MAX_SMS_CHARS}")
    if "STOP" not in body:
        errors.append(f"{where}: SMS must end with 'Reply STOP to opt out.'")
    return errors
