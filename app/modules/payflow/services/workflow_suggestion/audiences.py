"""Who a workflow's messages are written for — code, no AI, no DB.

A workflow's accounts are described by totals only (shares and averages, never one person), so the AI
can fit each message to them. The accounts are split by age into audiences: a big enough audience gets
its own version of every message; everyone else gets the main text.

Region, country, balance and languages are not split on - they describe each audience, and the AI uses
them to decide how to write. Days late and balance already shape the workflow's segment, and each
customer's language already has its own translation.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Callable

from app.modules.payflow.services.workflow_suggestion import config
from app.modules.payflow.services.workflow_suggestion.languages import language_code
from app.modules.payflow.services.workflow_suggestion.segmenter import (
    _band,
    _in_range,
    _years_since,
    contact_state_of,
    filter_eligible,
    match_account_to_segment,
)

# Audience key (stored with each version) → (age band, [low, high) years from date_of_birth).
AUDIENCES: dict[str, tuple[str, tuple[int, int | None]]] = {
    "age_18_24": ("18 – 24", (18, 25)),
    "age_25_34": ("25 – 34", (25, 35)),
    "age_35_44": ("35 – 44", (35, 45)),
    "age_45_54": ("45 – 54", (45, 55)),
    "age_55_64": ("55 – 64", (55, 65)),
    "age_65_plus": ("65 and over", (65, None)),
}
# The wider groups of versions written before the six above (until 2026-11): never written now, but a saved
# workflow's versions for them are still sent to their customers (audience_keys).
OLDER_AUDIENCES: dict[str, tuple[str, tuple[int, int | None]]] = {
    "age_18_34": ("18 – 34", (18, 35)),
    "age_35_64": ("35 – 64", (35, 65)),
}
MAIN_KEY = "main"  # the step's own text, for everyone without a version of their own
MIN_AUDIENCE_SIZE = 12  # fewer accounts → no own version (they get the main text)
MAX_VERSIONS = 6  # per message, besides the main text
UNKNOWN = "Unknown"

PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec", "SK": "Saskatchewan", "YT": "Yukon",
}
COUNTRIES = {"CA": "Canada"}  # any other code is shown as the code itself


def audience_label(key: str | None) -> str | None:
    """"age_65_plus" → "Age 65 and over", "main" → "Main text" (the step's own text), None → None."""
    if not key:
        return None
    if key == MAIN_KEY:
        return "Main text"
    band = AUDIENCES.get(key) or OLDER_AUDIENCES.get(key)
    return f"Age {band[0]}" if band else key


def audience_of(account, today: date | None = None) -> str | None:
    """The account's audience key from its date of birth; None without one (or under 18)."""
    age = _years_since(getattr(account, "date_of_birth", None), today or date.today())
    return next((key for key, (_, years) in AUDIENCES.items() if _in_range(age, years)), None)


def audience_keys(account, today: date | None = None) -> list[str]:
    """Every version key the account belongs to, its own group first: e.g. ["age_25_34", "age_18_34"] - the
    older wider group catches versions saved before the six groups. [] without a date of birth (or under 18)."""
    age = _years_since(getattr(account, "date_of_birth", None), today or date.today())
    return [key for groups in (AUDIENCES, OLDER_AUDIENCES) for key, (_, years) in groups.items()
            if _in_range(age, years)]


def find_audiences(members: list, today: date | None = None) -> dict[str, Any]:
    """Who a workflow's messages are for. `members`: the workflow's eligible accounts.

    {"group": profile of all members,
     "audiences": [{"key", "label", "accounts", "profile"}] - biggest first, each with its own version,
     "main": {"accounts", "profile"} - who gets the main text, and their totals (it is written for them)}

    An audience with MIN_AUDIENCE_SIZE+ accounts gets its own version (at most MAX_VERSIONS); everyone
    else gets the main text, which is also the fallback for anyone without a version. When fewer than
    MIN_AUDIENCE_SIZE accounts would be left for the main text, the biggest audience gets no version of
    its own: the main text is written for it (two texts for nearly the same people would only cost calls).
    """
    today = today or date.today()
    by_key: dict[str, list] = {}
    for account in members:
        key = audience_of(account, today)
        if key:
            by_key.setdefault(key, []).append(account)
    order = list(AUDIENCES)
    ranked = sorted(by_key.items(), key=lambda item: (-len(item[1]), order.index(item[0])))
    big = [(key, accounts) for key, accounts in ranked if len(accounts) >= MIN_AUDIENCE_SIZE]
    if big and len(members) - sum(len(accounts) for _, accounts in big) < MIN_AUDIENCE_SIZE:
        big = big[1:]
    chosen = big[:MAX_VERSIONS]

    in_a_version = {id(account) for _, accounts in chosen for account in accounts}
    rest = [account for account in members if id(account) not in in_a_version]
    return {
        "group": audience_profile(members, today),
        "audiences": [
            {
                "key": key,
                "label": audience_label(key),
                "accounts": len(accounts),
                "profile": audience_profile(accounts, today),
            }
            for key, accounts in chosen
        ],
        "main": {"accounts": len(rest), "profile": audience_profile(rest, today)},
    }


def audience_profile(accounts: list, today: date | None = None) -> dict[str, Any]:
    """Totals that describe `accounts`: shares in % of the accounts and averages - never one person."""
    if not accounts:
        return {"accounts": 0}
    today = today or date.today()
    balances = [float(account.outstanding_balance or 0) for account in accounts]
    days = [account.days_past_due for account in accounts if account.days_past_due is not None]
    return {
        "accounts": len(accounts),
        "age": _shares(accounts, lambda a: AUDIENCES[key][0] if (key := audience_of(a, today)) else UNKNOWN),
        "regions": _shares(accounts, region_of),
        "countries": _shares(accounts, country_of),
        "languages": _shares(accounts, lambda a: language_code(getattr(a, "language", None)) or UNKNOWN),
        "avg_balance": round(sum(balances) / len(balances), 2),
        "balance_bands": _shares(accounts, lambda a: _band(config.BALANCE_RANGES, a.outstanding_balance) or UNKNOWN),
        "partly_paid_pct": round(100 * sum(1 for a in accounts if (a.recovered_balance or 0) > 0) / len(accounts)),
        "avg_days_past_due": round(sum(days) / len(days)) if days else None,
    }


CONTACT_HISTORY = {
    "never_contacted": "Never contacted",
    "contacted_no_payment": "Contacted, no payment yet",
    "partly_paid": "Paid part",
}


def text_audiences(members: list, steps: list[dict], today: date | None = None) -> list[dict[str, Any]]:
    """Who gets each of a workflow's texts today: the main text and every audience version its messages have.

    `members`: the workflow's eligible accounts. A customer gets their age group's version when the messages have
    one (as the worker picks it), else the main text. [{"key", "label", "accounts", "profile"}] - the main text
    first, then the versions; each profile is audience_profile plus reach_profile. Totals only, never one person.
    """
    today = today or date.today()
    keys = {key for step in steps if step.get("kind") == "Communication"
            for key in ((step.get("config") or {}).get("versions") or {})}
    groups: dict[str, list] = {MAIN_KEY: [], **{key: [] for key in (*AUDIENCES, *OLDER_AUDIENCES) if key in keys}}
    for account in members:
        key = next((key for key in audience_keys(account, today) if key in groups), MAIN_KEY)
        groups[key].append(account)
    return [
        {
            "key": key,
            "label": audience_label(key),
            "accounts": len(accounts),
            "profile": {**audience_profile(accounts, today), **reach_profile(accounts)},
        }
        for key, accounts in groups.items()
    ]


def reach_profile(accounts: list) -> dict[str, Any]:
    """How `accounts` can be reached and what happened before, in % of the accounts ({} without accounts)."""
    if not accounts:
        return {}
    total = len(accounts)
    return {
        "with_email_pct": round(100 * sum(1 for a in accounts if str(a.email or "").strip()) / total),
        "with_mobile_pct": round(100 * sum(1 for a in accounts if str(a.phone_mobile or "").strip()) / total),
        "contact_history": _shares(accounts, lambda a: CONTACT_HISTORY[contact_state_of(a)]),
    }


def members_by_segment(accounts: list, segments: list[dict], today: date | None = None) -> dict[str, list]:
    """Suggest: each segment's own accounts - every eligible account in the most specific segment it fits
    (the same accounts the segment's stats count)."""
    eligible, _ = filter_eligible(accounts)
    members: dict[str, list] = {segment["segment_id"]: [] for segment in segments}
    for account in eligible:
        best = match_account_to_segment(account, segments, today)
        if best:
            members[best["segment_id"]].append(account)
    return members


def region_of(account) -> str:
    """Province / state name (a Canadian code is spelled out), else the region of the postal code."""
    for field in ("province_state", "region"):
        value = str(getattr(account, field, None) or "").strip()
        if value:
            return PROVINCES.get(value.upper(), value)
    postal = str(getattr(account, "postal_code", None) or "").strip().upper()
    for label, letters in config.POSTAL_REGION_PREFIXES.items():
        if postal and postal[0] in letters:
            return label.split(" (")[0]
    return UNKNOWN


def country_of(account) -> str:
    code = str(getattr(account, "country_code", None) or "").strip().upper()
    return COUNTRIES.get(code, code) if code else UNKNOWN


def _shares(accounts: list, label_of: Callable[[Any], str]) -> dict[str, int]:
    """{label: % of the accounts}, most accounts first."""
    counts = Counter(label_of(account) for account in accounts)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {label: round(100 * count / len(accounts)) for label, count in ranked}
