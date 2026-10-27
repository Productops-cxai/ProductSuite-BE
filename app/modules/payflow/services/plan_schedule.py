"""Payment-plan schedule math (Lovable amount-first Daily / Weekly / Monthly)."""

from __future__ import annotations

import math
from calendar import monthrange
from datetime import date, timedelta
from typing import Any

FREQUENCY_DIVISORS: dict[str, int] = {
    "daily": 60,
    "weekly": 12,
    "monthly": 6,
}

FREQUENCY_MAX_INSTALLMENTS: dict[str, int] = {
    "daily": 180,
    "weekly": 104,
    "monthly": 36,
}

CUSTOM_PLAN_OPTION_ID = "payment_plan_custom"


def _money(value: float) -> float:
    return round(float(value or 0), 2)


def normalize_frequency(value: str | None) -> str:
    freq = (value or "monthly").strip().lower()
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
    month = d.month - 1 + index
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


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
) -> dict[str, Any]:
    """Return Lovable-style quote: N payments, schedule, first charge rules."""
    freq = normalize_frequency(frequency)
    bal = _money(balance)
    if bal <= 0:
        raise ValueError("No outstanding balance")
    suggested = suggested_installment(bal, freq)
    amount = _money(installment_amount) if installment_amount is not None else suggested
    amount = min(max(amount, 1.0), bal)
    max_n = FREQUENCY_MAX_INSTALLMENTS[freq]
    n = max(1, int(math.ceil(bal / amount)))
    if n > max_n:
        raise ValueError(
            f"Too many {freq} payments ({n}). Increase the instalment amount "
            f"(maximum {max_n} payments for {freq})."
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
    return {
        "frequency": freq,
        "suggested_installment": suggested,
        "installment_amount": amount,
        "installments_total": n,
        "total_payable": bal,
        "currency_code": currency_code,
        "start_date": start_date.isoformat(),
        "first_payment_date": schedule[0]["due_date"] if schedule else start_date.isoformat(),
        "final_payment_date": schedule[-1]["due_date"] if schedule else start_date.isoformat(),
        "charge_today": charge_today,
        "first_charge_amount": first_amount if charge_today else 0.0,
        "setup_only": not charge_today,
        "auto_charge_enabled": True,
        "schedule": schedule,
        "option_id": CUSTOM_PLAN_OPTION_ID,
        "label": f"{n} {freq} instalments",
    }
