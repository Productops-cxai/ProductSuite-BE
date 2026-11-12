# Email Unsubscribe Option — Backend API (for Frontend)

All paths start with `/payflow`. Session cookie auth for staff endpoints; public preference endpoints need **no JWT**.

This doc covers **backend only**. FE should implement the customer preference page, staff list, and case detail reason display against these contracts. Do not rely on any FE branch — build from this API.

---

## Product behaviour (short)

1. Governance rule **Allow Unsubscribe Option** (`code: allow-unsubscribe-option`, action `Include Unsubscribe Option`).
2. While that system rule is **not Active**, collection emails keep the **legacy always-on** unsubscribe footer (unchanged).
3. Once the rule is **Active**, only clients that **selected** it under governance get unsubscribe links on collection emails.
4. When that rule applies to the account’s client, the public preference page should collect **reasons + optional comment** before unsubscribe.
5. Unsubscribes are stored and listed for staff; `email_consent = false` still blocks further email (existing behaviour).

Compliance category rename (already on BE): **Consent & Opt-Out** → **Unsubscribe Option** (legacy DB rows are renamed on sample seed).

---

## Reason catalog (fixed codes)

Use these `code` values when posting reasons. Labels are also returned by the API as `reason_options`.

| code | label |
|------|--------|
| `too_many` | Too many emails / contacting too often |
| `prefer_other_channel` | Prefer a different channel (SMS, phone, or mail) |
| `already_paid` | Already paid or arranged a payment plan |
| `wrong_person` | Not the right person / wrong contact details |
| `dispute` | Dispute this debt / do not believe I owe this |
| `attorney` | Working with an attorney or another agency |
| `not_helpful` | Emails are not helpful or not relevant |
| `prefer_no_email` | Prefer not to receive collection emails |
| `other` | Other |

If `other` is selected on the preference page (when `collect_reasons` is true), **comment is required**.

---

## Public endpoints (customer — no auth)

### 1. `GET /public/email-preferences/{token}`

Resolve the preference page.

**Response** (`PayflowEmailPreferencePublic`):

```json
{
  "account_reference": "ACC-123",
  "brand_name": "Acme Collections",
  "email_masked": "j***e@example.com",
  "subscribed": true,
  "email_consent": true,
  "unsubscribe_option_enabled": true,
  "collect_reasons": true,
  "reason_options": [
    { "code": "too_many", "label": "Too many emails / contacting too often" }
  ],
  "last_unsubscribe": null
}
```

| Field | Meaning for FE |
|-------|----------------|
| `subscribed` | `true` if customer still receives collection email (`email_consent is not false`) |
| `collect_reasons` | If `true`, show reason checklist + comment and require at least one reason before unsubscribe |
| `unsubscribe_option_enabled` | Same idea as rule applied for this account’s client |
| `reason_options` | Checklist options when collecting reasons; empty array when not |
| `last_unsubscribe` | Present when already unsubscribed and an event was recorded |

`last_unsubscribe` shape:

```json
{
  "id": 12,
  "reasons": ["too_many", "other"],
  "reason_labels": ["Too many emails / contacting too often", "Other"],
  "comment": "Please only call me",
  "source": "preference_page",
  "unsubscribed_at": "2026-11-12T17:00:00+00:00"
}
```

`source` values: `preference_page` | `one_click` | `staff`.

---

### 2. `POST /public/email-preferences/{token}/unsubscribe`

Unsubscribe. **Body is optional** so List-Unsubscribe one-click / legacy clients keep working.

**JSON body** (preference page):

```json
{
  "reasons": ["too_many", "other"],
  "comment": "Optional unless Other is selected",
  "source": "preference_page"
}
```

| When | Body | Validation |
|------|------|------------|
| Preference page + `collect_reasons` | JSON with `reasons` (+ comment if `other`) | At least one reason; comment required if `other` |
| Preference page + no collect | JSON optional / empty reasons OK | Sets `email_consent = false`, still records an event |
| One-click / form POST | No JSON (form body) | Treated as `source: one_click`; no reason required |

**Response:** same shape as GET resolve (with `subscribed: false` and `last_unsubscribe` filled).

Suggested FE route: `/email-preferences/:token` (public, outside PayFlow shell).

---

### 3. `POST /public/email-preferences/{token}/resubscribe`

No body. Sets `email_consent = true`. Response same public shape (`subscribed: true`).

---

## Staff endpoints (session auth)

### 4. `POST /accounts/{account_id}/email-consent/update`

Existing endpoint — **additive** optional fields. Still requires send-communications (or ops admin).

```json
{
  "email_consent": false,
  "reasons": ["prefer_no_email"],
  "comment": "Customer asked on call"
}
```

- `email_consent` (required bool) — unchanged
- `reasons` / `comment` — optional; recorded only when flipping **to** unsubscribed

Returns the account object (existing shape).

---

### 5. `GET /accounts/{account_id}/email-unsubscribe`

Latest unsubscribe event for a case (for case detail).

Needs `view_communications` for that client (or ops admin).

```json
{
  "account_id": 15,
  "last_unsubscribe": {
    "id": 12,
    "reasons": ["too_many"],
    "reason_labels": ["Too many emails / contacting too often"],
    "comment": null,
    "source": "preference_page",
    "unsubscribed_at": "2026-11-12T17:00:00+00:00"
  }
}
```

`last_unsubscribe` may be `null`.

---

### 6. `GET /email-unsubscribes`

Staff list: **latest event per account** that currently has `email_consent = false`.

Query (all optional):

| Param | Notes |
|-------|--------|
| `client_id` | Filter by client |
| `search` | Name / account reference / email / client name |
| `date_from` | ISO datetime |
| `date_to` | ISO datetime |
| `limit` | Default 50, max 200 |
| `offset` | Default 0 |

Needs `view_communications` (scoped to visible clients).

```json
{
  "items": [
    {
      "event_id": 12,
      "account_id": 15,
      "client_id": 3,
      "client_name": "Acme",
      "account_reference": "ACC-123",
      "customer_name": "Jane Doe",
      "email_masked": "j***e@example.com",
      "outstanding_balance": 450.0,
      "reasons": ["too_many"],
      "reason_labels": ["Too many emails / contacting too often"],
      "comment": null,
      "source": "preference_page",
      "unsubscribed_at": "2026-11-12T17:00:00+00:00"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0,
  "reason_options": [ { "code": "too_many", "label": "..." } ]
}
```

Suggested FE route: `/payflow/email-unsubscribes` (permission: `view_communications`). Nav label suggestion: **Email Unsubscribes** (near Communications).

---

## What FE should build

| Screen | Behaviour |
|--------|-----------|
| Public `/email-preferences/:token` | Resolve → if subscribed and `collect_reasons`, show checklist + comment → POST unsubscribe with `source: "preference_page"` → if unsubscribed, show resubscribe |
| Staff list `/payflow/email-unsubscribes` | Table from `GET /email-unsubscribes` (client / search / date filters) → link to case |
| Case detail | If `email_consent === false`, call `GET .../email-unsubscribe` and show reason labels + comment |
| Ops “Mark unsubscribed” | Existing consent toggle; optionally pass `reasons` / `comment` |

---

## What BE already does (no FE work)

- Attaches / omits unsubscribe footer + List-Unsubscribe on **collection** send based on the rule (legacy always-on until rule is Active).
- One-click List-Unsubscribe hits the same public unsubscribe URL (form POST).
- Sets `account.email_consent = false` and records `payflow_email_unsubscribe_events`.
- Compliance category label **Unsubscribe Option** (was Consent & Opt-Out).
- Action `Include Unsubscribe Option` is activatable (capability — does not block sends).

---

## Ops setup (so FE reason UI turns on)

1. Activate system rule **Allow Unsubscribe Option**.
2. On the client, select that rule under governance.
3. After that, resolve public preference returns `collect_reasons: true` and emails for that client include the unsubscribe option.

---

## Backward compatibility

- Old `POST .../unsubscribe` with **no JSON body** still works.
- Old public response fields (`account_reference`, `brand_name`, `email_masked`, `subscribed`, `email_consent`) unchanged; new fields are additive.
- `POST .../email-consent/update` still accepts `{ "email_consent": true|false }` only.
- SMS STOP / `sms_opt_out` unchanged.
