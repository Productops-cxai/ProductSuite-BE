"""Payment-plan schedule math (amount-first Weekly / Bi-Weekly / Monthly).

Daily is kept for backward-compatible quotes only — the customer portal no longer offers it.
"""

from __future__ import annotations

import math
from calendar import monthrange
from datetime import date, timedelta
from typing import Any

FREQUENCY_DIVISORS: dict[str, int] = {
    "daily": 60,
    "weekly": 12,
    "bi_weekly": 12,
    "monthly": 6,
}

# Absolute caps when no client plan-limit tier applies.
FREQUENCY_MAX_INSTALLMENTS: dict[str, int] = {
    "daily": 180,
    "weekly": 104,
    "bi_weekly": 52,
    "monthly": 36,
}

# Best-practice balance → max term (months). Used when client has no plan_limits saved.
DEFAULT_PLAN_LIMITS: list[dict[str, Any]] = [
    {"id": "pl-1", "min_balance": 0, "max_balance": 500, "max_term_months": 3},
    {"id": "pl-2", "min_balance": 500, "max_balance": 1000, "max_term_months": 5},
    {"id": "pl-3", "min_balance": 1000, "max_balance": 2000, "max_term_months": 6},
    {"id": "pl-4", "min_balance": 2000, "max_balance": None, "max_term_months": 12},
]

CUSTOM_PLAN_OPTION_ID = "payment_plan_custom"


def _money(value: float) -> float:
    return round(float(value or 0), 2)


def normalize_frequency(value: str | None) -> str:
    raw = (value or "monthly").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "biweekly": "bi_weekly",
        "bi_weekly": "bi_weekly",
        "fortnightly": "bi_weekly",
    }
    freq = aliases.get(raw, raw)
    if freq not in FREQUENCY_DIVISORS:
        return "monthly"
    return freq


def add_period(d: date, index: int, frequency: str) -> date:
    """Add index intervals of frequency to d (index 0 = same day)."""
    freq = normalize_frequency(frequency)
    if index <= 0:
        return d
    if freq == "daily":
        return d + timedelta(days=index)
    if freq == "weekly":
        return d + timedelta(weeks=index)
    if freq == "bi_weekly":
        return d + timedelta(weeks=index * 2)
    month = d.month - 1 + index
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def normalize_plan_limits(raw: Any) -> list[dict[str, Any]]:
    """Sanitize client payment_settings.plan_limits → sorted tier list."""
    if not isinstance(raw, list) or not raw:
        return [dict(row) for row in DEFAULT_PLAN_LIMITS]
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        try:
            min_b = float(item.get("min_balance") if item.get("min_balance") is not None else 0)
            max_raw = item.get("max_balance")
            max_b = None if max_raw is None or max_raw == "" or max_raw == "No limit" else float(max_raw)
            months = int(item.get("max_term_months") or 12)
        except (TypeError, ValueError):
            continue
        months = max(1, min(months, 36))
        if max_b is not None and max_b < min_b:
            continue
        out.append(
            {
                "id": str(item.get("id") or f"pl-{i + 1}"),
                "min_balance": min_b,
                "max_balance": max_b,
                "max_term_months": months,
            }
        )
    out.sort(key=lambda row: float(row.get("min_balance") or 0))
    return out or [dict(row) for row in DEFAULT_PLAN_LIMITS]


def validate_plan_limits_no_overlap(raw: Any) -> list[dict[str, Any]]:
    """Normalize plan_limits and reject overlaps / gaps between balance tiers.

    Contiguous tiers share an edge (prev.max_balance == next.min_balance) so
    ``tier_for_balance`` (balance > min and <= max) has no hole and no overlap.
    """
    tiers = normalize_plan_limits(raw)
    if len(tiers) < 2:
        return tiers
    for i in range(len(tiers) - 1):
        cur = tiers[i]
        nxt = tiers[i + 1]
        cur_max = cur.get("max_balance")
        nxt_min = float(nxt.get("min_balance") or 0)
        if cur_max is None:
            raise ValueError(
                "Only the last payment-plan balance range may have no upper limit"
            )
        cur_max_f = float(cur_max)
        if nxt_min < cur_max_f:
            raise ValueError(
                f"Payment-plan balance ranges overlap around "
                f"${nxt_min:,.2f}–${cur_max_f:,.2f}"
            )
        if nxt_min > cur_max_f:
            raise ValueError(
                f"Payment-plan balance ranges have a gap between "
                f"${cur_max_f:,.2f} and ${nxt_min:,.2f}"
            )
    if any(t.get("max_balance") is None for t in tiers[:-1]):
        raise ValueError(
            "Only the last payment-plan balance range may have no upper limit"
        )
    return tiers


def tier_for_balance(balance: float, plan_limits: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Pick the plan-limit tier for this balance (Lovable: > min and <= max)."""
    tiers = normalize_plan_limits(plan_limits)
    bal = float(balance or 0)
    for tier in tiers:
        min_b = float(tier.get("min_balance") or 0)
        max_b = tier.get("max_balance")
        if bal > min_b and (max_b is None or bal <= float(max_b)):
            return tier
    # Exact zero / edge: first tier, else last.
    if tiers and bal <= float(tiers[0].get("min_balance") or 0):
        return tiers[0]
    return tiers[-1]


def max_payments_for_frequency(frequency: str, max_term_months: int) -> int:
    freq = normalize_frequency(frequency)
    months = max(1, int(max_term_months or 12))
    if freq == "weekly":
        return months * 4
    if freq == "bi_weekly":
        return months * 2
    if freq == "daily":
        return months * 30
    return months


def suggested_installment(balance: float, frequency: str) -> float:
    freq = normalize_frequency(frequency)
    divisor = FREQUENCY_DIVISORS[freq]
    bal = max(0.0, float(balance or 0))
    if bal <= 0:
        return 5.0
    return max(5.0, round(bal / divisor / 5) * 5)


def build_plan_quote(
    *,
    balance: float,
    frequency: str,
    installment_amount: float | None,
    start_date: date,
    currency_code: str = "USD",
    today: date | None = None,
    plan_limits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return amount-first quote: N payments, schedule, first charge rules, tier min."""
    freq = normalize_frequency(frequency)
    bal = _money(balance)
    if bal <= 0:
        raise ValueError("No outstanding balance")

    tier = tier_for_balance(bal, plan_limits)
    max_term = int(tier.get("max_term_months") or 12)
    max_n = min(
        FREQUENCY_MAX_INSTALLMENTS[freq],
        max_payments_for_frequency(freq, max_term),
    )
    min_installment = _money(math.ceil(bal / max_n * 100) / 100) if max_n else bal
    min_installment = max(1.0, min_installment)

    suggested = suggested_installment(bal, freq)
    # Suggested must never be below the tier minimum.
    suggested = max(suggested, min_installment)

    amount = _money(installment_amount) if installment_amount is not None else suggested
    amount = min(max(amount, 1.0), bal)
    if amount + 1e-9 < min_installment:
        raise ValueError(
            f"Instalment must be at least ${_money(min_installment):,.2f} "
            f"for this balance ({max_term} month plan limit)."
        )

    n = max(1, int(math.ceil(bal / amount)))
    if n > max_n:
        raise ValueError(
            f"Too many {freq.replace('_', '-')} payments ({n}). Increase the instalment amount "
            f"(maximum {max_n} payments for this balance)."
        )

    schedule: list[dict[str, Any]] = []
    remaining = bal
    for i in range(1, n + 1):
        if i == n:
            amt = _money(remaining)
        else:
            amt = min(amount, _money(remaining))
        remaining = _money(remaining - amt)
        due = add_period(start_date, i - 1, freq)
        schedule.append(
            {
                "sequence": i,
                "amount": amt,
                "due_date": due.isoformat(),
                "frequency": freq,
            }
        )

    today_d = today or date.today()
    charge_today = start_date <= today_d
    first_amount = schedule[0]["amount"] if schedule else amount
    final_amount = schedule[-1]["amount"] if schedule else amount
    return {
        "frequency": freq,
        "suggested_installment": suggested,
        "min_installment": min_installment,
        "max_term_months": max_term,
        "max_installments": max_n,
        "installment_amount": amount,
        "installments_total": n,
        "final_installment_amount": final_amount,
        "total_payable": bal,
        "currency_code": currency_code,
        "start_date": start_date.isoformat(),
        "first_payment_date": schedule[0]["due_date"] if schedule else start_date.isoformat(),
        "final_payment_date": schedule[-1]["due_date"] if schedule else start_date.isoformat(),
        "charge_today": charge_today,
        "first_charge_amount": first_amount if charge_today else 0.0,
        "setup_only": not charge_today,
        # Recurring charges require explicit customer authorization (authorize_recurring).
        "auto_charge_enabled": False,
        "schedule": schedule,
        "option_id": CUSTOM_PLAN_OPTION_ID,
        "label": f"{n} {freq.replace('_', '-')} instalments",
    }
