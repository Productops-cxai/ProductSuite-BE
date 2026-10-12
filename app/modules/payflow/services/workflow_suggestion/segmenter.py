"""Group a portfolio's accounts into segments — pure code, no AI, no DB.

Steps: filter → label → group → merge → describe.

A segment uses the same fields as the UI "Who it applies to" panel, plus two
extra keys: `situation` and `contact_state`. A group that is too small is
widened one field at a time to "All …" (balance → contact state → delinquency),
so segments always nest and every account fits exactly one *most specific*
segment. Delinquency is kept longest — it matters most for collections.
Situations are never merged.
"""

from __future__ import annotations

import math
import string
from datetime import date
from typing import Any

from app.modules.payflow.services.workflow_suggestion import config
from app.shared.enums import PayflowCollectionStatus

ACTIVE = PayflowCollectionStatus.ACTIVE.value
PROMISE_TO_PAY = PayflowCollectionStatus.PROMISE_TO_PAY.value
PAYMENT_PLAN = PayflowCollectionStatus.PAYMENT_PLAN.value

# Situations that get a workflow, in display order.
SITUATIONS = [ACTIVE, PROMISE_TO_PAY, PAYMENT_PLAN]
# Arrangements are followed up as one group, whatever the balance or stage.
ARRANGEMENTS = {PROMISE_TO_PAY, PAYMENT_PLAN}

ANY_CONTACT = "any"
CONTACT_STATES = {
    "never_contacted": "Never contacted",
    "contacted_no_payment": "Contacted, nothing paid",
    "partly_paid": "Partly paid",
    ANY_CONTACT: "Any contact history",
}

ALL_STAGES = config.ALL_LABELS["delinquency"]
ALL_BALANCES = config.ALL_LABELS["balance_band"]

# Group key = (situation, delinquency, contact_state, balance_band).
# "All" value per key position; situation (position 0) is never widened.
_KEY_ALL = (None, ALL_STAGES, ANY_CONTACT, ALL_BALANCES)
_WIDEN_ORDER = (3, 2, 1)  # balance → contact state → delinquency


# --------------------------------------------------------------------------- build


def build_segments(
    accounts: list,
    *,
    min_size: int = config.MIN_SEGMENT_SIZE,
    max_segments: int = config.MAX_SEGMENTS,
) -> dict[str, Any]:
    eligible, excluded = filter_eligible(accounts)
    groups = group_accounts(eligible)

    # Too many segments → raise the minimum size 20% at a time until they fit.
    size = min_size
    merged = merge_small_groups(groups, size)
    while len(merged) > max_segments and size <= len(eligible):
        size = math.ceil(size * 1.2)
        merged = merge_small_groups(groups, size)

    ordered = sorted(merged.items(), key=lambda item: _sort_key(item[0]))
    return {
        "total_accounts": len(accounts),
        "eligible_accounts": len(eligible),
        "excluded": excluded,
        "segments": [
            describe_segment(segment_id, key, members)
            for segment_id, (key, members) in zip(string.ascii_uppercase, ordered)
        ],
    }


def filter_eligible(accounts: list) -> tuple[list, dict[str, int]]:
    eligible: list = []
    excluded = {"closed": 0, "resolved": 0, "human_review": 0, "on_hold": 0, "no_balance": 0}
    for account in accounts:
        reason = _exclusion_reason(account)
        if reason:
            excluded[reason] += 1
        else:
            eligible.append(account)
    return eligible, excluded


def label_account(account) -> tuple[str, str, str, str]:
    """Starting group key: (situation, delinquency, contact_state, balance_band)."""
    situation = situation_of(account)
    if situation in ARRANGEMENTS:
        return situation, ALL_STAGES, ANY_CONTACT, ALL_BALANCES
    stage = _band(config.DELINQUENCY_RANGES, account.days_past_due)
    balance = _band(config.BALANCE_RANGES, account.outstanding_balance)
    # Keep keys nested: later fields can only be specific when the stage is known.
    if stage is None:
        return situation, ALL_STAGES, ANY_CONTACT, ALL_BALANCES
    return situation, stage, contact_state_of(account), balance or ALL_BALANCES


def group_accounts(accounts: list) -> dict[tuple, list]:
    groups: dict[tuple, list] = {}
    for account in accounts:
        groups.setdefault(label_account(account), []).append(account)
    return groups


def merge_small_groups(groups: dict[tuple, list], min_size: int) -> dict[tuple, list]:
    """Widen each group smaller than min_size into its "All …" parent, one field per pass."""
    for index in _WIDEN_ORDER:
        merged: dict[tuple, list] = {}
        for key, members in groups.items():
            if len(members) < min_size and key[index] != _KEY_ALL[index]:
                key = key[:index] + _KEY_ALL[index:]
            merged.setdefault(key, []).extend(members)
        groups = merged
    return groups


def describe_segment(segment_id: str, key: tuple, members: list) -> dict[str, Any]:
    situation, stage, contact, balance = key
    segment = dict(config.ALL_LABELS)
    segment.update(
        {
            "delinquency": stage,
            "balance_band": balance,
            "situation": situation,
            "contact_state": contact,
        }
    )
    return {
        "segment_id": segment_id,
        "label": " · ".join([situation, stage, CONTACT_STATES[contact], balance]),
        "segment": segment,
        "stats": _stats(members),
    }


# --------------------------------------------------------------------------- matching


def match_account_to_segment(account, items: list[dict], today: date | None = None) -> dict | None:
    """Most specific item whose `segment` the account fits, or None.

    `items` can be segments from build_segments or serialized workflows —
    anything with a `segment` dict. Missing keys mean "All".
    """
    today = today or date.today()
    best, best_score = None, -1
    for item in items:
        segment = item.get("segment") or {}
        if not _fits(account, segment, today):
            continue
        score = _specificity(segment)
        if score > best_score:
            best, best_score = item, score
    return best


def situation_of(account) -> str:
    status = account.collection_status
    return status if status in SITUATIONS else ACTIVE


def contact_state_of(account) -> str:
    if (account.recovered_balance or 0) > 0:
        return "partly_paid"
    if account.last_contact_date or account.last_email_sent_date or account.last_sms_sent_date:
        return "contacted_no_payment"
    return "never_contacted"


# --------------------------------------------------------------------------- helpers


def _exclusion_reason(account) -> str | None:
    if (account.account_status or "").upper() == "CLOSED":
        return "closed"
    if account.collection_status == PayflowCollectionStatus.RESOLVED.value:
        return "resolved"
    if account.collection_status == PayflowCollectionStatus.HUMAN_REVIEW.value:
        return "human_review"
    if account.provincial_hold:
        return "on_hold"
    if (account.outstanding_balance or 0) <= 0:
        return "no_balance"
    return None


def _in_range(value, bounds: tuple) -> bool:
    low, high = bounds
    return value is not None and value >= low and (high is None or value < high)


def _band(ranges: dict[str, tuple], value) -> str | None:
    return next((label for label, bounds in ranges.items() if _in_range(value, bounds)), None)


def _fits(account, segment: dict, today: date) -> bool:
    situation = segment.get("situation")
    if situation and situation_of(account) != situation:
        return False
    contact = segment.get("contact_state")
    if contact and contact != ANY_CONTACT and contact_state_of(account) != contact:
        return False
    for field, definitions in config.BAND_DEFINITIONS.items():
        label = segment.get(field)
        if not label or label == config.ALL_LABELS[field]:
            continue
        if label not in definitions or not _fits_band(field, definitions[label], account, today):
            return False
    return True


def _fits_band(field: str, definition, account, today: date) -> bool:
    if field == "postal_region":
        postal = (account.postal_code or "").strip().upper()
        return bool(postal) and postal[0] in definition
    if field == "language":
        return (account.language or "").upper() in definition
    if field == "delinquency":
        value = account.days_past_due
    elif field == "balance_band":
        value = account.outstanding_balance
    elif field == "age_band":
        value = _years_since(account.date_of_birth, today)
    else:  # tenure
        value = _months_since(account.date_listed, today)
    return _in_range(value, definition)


def _specificity(segment: dict) -> int:
    score = int(bool(segment.get("situation")))
    score += int(segment.get("contact_state") not in (None, "", ANY_CONTACT))
    for field, all_label in config.ALL_LABELS.items():
        score += int(segment.get(field) not in (None, "", all_label))
    return score


def _years_since(start: date | None, today: date) -> int | None:
    if not start:
        return None
    return today.year - start.year - ((today.month, today.day) < (start.month, start.day))


def _months_since(start: date | None, today: date) -> int | None:
    if not start:
        return None
    return (today.year - start.year) * 12 + today.month - start.month - (today.day < start.day)


def _label_order(field: str, label: str) -> int:
    labels = config.SEGMENT_FIELDS[field]
    return len(labels) if label == labels[0] else labels.index(label)  # "All …" sorts last


def _sort_key(key: tuple) -> tuple:
    situation, stage, contact, balance = key
    return (
        SITUATIONS.index(situation),
        _label_order("delinquency", stage),
        list(CONTACT_STATES).index(contact),
        _label_order("balance_band", balance),
    )


def _pct(members: list, test) -> int:
    return round(100 * sum(1 for a in members if test(a)) / len(members))


def _stats(members: list) -> dict[str, Any]:
    balances = [float(a.outstanding_balance or 0) for a in members]
    dpds = [a.days_past_due for a in members if a.days_past_due is not None]
    languages: dict[str, int] = {}
    for a in members:
        code = (a.language or "unknown").upper()
        languages[code] = languages.get(code, 0) + 1
    return {
        "accounts": len(members),
        "total_outstanding": round(sum(balances), 2),
        "avg_outstanding": round(sum(balances) / len(members), 2),
        "avg_days_past_due": round(sum(dpds) / len(dpds)) if dpds else None,
        "with_email_pct": _pct(members, lambda a: bool(a.email)),
        "with_mobile_pct": _pct(members, lambda a: bool(a.phone_mobile)),
        "email_consent_pct": _pct(members, lambda a: a.email_consent is True),
        "languages": dict(sorted(languages.items())),
    }
