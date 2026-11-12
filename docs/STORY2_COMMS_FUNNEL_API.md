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
    "placement_note": null
  }
}
```

| Field | FE use |
|-------|--------|
| `valid_addresses` / `invalid_addresses` / `sent` | Total Records expand |
| `delivered` | Delivered expand base |
| `placement_available` | `true` only when real Inbox/Spam signals exist |
| `placement` | Inbox / Spam / Promotions counts — **empty when unavailable** |
| `opens_available` | `false` for SMS → show Opened/Read as **N/A** |
| `placement_note` | Message when placement unavailable |

When `placement_available` is `false`, do **not** invent Inbox = delivered.

---

## FE must use

1. Total Records expand → valid / invalid / sent from `funnel_breakdown`.
2. Delivered expand → `placement` only if `placement_available`; else show unavailable + `placement_note`.
3. SMS (`channel=SMS`): `opens_available: false` → Opened/Read = N/A.
