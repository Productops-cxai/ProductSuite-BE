"""Compliance evaluation engine (pure: no database, no clock).

Given the rules in force, the holiday calendar and what is known about one
proposed communication, decide ALLOW / BLOCK / RESCHEDULE / REVIEW_REQUIRED.

Principles (story FR-06, FR-11):
- Every applicable rule is evaluated, not only the most specific one.
- A prohibition always wins: an "Allow" rule never cancels a restriction.
- Missing jurisdiction or time zone is never read as "unrestricted": when rules
  are in force and the customer's location cannot be determined, the answer is
  REVIEW_REQUIRED.
- Advisory rules are reported as warnings; they do not change the decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

ALL_CHANNELS = "All Channels"
MANDATORY = "Mandatory"
ADVISORY = "Advisory"

BLOCKING_ACTIONS = frozenset({"Block", "Suppress"})
REVIEW_ACTIONS = frozenset({"Require Approval", "Flag for Review"})
RESCHEDULE_ACTION = "Reschedule"
ALLOW_ACTION = "Allow"

TIME_FIELDS = frozenset(
    {"Outside Permitted Hours", "Restricted Date", "Outside Customer Preference"}
)

# How far ahead to look for the next permitted moment, and in what steps.
_RESCHEDULE_STEP = timedelta(minutes=15)
_RESCHEDULE_HORIZON = timedelta(days=14)


@dataclass(frozen=True)
class Jurisdiction:
    country: str = ""
    state: str = ""
    region: str = ""
    city: str = ""
    postal: str = ""
    timezone: str = ""


@dataclass(frozen=True)
class ContactEvent:
    """One counted contact: when, on which channel, what kind, for which account."""

    at: datetime
    channel: str
    kind: str = "attempted"  # attempted | completed | conversation
    account_id: int | None = None
    debtor_ref: str | None = None
    portfolio_id: int | None = None


@dataclass
class EvaluationInput:
    client_id: int | None
    jurisdiction: Jurisdiction
    channel: str
    at: datetime
    account_id: int | None = None
    debtor_ref: str | None = None
    portfolio_id: int | None = None
    activity: str = "Collection"
    consent: bool | None = None
    opted_out: bool = False
    flags: set[str] = field(default_factory=set)
    history: list[ContactEvent] = field(default_factory=list)
    consent_date: datetime | None = None
    last_contact_at: datetime | None = None
    last_conversation_at: datetime | None = None
    notice_sent_at: datetime | None = None
    message_text: str | None = None
    language: str = ""
    balance: float = 0.0
    days_past_due: int = 0
    sender_identified: bool = True
    unsubscribe_instructions: bool = True
    template_approved: bool = True
    reply_enabled: bool = True
    collector_type: str = ""
    debt_type: str = ""
    inconvenient_times: list[dict[str, Any]] = field(default_factory=list)
    # Any other fact by condition-field name, e.g. {"Collector Licensed": "No"}.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationResult:
    decision: str
    matches: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    evaluated: list[str] = field(default_factory=list)
    next_permitted_at: datetime | None = None
    requires_review: bool = False
    explanation: str = ""
    # Filled by ComplianceOverrideService after evaluate(); never set by the pure engine.
    overrides_applied: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "matches": self.matches,
            "warnings": self.warnings,
            "evaluated": self.evaluated,
            "next_permitted_at": self.next_permitted_at.isoformat()
            if self.next_permitted_at
            else None,
            "requires_review": self.requires_review,
            "explanation": self.explanation,
            "rule_ids": [m["rule_id"] for m in self.matches],
            "reason_codes": [m["reason_code"] for m in self.matches],
            "overrides_applied": self.overrides_applied,
        }


# --------------------------------------------------------------------------- matching


def _same(scope_value: Any, target_value: str) -> bool:
    scope = str(scope_value or "").strip().lower()
    return not scope or scope == (target_value or "").strip().lower()


def jurisdiction_matches(scope: dict[str, Any], target: Jurisdiction) -> bool:
    """A blank scope field means "anywhere". Postal: a prefix, or a ``LOW..HIGH`` range."""
    postal_scope = str(scope.get("postal") or "").strip().upper()
    postal_target = (target.postal or "").strip().upper()
    if ".." in postal_scope:
        low, high = (part.strip() for part in postal_scope.split("..", 1))
        postal_ok = bool(low and high and postal_target) and low <= postal_target[: len(high)] <= high
    else:
        postal_ok = not postal_scope or postal_target.startswith(postal_scope)
    return (
        _same(scope.get("country"), target.country)
        and _same(scope.get("state"), target.state)
        and _same(scope.get("region"), target.region)
        and _same(scope.get("city"), target.city)
        and postal_ok
    )


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _zone(name: str) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone name
        return None


def _local(at: datetime, tz: ZoneInfo | None) -> datetime:
    aware = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return aware.astimezone(tz or timezone.utc)


def _in_effect(rule: dict[str, Any], on: date, channel: str) -> bool:
    if rule.get("status") != "Active":
        return False
    start, end = _as_date(rule.get("effective_from")), _as_date(rule.get("effective_until"))
    if start and on < start:
        return False
    if end and on > end:
        return False
    rule_channel = rule.get("channel") or ALL_CHANNELS
    return rule_channel == ALL_CHANNELS or rule_channel.lower() == (channel or "").lower()


def _for_client(rule: dict[str, Any], client_id: int | None) -> bool:
    return rule.get("client_id") in (None, client_id)


def _narrowed_out(rule: dict[str, Any], data: EvaluationInput) -> bool:
    """Collector / debt type on the rule that does not match what is known."""
    for key, actual in (("collector_type", data.collector_type), ("debt_type", data.debt_type)):
        wanted = str(rule.get(key) or "").strip().lower()
        known = (actual or "").strip().lower()
        if wanted and known and wanted != known:
            return True
    return False


def applicable_rules(
    rules: list[dict[str, Any]],
    data: EvaluationInput,
    *,
    disabled_rule_ids: set[int] | frozenset[int] = frozenset(),
) -> list[dict[str, Any]]:
    """Rules in force for this communication, highest priority first."""
    tz = _zone(data.jurisdiction.timezone)
    on = _local(data.at, tz).date()
    found = [
        r
        for r in rules
        if _for_client(r, data.client_id)
        and _in_effect(r, on, data.channel)
        and jurisdiction_matches(r, data.jurisdiction)
        and not _narrowed_out(r, data)
        and (r.get("enforcement") == MANDATORY or r.get("id") not in disabled_rule_ids)
    ]
    return sorted(found, key=lambda r: -int(r.get("priority") or 0))


# --------------------------------------------------------------------------- facts


def _clock(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{2}:\d{2}", text) else None


def _js_day(local: datetime) -> int:
    """0 = Sunday … 6 = Saturday (the numbering the rule settings use)."""
    return (local.weekday() + 1) % 7


def outside_hours(settings: dict[str, Any], local: datetime) -> bool:
    """Outside the permitted days / hours. No window configured = never outside."""
    start, end = _clock(settings.get("start")), _clock(settings.get("end"))
    days = settings.get("days")
    if start is None and end is None and not days:
        return False
    day = _js_day(local)
    if days and day not in [int(d) for d in days]:
        return True
    if day == 0:
        start = _clock(settings.get("sunday_start")) or _clock(settings.get("weekend_start")) or start
        end = _clock(settings.get("sunday_end")) or _clock(settings.get("weekend_end")) or end
    elif day == 6:
        start = (
            _clock(settings.get("saturday_start")) or _clock(settings.get("weekend_start")) or start
        )
        end = _clock(settings.get("saturday_end")) or _clock(settings.get("weekend_end")) or end
    now = local.strftime("%H:%M")
    if start and now < start:
        return True
    if end and now >= end:
        return True
    return False


def restricted_date(holidays: list[dict[str, Any]], where: Jurisdiction, local: datetime) -> bool:
    today = local.date().isoformat()
    for holiday in holidays:
        if not _same(holiday.get("country"), where.country):
            continue
        if not _same(holiday.get("state"), where.state):
            continue
        when = str(holiday.get("date") or "")[:10]
        if holiday.get("recurring"):
            if when[5:] == today[5:]:
                return True
        elif when == today:
            return True
    return False


def _inconvenient(windows: list[dict[str, Any]], local: datetime) -> bool:
    now, day = local.strftime("%H:%M"), _js_day(local)
    for window in windows or []:
        days = window.get("days")
        if days and day not in [int(d) for d in days]:
            continue
        start = _clock(window.get("start")) or "00:00"
        end = _clock(window.get("end")) or "24:00"
        if start <= now < end:
            return True
    return False


def _in_scope(event: ContactEvent, data: EvaluationInput, scope: str) -> bool:
    """FR-08: account / customer / creditor (portfolio) rolling counters."""
    scope = (scope or "account").lower()
    if scope == "creditor":
        if data.portfolio_id is not None:
            return event.portfolio_id == data.portfolio_id
        # Unknown portfolio: fall back to customer so we never ignore same-person contacts.
        scope = "customer"
    if scope == "customer":
        if data.debtor_ref:
            if event.debtor_ref and event.debtor_ref == data.debtor_ref:
                return True
            # Events without debtor_ref still count when they are this account.
            return event.account_id is not None and event.account_id == data.account_id
        return event.account_id is None or event.account_id == data.account_id
    # account (default)
    if event.account_id is not None and data.account_id is not None:
        return event.account_id == data.account_id
    return True


def _count(
    data: EvaluationInput,
    settings: dict[str, Any],
    *,
    days: float,
    at: datetime,
) -> int:
    kinds = settings.get("count_events") or ["attempted"]
    combined = settings.get("combined", True)
    scope = (settings.get("scope") or "account").lower()
    since = at - timedelta(days=days)
    total = 0
    for event in data.history:
        when = event.at if event.at.tzinfo else event.at.replace(tzinfo=timezone.utc)
        if not (since < when <= at):
            continue
        if event.kind not in kinds:
            continue
        if not combined and event.channel.lower() != (data.channel or "").lower():
            continue
        if not _in_scope(event, data, scope):
            continue
        total += 1
    return total


def _days_since(then: datetime | None, at: datetime) -> float | None:
    if then is None:
        return None
    aware = then if then.tzinfo else then.replace(tzinfo=timezone.utc)
    return (at - aware).total_seconds() / 86400.0


def _yes(value: bool) -> str:
    return "Yes" if value else "No"


def _actual(
    condition: dict[str, Any],
    rule: dict[str, Any],
    data: EvaluationInput,
    holidays: list[dict[str, Any]],
    at: datetime,
    local: datetime,
) -> Any:
    name = condition.get("field") or ""
    settings = rule.get("settings") or {}
    if name in data.extra:
        return data.extra[name]
    if name == "Contact Attempts":
        return _count(data, settings, days=float(condition.get("period") or 7), at=at)
    if name == "Days Since Last Contact":
        return _days_since(data.last_contact_at, at)
    if name == "Days Since Conversation":
        return _days_since(data.last_conversation_at, at)
    if name == "Days Since Notice":
        return _days_since(data.notice_sent_at, at)
    known: dict[str, Any] = {
        "Consent": _yes(data.consent is True),
        "Opt-Out": _yes(data.opted_out),
        "Outside Permitted Hours": _yes(outside_hours(settings, local)),
        "Restricted Date": _yes(restricted_date(holidays, data.jurisdiction, local)),
        "Outside Customer Preference": _yes(_inconvenient(data.inconvenient_times, local)),
        "Written Notice Sent": _yes(data.notice_sent_at is not None),
        "Sender Identified": _yes(data.sender_identified),
        "Unsubscribe Instructions": _yes(data.unsubscribe_instructions),
        "Template Approved": _yes(data.template_approved),
        "Reply Handling Enabled": _yes(data.reply_enabled),
        "Channel Permitted": "Yes",
        "Collector Licensed": "Yes",
        "Language": data.language,
        "Communication Type": data.activity,
        "Outstanding Balance": data.balance,
        "Days Past Due": data.days_past_due,
    }
    if name in known:
        return known[name]
    return _yes(name in data.flags)


def compare(actual: Any, operator: str, expected: Any) -> bool:
    if actual is None:
        return False
    op = (operator or "Equals").strip().lower()
    text, want = str(actual).strip().lower(), str(expected if expected is not None else "").strip().lower()
    try:
        if op == "greater than":
            return float(actual) > float(expected)
        if op == "less than":
            return float(actual) < float(expected)
        if op == "between":
            low, high = (float(part) for part in re.split(r"\.\.|,", str(expected))[:2])
            return low <= float(actual) <= high
    except (TypeError, ValueError):
        return False
    if op == "includes":
        return want in text
    if op == "excludes":
        return want not in text
    return text == want


def _reason_code(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (text or "").upper()).strip("_") or "RULE"


def _triggered(
    rule: dict[str, Any],
    data: EvaluationInput,
    holidays: list[dict[str, Any]],
    at: datetime,
    local: datetime,
) -> tuple[bool, str, str]:
    """(triggered, reason code, explanation)."""
    settings = rule.get("settings") or {}
    if settings.get("promotional_only") and data.activity != "Promotional":
        return False, "", ""
    conditions = [c for c in (rule.get("conditions") or []) if isinstance(c, dict)]
    checks = [
        compare(_actual(c, rule, data, holidays, at, local), c.get("operator") or "Equals", c.get("value"))
        for c in conditions
    ]
    hit = bool(checks) and (
        all(checks) if (rule.get("logic") or "AND").upper() == "AND" else any(checks)
    )
    if hit:
        first = next(c for c, ok in zip(conditions, checks) if ok)
        joiner = f" {(rule.get('logic') or 'AND').upper()} "
        explanation = joiner.join(
            f"{c.get('field')} {str(c.get('operator') or 'Equals').lower()} {c.get('value')}"
            + (f" / rolling {c.get('period') or 7} days" if c.get("field") == "Contact Attempts" else "")
            for c in conditions
        )
        return True, _reason_code(first.get("field") or ""), explanation

    category = rule.get("category") or ""
    if category == "Frequency":
        for key, days, label in (("daily", 1, "day"), ("weekly", 7, "7 days"), ("monthly", 30, "30 days")):
            limit = int(settings.get(key) or 0)
            if limit > 0 and _count(data, settings, days=days, at=at) >= limit:
                return True, "FREQUENCY_LIMIT", f"{limit} contacts per {label} reached"
        interval = float(settings.get("interval_hours") or 0)
        since = _days_since(data.last_contact_at, at)
        if interval > 0 and since is not None and since * 24 < interval:
            return True, "MINIMUM_INTERVAL", f"Less than {interval:g} hours since the last contact"
    expiry = int(settings.get("consent_expiry_days") or 0)
    if category == "Consent & Opt-Out" and expiry > 0:
        age = _days_since(data.consent_date, at)
        if age is None or age >= expiry:
            return True, "CONSENT_EXPIRED", f"Consent older than {expiry} days or not recorded"
    required = str(settings.get("required_text") or "").strip()
    if category == "Content" and required:
        if required.lower() not in (data.message_text or "").lower():
            return True, "REQUIRED_TEXT_MISSING", f'Message must include: "{required}"'
    return False, "", ""


def _is_time_based(rule: dict[str, Any]) -> bool:
    return any(
        isinstance(c, dict) and c.get("field") in TIME_FIELDS for c in (rule.get("conditions") or [])
    )


def _match(rule: dict[str, Any], code: str, explanation: str) -> dict[str, Any]:
    policy = rule.get("override_policy") or "Not Permitted"
    return {
        "rule_id": rule.get("id"),
        "rule_code": rule.get("code"),
        "version": rule.get("version"),
        "name": rule.get("name"),
        "action": rule.get("action"),
        "enforcement": rule.get("enforcement"),
        "override_policy": policy,
        "overrideable": policy != "Not Permitted",
        "reason_code": code,
        "explanation": explanation,
        "layer": "Client Compliance" if rule.get("client_id") else "Compliance",
    }


def _context_match(code: str, name: str, explanation: str) -> dict[str, Any]:
    return {
        "rule_id": None,
        "rule_code": "CONTEXT",
        "version": None,
        "name": name,
        "action": "Require Approval",
        "enforcement": MANDATORY,
        "override_policy": "Not Permitted",
        "overrideable": False,
        "reason_code": code,
        "explanation": explanation,
        "layer": "Compliance",
    }


# --------------------------------------------------------------------------- evaluate


def evaluate(
    rules: list[dict[str, Any]],
    holidays: list[dict[str, Any]],
    data: EvaluationInput,
    *,
    disabled_rule_ids: set[int] | frozenset[int] = frozenset(),
) -> EvaluationResult:
    at = data.at if data.at.tzinfo else data.at.replace(tzinfo=timezone.utc)
    disabled = frozenset(disabled_rule_ids)

    # Where the customer is cannot be determined. If any rule could apply to this
    # client's communications, that is not "no rules": someone must look at it.
    if not (data.jurisdiction.country or "").strip():
        in_force = [
            r
            for r in rules
            if _for_client(r, data.client_id) and _in_effect(r, at.date(), data.channel)
        ]
        if not in_force:
            return EvaluationResult(decision="ALLOW", explanation="No compliance rules are in force.")
        return EvaluationResult(
            decision="REVIEW_REQUIRED",
            requires_review=True,
            matches=[
                _context_match(
                    "JURISDICTION_UNKNOWN",
                    "Customer jurisdiction",
                    "The customer's country / state could not be determined, so the applicable "
                    "rules cannot be resolved.",
                )
            ],
            explanation="Customer jurisdiction is unknown.",
        )

    tz = _zone(data.jurisdiction.timezone)
    local = _local(at, tz)
    rules_in_force = applicable_rules(rules, data, disabled_rule_ids=disabled)
    result = EvaluationResult(decision="ALLOW", evaluated=[r.get("code") for r in rules_in_force])

    if tz is None and any(_is_time_based(r) for r in rules_in_force):
        result.decision = "REVIEW_REQUIRED"
        result.requires_review = True
        result.matches.append(
            _context_match(
                "TIMEZONE_UNKNOWN",
                "Customer local time",
                "The customer's time zone could not be determined, so permitted hours "
                "cannot be checked.",
            )
        )
        result.explanation = "Customer time zone is unknown."
        return result

    for rule in rules_in_force:
        hit, code, explanation = _triggered(rule, data, holidays, at, local)
        if not hit or rule.get("action") == ALLOW_ACTION:
            continue
        entry = _match(rule, code, explanation)
        if rule.get("enforcement") == ADVISORY:
            result.warnings.append(entry)
        else:
            result.matches.append(entry)

    actions = {m["action"] for m in result.matches}
    if actions & BLOCKING_ACTIONS:
        result.decision = "BLOCK"
    elif actions & REVIEW_ACTIONS:
        result.decision = "REVIEW_REQUIRED"
        result.requires_review = True
    elif RESCHEDULE_ACTION in actions:
        result.decision = "RESCHEDULE"
        result.next_permitted_at = _next_permitted(rules_in_force, holidays, data, at, tz)
    result.explanation = (
        "; ".join(f"{m['rule_code']}: {m['explanation']}" for m in result.matches)
        or "All applicable checks passed."
    )
    return result


def _next_permitted(
    rules_in_force: list[dict[str, Any]],
    holidays: list[dict[str, Any]],
    data: EvaluationInput,
    at: datetime,
    tz: ZoneInfo | None,
) -> datetime | None:
    """First later moment at which no enforced rule is triggered (15-minute steps).

    Walks real instants and re-checks every restriction together, so the result
    respects hours, days, holidays and customer preferences at once (and daylight
    saving, through the time zone rules).
    """
    enforced = [
        r
        for r in rules_in_force
        if r.get("action") != ALLOW_ACTION and r.get("enforcement") != ADVISORY
    ]
    # Start on a quarter-hour boundary so the answer is a clean time.
    start = at.replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % 15)
    steps = int(_RESCHEDULE_HORIZON / _RESCHEDULE_STEP)
    for step in range(1, steps + 1):
        candidate = start + step * _RESCHEDULE_STEP
        local = _local(candidate, tz)
        on = local.date()
        if all(
            not _in_effect(r, on, data.channel)
            or not _triggered(r, data, holidays, candidate, local)[0]
            for r in enforced
        ):
            return candidate.astimezone(timezone.utc)
    return None
