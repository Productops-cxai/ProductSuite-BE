# Story 3 — Client Partial + Plan Limits (BE API for FE)

Paths under `/payflow`. Session auth (`modify` client / existing client update permission).

---

## `GET /clients/{id}`

`payment_settings` on the client detail (existing object):

```json
{
  "payment_settings": {
    "allow_partial": true,
    "allow_payment_plan": true,
    "min_partial_amount": 25,
    "min_plan_amount": 100,
    "plan_limits": [
      { "id": "pl-1", "min_balance": 0, "max_balance": 500, "max_term_months": 3 },
      { "id": "pl-2", "min_balance": 500, "max_balance": 1000, "max_term_months": 5 },
      { "id": "pl-3", "min_balance": 1000, "max_balance": 2000, "max_term_months": 6 },
      { "id": "pl-4", "min_balance": 2000, "max_balance": null, "max_term_months": 12 }
    ]
  }
}
```

`max_balance: null` = no upper limit (last tier only).

---

## `POST /clients/{id}/update`

**Request** (payment section — send inside body as today):

```json
{
  "payment_settings": {
    "allow_partial": true,
    "allow_payment_plan": true,
    "min_partial_amount": 25,
    "min_plan_amount": 100,
    "plan_limits": [
      { "id": "pl-1", "min_balance": 0, "max_balance": 500, "max_term_months": 3 },
      { "id": "pl-2", "min_balance": 500, "max_balance": 1000, "max_term_months": 5 },
      { "id": "pl-3", "min_balance": 1000, "max_balance": 2000, "max_term_months": 6 },
      { "id": "pl-4", "min_balance": 2000, "max_balance": null, "max_term_months": 12 }
    ]
  }
}
```

**Validation (new):** `plan_limits` must be contiguous — no overlaps, no gaps.  
`prev.max_balance` must equal `next.min_balance`. Only the **last** row may have `max_balance: null`.

**Error (400):** e.g. `"Payment-plan balance ranges overlap around $500.00–$600.00"`

**Response:** existing client detail (with saved `payment_settings`).

---

## Enforced on portal (no extra FE call)

- `GET /public/payment-links/{token}` — options respect `allow_partial` / `allow_payment_plan` / mins.
- `POST .../plan-quote` and `.../initiate` — instalment min from the balance’s `plan_limits` tier.

---

## FE must use

1. Save `plan_limits` as contiguous tiers; show API 400 text on overlap/gap.
2. Toggles + min amounts as above; portal already filters/enforces.
