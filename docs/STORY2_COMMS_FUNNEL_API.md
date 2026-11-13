# Story 2 — Communication-to-Payment Funnel (BE API for FE)

Path under `/payflow`. Session auth.

---

## `GET /dashboard`

**Query** (unchanged): `date_range`, `client_id`, `channel`, `workflow`

**Response — funnel** (unchanged steps):

`Sent` → `Delivered` → `Opened / Read` → `Clicked` → `Payment Initiated` → `Paid`

**Response — `funnel_breakdown`** (additive fields):

```json
{
  "funnel_breakdown": {
    "valid_addresses": 120,
    "invalid_addresses": 5,
    "sent": 115,
    "delivered": 100,
    "placement_available": true,
    "placement": [
      { "label": "Inbox", "volume": 40 },
      { "label": "Spam", "volume": 2 },
      { "label": "Promotions", "volume": 0 }
    ],
    "opens_available": true,
    "promotions_available": false,
    "placement_note": "Inbox uses open/click signals; Spam uses provider spam-report events. Promotions is not reported by the email provider."
  }
}
```

| Field | FE use |
|-------|--------|
| `valid_addresses` / `invalid_addresses` / `sent` | Total Records expand. `valid + invalid` = scoped communication rows (partition). Invalid includes address/format failures plus bounce/dropped/undeliverable fails. |
| `delivered` | Delivered expand base |
| `placement_available` | `true` only when real Inbox/Spam signals exist |
| `placement` | Inbox / Spam / Promotions counts — **empty when unavailable** |
| `opens_available` | `false` for SMS → show Opened/Read as **N/A** |
| `promotions_available` | `false` today (ESP does not report Promotions/tab). FE shows Promotions as **N/A**, not `0`. |
| `placement_note` | Explains signal sources or why placement is unavailable |

### Placement rules

- **Inbox** — email rows with open/click (or opened/clicked status), excluding spam-reported rows.
- **Spam** — provider `spamreport` / spam signals on restriction_reason, provider_error, or events.
- **Promotions** — always volume `0` and `promotions_available: false` until a provider supplies tab placement.
- When `placement_available` is `false`, do **not** invent Inbox = delivered.
- Inbox + Spam are capped so they never exceed Delivered.

---

## FE must use

1. Total Records expand → valid / invalid / sent from `funnel_breakdown`.
2. Delivered expand → `placement` only if `placement_available`; else show unavailable + `placement_note`.
3. Promotions tile → **N/A** when `promotions_available` is false (do not treat `0` as a real count).
4. SMS (`channel=SMS`): `opens_available: false` → Opened/Read = N/A.
5. Funnel panel → **Reset Filters** when any funnel filter differs from defaults.
