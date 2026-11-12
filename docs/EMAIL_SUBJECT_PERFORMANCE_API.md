# Email Subject Performance — API for the Frontend

All paths start with `/payflow`. Same login as the rest of PayFlow (Bearer token / session).

General points:

- **Rates are fractions**: `0.425` means 42.5%. All rates use Delivered as the denominator.
- **A subject** is the subject as written on the workflow step (`{{placeholders}}` kept, extra spaces removed). `subject_key` identifies it; send it back as it is.
- **Opens are estimates.** Automatic opens by mail apps are left out.
- **Unique clicks** count an email once when it has a portal/`clicks_count` visit, a `last_clicked_url` from a SendGrid click, or payment-link-clicked status. SendGrid click events update engagement/`last_clicked_url`; the PayFlow app portal visit still owns incremental `clicks_count`.
- **Completed** = number of successful payment transactions credited to the emails (not an amount). Refunded payments are not counted.
- Only clients the user is allowed to see are returned. `403` = no access, `400` = bad input, `404` = not found, `502` = the AI is not available.
- Tracking starts from the day this is deployed: older emails are not included.

| # | Endpoint | What it is for |
|---|---|---|
| 1 | `GET /dashboard/email-subjects` | Dashboard section: filters, 4 KPI cards, table |
| 2 | `GET /dashboard/email-subjects/details` | Popup for one subject |
| 3 | `POST /dashboard/email-subjects/insight` | "Generate AI Insights" |
| 4 | `GET /email-subjects/suggestions` | Suggested subjects for an email step in the workflow editor |
| 5 | `POST /workflows/{id}/email-suggestion` | AI draft (subject + body) for one email step, not saved |
| 6 | `GET /workflows/{id}/message-history` | Every text the workflow has sent, with its results |
| 7 | `GET /email-subjects/low-performers` | Email steps that under-perform |
| 8 | `POST /workflows/{id}/email-optimization` | Create AI proposals for one workflow |
| 9 | `GET /email-proposals` | List AI proposals |
| 10 | `POST /email-proposals/{id}/decide` | Accept / edit / reject a proposal |
| 11 | `POST /email-subjects/low-performers/scan` | Run the automatic check now (Operations Admin) |
| 12 | `GET /dashboard/email-subjects/trend` | Trend of the selection: whole, or by client / workflow / reminder stage |
| 13 | `POST /email-subjects/draft` | AI draft (subject + body) for a step of a workflow that is not saved yet |

---

## Shared filters (endpoints 1, 2, 3)

| Param | Values | Note |
|---|---|---|
| `date_range` | `30d` (default), `7d`, `today`, `custom` | |
| `date_from`, `date_to` | `YYYY-MM-DD` | required with `custom` |
| `client_id` | number | omit = all the user's clients |
| `portfolio_ids` | number, repeatable | only applies together with `client_id` |
| `strategy_id` | number | workflow |
| `reminder_stage` | `First reminder`, `Second reminder`, `Final reminder`, `Payment plan`, `Promise to pay` | |
| `purpose` | e.g. `Payment Reminder`, `Firm Reminder` | email purpose |
| `search` | text | matches the subject |

## The metrics object

Used for a table row, a KPI card, a body variation, a suggestion.

```json
{
  "sent": 120, "delivered": 116,
  "opens": 81, "open_rate": 0.6983,
  "clicks": 40, "click_rate": 0.3448,
  "payment_link_clicks": 38,
  "initiated": 25,
  "completed": 19, "emails_with_payment": 17,
  "conversion": 0.1638,
  "manual_sent": 2,
  "low_sample": false
}
```

`emails_with_payment` = how many emails led to at least one payment (one email can lead to several instalments). `manual_sent` = how many of them were sent by hand. `low_sample: true` = fewer delivered emails than the minimum (default 100).

---

## 1. `GET /dashboard/email-subjects`

Dashboard section. Query: shared filters, plus `sort` (`completed` default; any metric name or `subject`) and `order` (`desc` default / `asc`).

```json
{
  "filter_options": {
    "date_ranges": ["30d", "7d", "today", "custom"],
    "clients": [{"id": 1, "name": "CLIENT X"}],
    "portfolios": [{"id": 4, "name": "Portfolio A", "client_id": 1}],
    "workflows": [{"id": 9, "name": "Early stage", "client_id": 1}],
    "reminder_stages": ["First reminder", "Second reminder", "Final reminder", "Payment plan", "Promise to pay"],
    "purposes": ["Payment Reminder", "Firm Reminder", "..."]
  },
  "measurement": {
    "min_deliveries": 100,
    "attribution_window_days": 7,
    "attribution_rule": "last_payment_link_click",
    "rate_denominator": "delivered",
    "opens_are_estimated": true
  },
  "leaders": {"opened": {}, "clicked": {}, "payments": {}, "conversion": null},
  "rows": [
    {
      "subject_key": "Your payment options are ready",
      "subject": "Your payment options are ready",
      "variations": 2,
      "contexts": ["CLIENT X · Second reminder"],
      "reminder_stages": ["Second reminder"],
      "...": "the metrics object"
    }
  ],
  "total": 6,
  "unattributed_payments": 3
}
```

- `leaders.*` is a row (same shape as `rows[]`) or `null`. `conversion` is `null` when no subject has enough delivered emails.
- `unattributed_payments` = successful payments in the period that could not be credited to any message.

## 2. `GET /dashboard/email-subjects/details`

Popup for one subject. Query: shared filters (`search` is ignored), `subject_key` (required), `period` = `daily` (default) / `weekly`.

```json
{
  "subject_key": "...", "subject": "...",
  "reminder_stages": ["Second reminder"],
  "contexts": ["CLIENT X · Second reminder"],
  "summary": {"...": "a row, as in endpoint 1"},
  "funnel": [
    {"label": "Sent", "value": 120, "pct_of_sent": 1.0},
    {"label": "Delivered", "value": 116, "pct_of_sent": 0.9667},
    {"label": "Opened", "value": 81, "pct_of_sent": 0.675},
    {"label": "Clicked", "value": 40, "pct_of_sent": 0.3333},
    {"label": "Payment Initiated", "value": 25, "pct_of_sent": 0.2083},
    {"label": "Completed Payment", "value": 19, "pct_of_sent": 0.1583}
  ],
  "period": "daily",
  "trend": [{"date": "2026-11-10", "opens": 12, "clicks": 6, "payments": 3}],
  "breakdown": {
    "by_client":   [{"id": 1, "label": "CLIENT X", "delivered": 116, "completed": 19, "conversion": 0.1638}],
    "by_workflow": [{"id": 9, "label": "Early stage", "delivered": 116, "completed": 19, "conversion": 0.1638}],
    "by_stage":    [{"id": "Second reminder", "label": "Second reminder", "delivered": 116, "completed": 19, "conversion": 0.1638}]
  },
  "variations": [
    {
      "id": 31, "label": "Body version 1",
      "subject": "...", "body": "Hi {{first_name}}, ...",
      "origin": "AI Generated", "language": "EN", "reminder_stage": "Second reminder",
      "first_used_at": "...", "last_used_at": "...",
      "...": "the metrics object"
    }
  ],
  "similar": [{"...": "rows of other subjects at the same reminder stage"}],
  "measurement": {"...": "as in endpoint 1"}
}
```

- Weekly `trend` dates are the Monday of each week.
- `variations[].body` is the text as written (with `{{placeholders}}`), never a customer's copy. It is `null` for emails sent by hand (`label: "Manual email"`).
- `origin`: `AI Generated`, `AI Generated, Human Edited`, `Human Created` or `System Template`.

## 3. `POST /dashboard/email-subjects/insight`

AI explanation from the tracked numbers. Body: the shared filters as JSON fields, plus:

| Field | Values |
|---|---|
| `scope` | `subject` (a table row or the popup), `kpi` (a KPI card), `section` (everything in the filters) |
| `subject_key` | required for `subject` |
| `metric` | required for `kpi`: `opens`, `clicks`, `completed`, `conversion` |

```json
{
  "subject_key": "...", "subject": "...",
  "insight": "Estimated opens are above the typical 61.0%, but clicks ...",
  "observations": ["...", "..."],
  "insufficient_data": false,
  "evidence": {"metrics": {}, "reminder_stages": [], "contexts": [], "comparable_subjects": {}, "min_deliveries": 100},
  "generated_by": "ai",
  "cached": false,
  "disclaimer": "Open rates are estimates. ..."
}
```

- `insufficient_data: true` = not enough delivered emails (or nothing in the selection); `insight` says so.
- `generated_by`: `ai`, or `rules` (fixed wording, used when data is too thin or the AI is not available). Always `200`.
- For `section`, `subject` is `null` and `evidence` has `best` and `weakest`.

## 4. `GET /email-subjects/suggestions`

Suggested subjects for an email step. Query: `client_id` (required), `strategy_id` (omit for a workflow not saved yet), `reminder_stage`, `purpose`.

```json
{
  "client_id": 1, "strategy_id": 9,
  "reminder_stage": "Second reminder", "purpose": "Payment Reminder",
  "scope": "Same client, workflow, stage and purpose",
  "rows": [
    {
      "subject": "Your payment options are ready",
      "explanation": "Reuses a historically successful subject in this reminder context.",
      "measured": true,
      "source_subject": "Your payment options are ready",
      "metrics": {"...": "the metrics object"},
      "low_sample": false,
      "body": "Hi {{first_name}}, ...",
      "body_origin": "AI Generated"
    }
  ],
  "ai_generated": true,
  "disclaimer": "..."
}
```

- Up to 3 rows. `measured: false` = a new AI variation of `source_subject` that has not been sent yet; its `metrics` are the source subject's and `body` is `null`.
- `rows: []` when the client has no history for this context (`scope` says so).
- Only this client's own history is used.

## 5. `POST /workflows/{strategy_id}/email-suggestion`

AI draft of a subject and body for one email step. **Nothing is saved.** Body: `{"step_id": "s2", "remarks": "optional instruction"}`.

```json
{
  "strategy_id": 9, "step_id": "s2",
  "existing": {"subject": "...", "body": "..."},
  "subject": "...", "body": "...",
  "origin": "AI Generated",
  "generated_by": "ai",
  "explanation": "Written by the AI in this step's tone, building on ...",
  "evidence": {"reminder_stage": "...", "purpose": "...", "scope": "...", "examples": [{"subject": "...", "metrics": {}}]},
  "saved": false,
  "disclaimer": "..."
}
```

- `generated_by: "history"` = the AI was not available, so the client's best measured email is returned instead.
- `502` when the AI is not available and there is no measured email to offer.
- Needs the Create / Edit Workflows permission.

## 6. `GET /workflows/{strategy_id}/message-history`

Every distinct email text the workflow has sent, newest first. Optional query: `step_id`.

```json
{
  "strategy_id": 9, "strategy_name": "Early stage",
  "items": [
    {
      "id": 31, "step_id": "s2", "strategy_version": 3,
      "reminder_stage": "Second reminder", "purpose": "Payment Reminder", "language": "EN",
      "origin": "AI Generated",
      "subject": "...", "body": "...",
      "first_used_at": "...", "last_used_at": "...",
      "is_current_subject": true,
      "...": "the metrics object"
    }
  ],
  "total": 2
}
```

An old version keeps its results after the workflow's text is changed.

## 7. `GET /email-subjects/low-performers`

Email steps of Active workflows compared with similar emails (same client, reminder stage and purpose). Read-only. Query: `client_id` (optional), `include_all` (`false` default = only the under-performing ones).

```json
{
  "items": [
    {
      "client_id": 1, "client_name": "CLIENT X",
      "strategy_id": 9, "strategy_name": "Early stage",
      "step_id": "s2", "step_title": "Second email",
      "subject": "...", "reminder_stage": "Second reminder", "purpose": "Payment Reminder",
      "recommendation": "Replace",
      "reason": "Conversion 2.1% is less than half of the typical comparable template: ...",
      "metrics": {"...": "the metrics object, or null when nothing was sent"},
      "comparable_templates": {"subjects": 4, "open_rate": 0.61, "click_rate": 0.3, "conversion": 0.14},
      "pending_proposal_id": 12
    }
  ],
  "total": 1,
  "min_deliveries": 100
}
```

`recommendation`: `Retain`, `Revise`, `Test alternative subjects`, `Replace`, `Insufficient data`.

## 8. `POST /workflows/{strategy_id}/email-optimization`

Creates one AI proposal per email step of the workflow. The workflow is **not** changed. No body. Needs Create / Edit Workflows.

```json
{"strategy_id": 9, "strategy_name": "Early stage", "proposals": [{"...": "a proposal, see endpoint 9"}], "disclaimer": "..."}
```

## 9. `GET /email-proposals`

Query (all optional): `strategy_id`, `client_id`, `status` (`Pending`, `Accepted`, `Edited`, `Rejected`, `Superseded`).

```json
{
  "items": [
    {
      "id": 12, "client_id": 1, "strategy_id": 9, "strategy_name": "Early stage",
      "step_id": "s2", "step_title": "Second email",
      "recommendation": "Replace",
      "status": "Pending",
      "existing": {"subject": "...", "body": "...", "origin": "Human Created"},
      "proposed": {"subject": "...", "body": "...", "origin": "AI Generated"},
      "alternative_subjects": [{"subject": "...", "metrics": {}}],
      "explanation": "...",
      "evidence": {"reminder_stage": "...", "purpose": "...", "step": {}, "comparable_templates": {}, "scope": "...", "min_deliveries": 100, "existing_origin": "..."},
      "final": null,
      "decision_note": null,
      "created_by": "PayFlow AI (automatic check)", "created_at": "...",
      "decided_by": null, "decided_at": null
    }
  ],
  "total": 1
}
```

- `proposed` is `null` for `Retain` and `Insufficient data` (nothing to accept).
- `proposed.origin` is `AI Generated`, or `Measured history` when the AI was not available and the client's best measured subject is proposed instead.
- `created_by` is a user's name, or `PayFlow AI (automatic check)` for proposals the system created on its own.
- `final` = the text that was put on the workflow (after Accepted / Edited).

## 10. `POST /email-proposals/{proposal_id}/decide`

Body:

```json
{"action": "accept", "subject": null, "body": null, "note": "optional"}
```

| `action` | Result |
|---|---|
| `accept` | The proposed text is put on the workflow step. Status `Accepted`. |
| `edit` | The user's `subject` / `body` is put on the step (a field left out keeps the proposed one). Status `Edited`. |
| `reject` | Nothing changes on the workflow. Status `Rejected`. |

Returns the proposal (same shape as endpoint 9).

- After `accept` / `edit` an Active or Approved workflow gets a new version and goes to **Under Review**: it must be approved and activated again, as with any workflow edit.
- `400` when the text does not pass the message checks (the message lists the problems), when the proposal was already decided, or when the step's text changed after the proposal was made.
- Needs Create / Edit Workflows.

## 11. `POST /email-subjects/low-performers/scan`

Runs now the check the system runs on its own once a day: under-performing email steps of Active workflows get a proposal, and the client's supervisors get a notification (`notification_type: "email_proposals_ready"`, link to the workflow). Operations Admin only. No body.

```json
{"workflows": 1, "proposals": 2}
```

## 12. `GET /dashboard/email-subjects/trend`

Query: shared filters, `group_by` = `overall` (default) / `client` / `workflow` / `stage`, `period` = `daily` (default) / `weekly`.

```json
{
  "group_by": "stage", "period": "weekly",
  "dates": ["2026-10-12", "2026-10-19"],
  "series": [
    {
      "id": "Second reminder", "label": "Second reminder",
      "totals": {"delivered": 570, "opens": 430, "clicks": 160, "completed": 74, "conversion": 0.1298},
      "points": [{"date": "2026-10-12", "delivered": 120, "opens": 90, "clicks": 31, "payments": 15, "conversion": 0.125}]
    }
  ],
  "more_groups": 0
}
```

- `overall` returns one series. Grouped: at most 5 series (most completed payments first); `more_groups` = how many were left out.
- Every series has a point for every date in `dates`.

## 13. `POST /email-subjects/draft`

Same as endpoint 5, for a step that is not saved yet. Body: `{"client_id": 1, "purpose": "Payment Reminder", "reminder_stage": "Second reminder", "remarks": "optional"}`.

Response: the same shape as endpoint 5, with `strategy_id: null`, `step_id: "draft"` and `existing: {subject: null, body: null}`. Needs Create / Edit Workflows. `502` when the AI is not available and there is no measured email to offer.

---

## Other things that changed

- **AI and clients**: the AI is given one client's data at a time. An insight for a selection of several clients (or a subject shared by several clients) comes back with `generated_by: "rules"` and `evidence.ai_context: "not_used_several_clients"`; otherwise `evidence.ai_context` is `"one_client"`.
- **Proposals list** (endpoint 9) also returns `pending` (how many are waiting for a decision).
- **Automatic check**: besides the daily run, opening endpoint 7 or 9 starts it in the background when it is due.

- **Workflow steps** (`GET /payflow/workflows/{id}`): an email / SMS step's `config` may now have `content_origin` (`AI Generated`, `AI Generated, Human Edited`, `Human Created`) and may be given `reminder_stage` (one of the five stages). Without `reminder_stage` the stage is worked out from the step's purpose and position.
- **Workflow `ai_context`**: may have a new line `{"label": "Email performance", "value": "..."}` when the AI wrote the emails using measured results.
- **Payment links** now look like `/pay/<token>.<ref>`. The part after `/pay/` must be sent to the payment APIs unchanged.
- **Permissions**: the dashboard endpoints (1–3) need `view_analytics` for the client; 4, 6, 7, 9 need `view_workflows`; 5, 8, 10 need `create_edit_workflows`.
