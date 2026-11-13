# Story 1 — Single-Page Payment Plan Setup (BE API for FE)

Paths under `/payflow`. Public payment-link endpoints need **no JWT**.

---

## `GET /public/payment-links/{token}`

Payment option for plans (when allowed) now returns:

```json
{
  "id": "payment_plan_custom",
  "type": "payment_plan",
  "label": "Set up a payment plan",
  "frequencies": ["weekly", "bi_weekly", "monthly"],
  "custom_plan": true,
  "min_amount": 41.67,
  "max_amount": 500.0,
  "auto_charge_enabled": false,
  "total_payable": 500.0
}
```

`min_amount` = client plan-limit minimum instalment for the balance.  
`auto_charge_enabled` is always `false` until the customer authorizes recurring on initiate.

---

## `POST /public/payment-links/{token}/plan-quote`

**Request**

```json
{
  "frequency": "monthly",
  "installment_amount": 100,
  "start_date": "2026-11-12"
}
```

`frequency`: `weekly` | `bi_weekly` | `monthly`  
`installment_amount`, `start_date` optional.

**Response**

```json
{
  "frequency": "monthly",
  "suggested_installment": 100.0,
  "min_installment": 41.67,
  "max_term_months": 12,
  "max_installments": 12,
  "installment_amount": 100.0,
  "installments_total": 5,
  "final_installment_amount": 100.0,
  "total_payable": 500.0,
  "currency_code": "USD",
  "start_date": "2026-11-12",
  "first_payment_date": "2026-11-12",
  "final_payment_date": "2027-03-12",
  "charge_today": true,
  "first_charge_amount": 100.0,
  "setup_only": false,
  "auto_charge_enabled": false,
  "schedule": [
    { "sequence": 1, "amount": 100.0, "due_date": "2026-11-12", "frequency": "monthly" }
  ],
  "option_id": "payment_plan_custom",
  "label": "5 monthly instalments"
}
```

---

## `POST /public/payment-links/{token}/initiate`

**Request** (plan)

```json
{
  "option_id": "payment_plan_custom",
  "frequency": "monthly",
  "installment_amount": 100,
  "start_date": "2026-11-12",
  "authorize_recurring": false,
  "idempotency_key": "optional-key"
}
```

| Field | Notes |
|-------|--------|
| `authorize_recurring` | **New, optional.** `true` = auto-charge future instalments. Default / omit = `false` (no auto-charge). |

**Response:** existing attempt shape (`checkout_url`, `status`, …). Unchanged.

After checkout, attempt/plan `auto_charge_enabled` follows `authorize_recurring`.

---

## FE must use

1. Frequencies: only `weekly` / `bi_weekly` / `monthly`.
2. Quote API for live “Your Plan” summary; gate Continue on valid quote.
3. Optional `start_date` (ISO date, not before today) — future dates yield `setup_only: true`.
4. Pass `authorize_recurring` from an explicit customer checkbox (optional consent).
5. Review step restates plan summary + whether automatic charges were authorized.
6. Plan *setup* stays one screen (freq + amount + Your Plan). Options → plan → review/checkout matches Lovable; do not collapse checkout into the setup screen.
