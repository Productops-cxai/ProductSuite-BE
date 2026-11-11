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
    r"\b(?:lawsuit|sue|sued|court|legal (?:action|proceedings?|steps?|measures?|process)|proceedings?|"
    r"lawyer|attorney|police|arrest|jail|prison|criminal|warrant|"
    r"garnish\w*|seiz\w*|repossess\w*|bailiff|credit (?:bureau|score|report\w*)|"
    r"further (?:action|steps|measures)|\w*interrupt\w*|disconnect\w*|cut off|shut off|suspen\w*)\b",
    re.IGNORECASE,
)
# "will be sent to a collection agency" is a threat. Saying who writes is not: an agency writing for a
# client must say so ("we are a collection agency contacting you on behalf of ...").
_AGENCY_THREAT = re.compile(
    r"\b(?:send|sent|pass(?:ed)?|refer(?:red)?|hand(?:ed)?|transfer(?:red)?|turn(?:ed)? over|assign(?:ed)?)\s+"
    r"(?:\w+\s+){0,2}to\s+(?:(?:a|an|our|the)\s+)?(?:(?:outside|external|third[- ]party)\s+)?"
    r"(?:collections?|debt)\s+(?:agenc\w*|collector\w*|compan\w*)",
    re.IGNORECASE,
)
_FINAL_WORDS = re.compile(r"\bfinal\b|\blast (?:reminder|notice|chance)\b", re.IGNORECASE)
_REVIEW_WORDS = re.compile(r"\breview", re.IGNORECASE)
# "30-59 days past due", "within 48 hours", "a 7-day deadline": differ per account and change over time.
_DURATION = re.compile(
    r"\b\d+\s*(?:[-–‑]\s*\d+\s*)?\+?[\s-]*(?:hours?|hrs?|days?|weeks?|months?)\b|\bdeadline", re.IGNORECASE
)
# Contact details the AI typed itself (made up or out of date): only {{payment_link}} may point anywhere.
_TYPED_ADDRESS = re.compile(
    r"https?://|\bwww\.|\b[\w.+-]+@[\w-]+\.[\w.-]+|\b[\w-]+\.(?:com|ca|net|org|io|ai|co|info)\b", re.IGNORECASE
)
_NUMBER_RUN = re.compile(r"\+?\(?\d[\d\s().-]{5,}\d")  # "1-800-555-0199", "(416) 555 0199"
PHONE_DIGITS = 7


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
    if any(sum(ch.isdigit() for ch in run.group(0)) >= PHONE_DIGITS for run in _NUMBER_RUN.finditer(leftover)):
        errors.append(f"{where}: types a phone number or another long number - leave it out")
    if _TYPED_ADDRESS.search(leftover):
        errors.append(f"{where}: types a web or email address - the only link allowed is {{{{payment_link}}}}")
    if _DURATION.search(leftover):
        errors.append(
            f"{where}: mentions a number of hours / days / weeks or a deadline - it differs per account, leave it out"
        )
    banned = _BANNED.search(text) or _AGENCY_THREAT.search(text)
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


# --------------------------------------------------------------------------- other-language versions

# A version is a translation of main text that already passed check_message, so it gets only the checks that
# need no word list: they work for any language. Its length (words only, variables left out) may differ from the
# original's (French ~1.3x, Chinese ~0.45x) but not by more than this.
TRANSLATION_LENGTH_RATIO = (0.2, 2.5)
_NUMBER = re.compile(r"\d+")
# The SMS opt-out phrase, e.g. "Reply STOP to opt out.": a few words before STOP, then to the end of the sentence.
_STOP_SENTENCE = re.compile(r"(?:\b\w+\s+){0,3}STOP\b[^.!?\n{}]*[.!?]?")
SMS_KEY = "sms_body"


def check_translation(
    where: str, version: dict[str, Any], source: dict[str, Any], channel: str
) -> list[str]:
    """Problems with one message's other-language version — [] when it can be saved.

    `source`: the message's main text ({"subject", "body", "sms_body"}, already checked).
    `channel`: "Email", "SMS" (text in "body") or a contact step (email in subject + body, SMS in sms_body).
    """
    errors: list[str] = []
    for key in ("subject", "body", SMS_KEY):
        original = str(source.get(key) or "").strip()
        if not original:
            continue
        text = str(version.get(key) or "").strip()
        part = f"{where} ({key})"
        if not text:
            errors.append(f"{part}: missing - translate it too")
            continue
        errors += _check_translated_text(part, text, original, is_sms=key == SMS_KEY or channel == "SMS")
        if key == "subject" and ("\n" in text or len(render_sample(text)) > MAX_SUBJECT_CHARS):
            errors.append(f"{part}: subject must be one line of at most {MAX_SUBJECT_CHARS} characters")
    return errors


def _variable_list(names: set[str]) -> str:
    return ", ".join(f"{{{{{name}}}}}" for name in sorted(names)) or "none"


def _check_translated_text(where: str, text: str, original: str, *, is_sms: bool) -> list[str]:
    errors: list[str] = []
    used, wanted = set(_VARIABLE.findall(text)), set(_VARIABLE.findall(original))
    if used != wanted:
        errors.append(
            f"{where}: must use exactly the variables of the original ({_variable_list(wanted)}), written the "
            f"same way - it uses {_variable_list(used)}"
        )
    leftover = _VARIABLE.sub("", text)
    if "{" in leftover or "}" in leftover or _PLACEHOLDER.search(leftover):
        errors.append(f"{where}: has a broken variable or a placeholder - keep the {{{{variables}}}} as they are")
    # The original was checked: any number it does not have is an invented amount, date, phone or deadline.
    extra = sorted(set(_NUMBER.findall(leftover)) - set(_NUMBER.findall(_VARIABLE.sub("", original))))
    if extra:
        errors.append(f"{where}: has numbers the original does not have ({', '.join(extra)}) - translate only")
    if _TYPED_ADDRESS.search(leftover):
        errors.append(f"{where}: types a web or email address - the only link allowed is {{{{payment_link}}}}")
    if text.strip() == original.strip():
        errors.append(f"{where}: is not translated")
    low, high = TRANSLATION_LENGTH_RATIO
    ratio = len(leftover.strip()) / max(len(_VARIABLE.sub("", original).strip()), 1)
    if not low <= ratio <= high:
        errors.append(f"{where}: is {ratio:.1f} times as long as the original - translate it, adding or leaving out nothing")
    if is_sms:
        length = len(render_sample(text))
        if length > MAX_SMS_CHARS:
            errors.append(f"{where}: SMS is {length} characters with values filled in - at most {MAX_SMS_CHARS}")
        if "STOP" not in text:
            errors.append(f"{where}: SMS must keep the word STOP (e.g. 'Reply STOP to opt out.' in this language)")
        opt_out = next((m.group(0).strip() for m in _STOP_SENTENCE.finditer(original)), "")
        if opt_out and opt_out in text:
            errors.append(f"{where}: translate the opt-out sentence '{opt_out}' too, keeping the word STOP")
    return errors
