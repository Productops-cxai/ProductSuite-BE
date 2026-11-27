# Adaptive workflows — frontend guide

## What the feature does

A workflow's flowchart stays the plan. On top of it, PayFlow now watches what each customer does (opens, clicks, payment page visits, payments, silence) and adjusts **that customer's** plan:

- **Follow-ups:** a ready, approved message for a situation (e.g. "Payment Not Finished") is sent **in place of** the next planned reminder, at that reminder's time. The customer never gets an extra message.
- Each follow-up type has a **First Time** text and a **Repeat** text, for when the same situation happens again. After both, the AI or Operations take over.
- Other changes: move the next message, switch the channel (Email / SMS), check again later, send the case to Operations.
- **Fixed rules** make most changes, and those are applied automatically. The **AI** only decides unclear cases. For **supervised** clients, an AI decision waits for Operations to **Approve, Modify or Reject** it. The plan keeps running meanwhile.

It appears in four places:

| Where | What is new |
|---|---|
| Workflow editor | **Adaptive** switch; **Adaptive Follow-ups** section (7 types × First Time / Repeat); the follow-up pop-up |
| Case page | **Adaptive Workflow** card: what's next, the decision, Approve / Modify / Reject, Reassess Plan, follow-up usage, history |
| Notification bell | "AI decision awaiting approval", only while it still waits |
| Subject Performance | A new reminder stage **"Follow-up"** (no frontend work: the filter values come from the API) |

**Everything comes from the backend.** Don't hard-code follow-up names, waiting times, statuses or which follow-ups a workflow has. Each section below says which call gives what.

All calls are under `/payflow`, with the usual PayFlow session. Updates are `POST` (no PATCH / PUT), as everywhere in PayFlow.

---

## 0. Quick map

| Screen / element | Call |
|---|---|
| Adaptive switch | `GET /payflow/workflows/{id}` → `adaptive`; save with `POST /payflow/workflows/{id}/update` |
| Follow-up list: names, "Sent when", waiting period, First Time / Repeat | `GET /payflow/adaptive/follow-up-types` |
| Which follow-ups this workflow has, and their texts | The workflow's `steps` (steps with `config.follow_up_for`) |
| "Write follow-ups" button | `POST /payflow/workflows/{id}/content` with `{"follow_ups_only": true}` |
| Follow-up pop-up: Regenerate | `POST /payflow/workflows/{id}/content` with `step_id` (+ `audience`, `remarks`) |
| Case page: the whole Adaptive card | `GET /payflow/accounts/{account_id}/workflow-execution` → `execution.adaptive` |
| Approve / Modify / Reject | `POST /payflow/adaptive-decisions/{id}/approve` · `/modify` · `/reject` |
| Reassess Plan | `POST /payflow/workflow-runs/{run_id}/reassess` |
| Bell | `GET /payflow/notifications`: type `adaptive_decision_awaiting`, unread only |

---

## 1. Workflow editor: the Adaptive Workflow card

### 1.1 The Adaptive switch

- Read it from `adaptive` (true / false) on `GET /payflow/workflows/{id}`. It's on by default.
- Save it with `POST /payflow/workflows/{id}/update`, body `{"adaptive": false}`. Switching alone does not send the workflow back to review.
- Off means the workflow runs exactly as before this feature: no follow-ups, no decisions.

### 1.2 The follow-up list

Build the list from two sources:

**a) The types:** `GET /payflow/adaptive/follow-up-types` (read-only, no AI, cheap: load it once).

```json
{
  "items": [
    {
      "follow_up_for": "opened_not_clicked",
      "label": "Opened, Not Paid",
      "trigger": "Email opened; payment link not clicked within 48 hours of the open (a customer who clicks moves to Payment Not Finished)",
      "waiting_hours": 48,
      "needs_payment_plans": false,
      "levels": [
        {"level": 1, "name": "First time", "step_title": "Follow-up: opened, not paid"},
        {"level": 2, "name": "Repeat", "step_title": "Follow-up: opened, not paid (repeat)"}
      ]
    }
  ]
}
```

| Field | Show it as |
|---|---|
| `label` | The row title, e.g. "Opened, Not Paid" |
| `trigger` | "Sent When · …" |
| `waiting_hours` | "Waiting period · 48 hours" |
| `levels` | One badge per level: "First Time", "Repeat" |
| `needs_payment_plans` | Only offered to clients with payment plans (see below) |

There are 7 types today. The numbers come from the backend settings, so they stay right if a setting changes.

**b) This workflow's follow-ups:** they are ordinary `Communication` steps in the workflow's `steps`, marked by their config:

```json
{
  "id": "fu_opened_not_clicked_repeat",
  "kind": "Communication",
  "title": "Follow-up: opened, not paid (repeat)",
  "config": {
    "purpose": "Payment Reminder",
    "follow_up_for": "opened_not_clicked",
    "follow_up_level": 2,
    "subject": "...", "body": "...", "sms_body": "...",
    "versions": {"age_65_plus": {"...": "..."}},
    "translations": {"FR": {"...": "..."}}
  }
}
```

- A step is a follow-up when `config.follow_up_for` is set.
- `config.follow_up_level`: `2` is the Repeat text. **Missing means 1**, the First Time text.
- Match each type's level to the step with the same `follow_up_for` and level.

**The badge per level:**

| The step | Badge |
|---|---|
| Has `body` or `sms_body` | "First Time · Written" / "Repeat · Written" |
| Exists, no text | **"Not written yet"**, amber, with the note "A follow-up without text is never sent" |
| No step for that type | Don't show the type. For example, "Plan Setup Not Finished" isn't added for clients without payment plans, or for segments already on a plan |

**Don't draw follow-up steps on the flowchart canvas.** They have no links (`next` / `yes` / `no`) on purpose and only appear in this section.

### 1.3 When the workflow has no follow-ups: the "Write follow-ups" button

Workflows built by hand start without follow-ups. AI workflows can also have some "Not written yet". Show a **Write follow-ups** button when the workflow has **no** follow-up steps, or has some without text.

```
POST /payflow/workflows/{id}/content
{"follow_ups_only": true}
```

- **What it does:** adds the follow-ups the workflow is missing, then the AI writes only the follow-ups without text. A person's own texts and the flowchart's messages are never touched.
- **Response:** the whole workflow (same as `GET /payflow/workflows/{id}`). Refresh the list from it.
- **Takes:** 30 s to a few minutes (AI calls). Show a loading state.
- **Only for** Draft, AI Proposed and Under Review workflows, the same as Write messages.

| Situation | Response |
|---|---|
| Nothing to write: all written, or the workflow has no reminder or payment-link message for a follow-up to replace | 400 "No follow-up to write: …" → show it as an info message, e.g. "No follow-ups: nothing for them to replace" |
| Sent together with `step_id` or `audience` | 400 "follow_ups_only writes the follow-ups without text - leave out step_id and audience" |
| The AI couldn't write them | 502 "AI could not write valid messages: …". Nothing changed, the user can try again |

The existing **Write messages** call (`POST /content` with no body) now also adds and writes missing follow-ups, along with every message.

### 1.4 Saving the workflow

Save as today with `POST /payflow/workflows/{id}/update` and the full `steps`. **Send follow-up steps back unchanged**, including `follow_up_for` and `follow_up_level`. If those two are dropped, the step stops being a follow-up.

- Allowed `follow_up_for` values: `email_not_opened`, `opened_not_clicked`, `clicked_not_paid`, `payment_failed`, `payment_partial`, `no_activity`, `plan_setup_not_finished`.
- `follow_up_level`: a whole number from 1 to 9.
- One step per type and level, otherwise the save returns 400.

---

## 2. The follow-up pop-up (View / Edit)

Open it from a row of the list. It's the same step panel as any message (see `MESSAGE_VERSIONS_FE.md`), with these differences:

| Element | Follow-up behaviour |
|---|---|
| **First Time / Repeat tabs** | Each tab is a different step: level 1 and level 2 of that type. Everything below the tabs shows that step |
| Header | "Adaptive Follow-up · Finalized with overall workflow approval", plus `trigger` and `waiting_hours` from §1.2 |
| **Channel** | Read-only: **"Email or SMS, chosen when sending"**. Follow-ups have no fixed channel |
| **Message Purpose** | Read-only "Payment Reminder" |
| Template | **Don't show it.** A follow-up always uses its own text |
| Versions dropdown, texts, translations, AI explanation (`brief.why`) | Exactly as for any message (`MESSAGE_VERSIONS_FE.md`). Follow-ups get the same age-group versions as the workflow's other messages |
| **Save version** | `POST /payflow/workflows/{id}/update`, as for any message. Problems come back in `text_warnings` |
| **Regenerate** | `POST /payflow/workflows/{id}/content` with `{"step_id": "<the tab's step id>", "audience": "main" or e.g. "age_65_plus", "remarks": "..."}` |
| Performance insights | Reminder stage shows **"Follow-up"**: follow-up results are counted apart from the reminders' |
| Personalization Context | `GET /payflow/workflows/{id}/audiences`, as for any message |

There is no approval per follow-up text. They are approved with the workflow.

---

## 3. Case page: the Adaptive Workflow card

### 3.1 Loading it

```
GET /payflow/accounts/{account_id}/workflow-execution
```

The response's `execution.adaptive` holds everything in this section. `execution` is `null` when the case has no workflow run: hide the card. When `execution.adaptive.adaptive` is `false` (switch off), hide the card or show "Adaptive is off for this workflow".

The same object is also available on its own with `GET /payflow/workflow-runs/{run_id}/adaptive` (`run_id` is `execution.id`).

**Reload it** after Approve / Modify / Reject and after Reassess Plan.

### 3.2 The shape

```json
{
  "run_id": 41, "account_id": 88831, "client_id": 3, "strategy_id": 12,
  "adaptive": true,
  "engagement_state": "Opened",
  "preferred_channel": null,
  "last_reassessed_at": "2026-11-25T16:00:00Z",
  "next_scheduled": {"step_id": "c3", "kind": "Condition", "title": "Still unpaid?", "at": "2026-11-28T14:00:00Z"},
  "next_message": {"step_id": "s4", "title": "Reminder 4", "not_before": "2026-11-28T14:00:00Z"},
  "swaps": {"s4": "fu_payment_failed_repeat"},
  "lined_up": [
    {"replace_step_id": "s4", "replace_title": "Reminder 4",
     "follow_up_step_id": "fu_payment_failed_repeat", "follow_up_title": "Follow-up: payment failed (repeat)",
     "follow_up_level": 2}
  ],
  "decisions": [ /* newest first, see 3.4 */ ],
  "events": [ /* newest first, see 3.8 */ ],
  "messages": [ /* newest first, see 3.8 */ ],
  "follow_ups": [ /* the usage table, see 3.7 */ ]
}
```

### 3.3 The top of the card

| Element | From |
|---|---|
| Customer engagement | `engagement_state`: "Not opened", "Opened", "Clicked", "Payment started", "Paid part", "Paid", "No activity", "Unsubscribed" |
| Next scheduled action | **`next_message.title`** + `next_message.not_before` (the message goes then, or right after a short check). **If `lined_up` has an entry for that message** (`replace_step_id == next_message.step_id`), show it instead: **"{follow_up_title}, in place of {replace_title}"**, at the same time. Example: "Payment failed (repeat), in place of Reminder 4 · 28 Nov, 14:00". `next_message` is `null` when no message is planned, or when a check could lead to two different messages: then show `next_scheduled.title` + `at`. Don't show `next_scheduled` otherwise: it is the run's next step, which is often a wait or a check ("Still unpaid?"), not a message |
| Channel | `preferred_channel`: `"SMS"` or `"Email"` means that channel is tried first from now on. `null` means the usual order: email when possible |
| While a decision waits | Under the next action: **"Sent as planned unless the change is approved first."** Never show "On hold": nothing stops while a decision waits |
| Status chip | "Active · 1 decision waiting" when a decision has `status: "awaiting_approval"` |

### 3.4 The decision

Show the **latest decision with `status: "awaiting_approval"`** as the main card. If none is waiting, show the latest decision whose `action` isn't `keep_plan` as "Last change", without buttons (`keep_plan` rows are routine checks: keep them for the history).

A decision item:

```json
{
  "id": 912, "run_id": 41, "account_id": 88831, "strategy_id": 12, "client_id": 3,
  "decided_by": "ai",
  "action": "switch_channel",
  "replace_step_id": null, "follow_up_step_id": null,
  "at": null, "channel": "SMS",
  "reason": "Three emails went unopened; SMS may reach her",
  "confidence": 80,
  "needs_approval": true,
  "status": "awaiting_approval",
  "decided_by_user": null, "decision_note": null, "decided_at": null,
  "executed_at": null, "outcome": null,
  "created_at": "2026-11-25T16:00:00Z",
  "replace_title": null, "follow_up_title": null,
  "preview": null
}
```

`replace_title` / `follow_up_title` are the titles of `replace_step_id` / `follow_up_step_id`, ready to show.

**One decision is one action.** Show only the fields that belong to its action:

| `action` | Title | Show |
|---|---|---|
| `use_follow_up` | "Use follow-up" | **"{follow_up_title}, in place of {replace_title}"** (First Time / Repeat from `preview.level`), when (that message's time; `at` if set), and **Preview proposed communication** from `preview` (§3.5) |
| `drop_follow_up` | "Follow-up cancelled" | **"{follow_up_title} cancelled - {replace_title} goes as planned"** (the `reason` says why, e.g. the customer opened the email) |
| `move_next` | "Next message moved" | New time: `at` |
| `switch_channel` | "Channel switched" | **"{old} → {channel}, from now on"**: for a waiting decision, old is the snapshot's `preferred_channel` (`null` = "Email"). No follow-up fields |
| `recheck_at` | "Check again later" | When: `at` |
| `to_operations` / `pause` | "Sent to Operations" | The case is now in Human Review: open the Human Review card for it |
| `stop` | "Stopped" | "Paid in full / nothing owed" |
| `keep_plan` | "No change" | `reason` only. Usually shown in the history only |

Under it, always show:
- **Source:** from `decided_by` (§5).
- **Confidence:** `confidence` (AI only).
- **Why:** `reason`.
- When it was decided: `created_at`.
- **"Compliance is checked when the message is sent."** There's no compliance result on a decision: compliance rules, send windows and opt-outs are checked at send time.

**The buttons** appear **only** when `status == "awaiting_approval"`. That only happens for AI decisions of supervised clients; rule decisions are applied automatically. Text under them: "AI decision · supervised client · Approve, modify or reject before the planned message is due."

### 3.5 Preview proposed communication

Only on `use_follow_up` decisions. `preview` holds the text **this customer** would get: their age-group version, in their language when it's written in it, picked exactly as the sender picks it.

```json
"preview": {
  "step_id": "fu_payment_failed_repeat",
  "title": "Follow-up: payment failed (repeat)",
  "level": 2,
  "version": "age_35_44",
  "version_label": "Age 35 – 44",
  "language": null,
  "subject": "…", "body": "…", "sms_body": "…"
}
```

- Label: "{version_label} · {language name, or the client's main language when `language` is null}", e.g. "Age 35 – 44 · English".
- Show the email (`subject` + `body`) and the SMS (`sms_body`) in tabs. Values in `{{ }}` are filled per customer when it's sent.
- **Not here:** the date picker, consent / opt-out toggles, "Check before execution" and "Simulate scheduling". Those are step-panel test tools. On a case, consent and opt-out are facts from the account: show them read-only if you show them at all.

### 3.6 Approve / Modify / Reject

| Button | Call | Body |
|---|---|---|
| Approve | `POST /payflow/adaptive-decisions/{id}/approve` | `{"note": "optional"}` (send `{}` if no note) |
| Reject | `POST /payflow/adaptive-decisions/{id}/reject` | `{"note": "optional"}` |
| Modify | `POST /payflow/adaptive-decisions/{id}/modify` | See below |

All three return the updated decision item. Then **reload the card** (§3.1).

**Modify form:** a small form with 6 choices. Send only the fields of the chosen action, plus an optional `note`:

| Choice | `action` | Also send |
|---|---|---|
| Keep the plan | `keep_plan` | — |
| Use a follow-up | `use_follow_up` | `follow_up_step_id`: pick from the usage table rows with `state: "available"` (§3.7). `replace_step_id`: **`next_message.step_id`**. Hide this choice when `next_message` is `null` |
| Move the next message | `move_next` | `at`: ISO date-time in the future |
| Switch channel | `switch_channel` | `channel`: `"Email"` or `"SMS"` |
| Check again later | `recheck_at` | `at`: ISO date-time |
| Send to Operations | `to_operations` | — |

```json
{"action": "move_next", "at": "2026-11-30T10:00:00Z", "note": "Customer asked for a week"}
```

The AI's original suggestion stays on the decision (`outcome.ai_decision`) next to what Operations chose (`outcome.reviewer`). Show it in the history as "AI suggested … · Changed by {decided_by_user}".

**Errors and results:**

| Situation | Response / result |
|---|---|
| The decision no longer waits (already handled, replaced or expired) | **409** "Decision is approved, not awaiting_approval" → reload the card |
| A Modify with an unknown action or bad values | **400** with the reason |
| It came too late (e.g. the message it would replace already went out) | 200, `status: "expired"`, `outcome.expired_reason` (§5) → show "Expired: the planned message already went out" |
| It couldn't be carried out (e.g. SMS for a customer with no mobile number) | 200, `status: "failed"`, `outcome.error` → show "Failed: {error}" |

Permissions: viewing needs `view_workflows`; the three buttons, Reassess Plan and Write follow-ups need `create_edit_workflows` for that client. **This is not Human Review:** don't use the Human Review APIs or screens for it.

### 3.7 Follow-up usage table

`follow_ups` has one row per follow-up text of the run's workflow:

```json
{
  "step_id": "fu_clicked_not_paid", "follow_up_for": "clicked_not_paid", "level": 1,
  "title": "Follow-up: payment not finished",
  "state": "sent",
  "sent_at": "2026-11-19T14:00:00Z",
  "replaced_step_id": "s2", "replaced_title": "Reminder 2",
  "last_failed": false
}
```

Group the rows by `follow_up_for`, with names from `GET /payflow/adaptive/follow-up-types`. Make one column per level: First Time, Repeat.

| `state` | Badge | Under it |
|---|---|---|
| `sent` | **Sent** | `sent_at` · "Replaced {replaced_title}" |
| `lined_up` | **Scheduled** | "Replaces {replaced_title}" (its time is `next_message.not_before` when it replaces the next message) |
| `available` | **Available** | — If `last_failed` is true: "Last send failed — still available" |
| `not_written` | **Not written yet** | Not used until it's written |

- **Exhausted:** when every level of a type is `sent`, show "Exhausted" next to the type. From then on, that situation is handled by the AI or Operations.
- Note under the table: "First-time content is used the first time; repeat content the next time. Once both have been sent, further occurrences are handled by the plan and AI reassessment."

### 3.8 Engagement & Decision History

Merge three lists, all newest first, into one timeline by time:

| List | Time field | Entry |
|---|---|---|
| `events` | `at` | What the customer did or what a timer noticed: the label from §5. `detail.machine_open: true` on `email_opened` means the mail app opened it, not the customer: show "Opened (mail app)" in grey |
| `decisions` | `created_at` | Action title (§3.4) + status badge + `reason` + source |
| `messages` | `sent_at` | Sent-message card (below) |

**The sent-message card**, from a `messages` item:

```json
{
  "communication_id": 5521, "step_id": "fu_clicked_not_paid", "title": "Follow-up: payment not finished",
  "channel": "Email", "status": "Delivered", "sent_at": "2026-11-19T14:00:00Z",
  "version": "age_65_plus", "version_label": "Age 65 and over",
  "follow_up": true, "follow_up_level": 1,
  "replaced_step_id": "s2", "replaced_title": "Reminder 2"
}
```

- A follow-up: **"Follow-up: Payment Not Finished (First Time) · replaced Reminder 2 · Age 65 and over"**.
- A normal message: "{title} · {version_label}".
- `status` Failed / Cancelled / Suppressed: show it in red. It didn't reach the customer and didn't use up the follow-up.

### 3.9 Reassess Plan

```
POST /payflow/workflow-runs/{run_id}/reassess
```

No body. Response: `{"run_id": 41, "job_id": 990, "scheduled_at": "..."}`.

- It **queues** a fresh look by the rules and the AI. The result usually arrives within seconds.
- **Show:** "Reassessing…", then reload the card after a few seconds (poll every 3 s, at most about 30 s).
- **The result:** a new decision in `decisions`. It may be "No change", or, for supervised clients, an AI decision waiting for approval.
- **Errors:** 400 when the case has ended or the workflow isn't adaptive. Hide the button in those cases.
- **Help text:** "Ask the AI to look at the latest activity again. The plan keeps running meanwhile."
- Don't confuse it with the old `POST /accounts/{id}/ai-reassess`, which is a different feature.

---

## 4. Notification bell

`GET /payflow/notifications` (existing). AI decisions waiting for approval come as:

```json
{
  "id": 77, "notification_type": "adaptive_decision_awaiting",
  "title": "Adaptive decision · switch_channel",
  "body": "Three emails went unopened; SMS may reach her",
  "link": "/payflow/cases/88831",
  "client_id": 3, "entity_type": "adaptive_decision", "entity_id": "912",
  "read": false, "read_at": null, "created_at": "..."
}
```

- **Show only unread ones** (`read: false`) under "Awaiting your review", labelled **"AI decision awaiting approval"**.
- **The backend clears them by itself:** once the decision is approved, changed, rejected, expired or replaced, the notification is marked read and leaves the bell.
- **Click:** open the case page at `link` and scroll to the Adaptive Workflow card. `entity_id` is the decision's id.
- It's **not** a Human Review: don't open the Human Review screen for it.
- Only AI decisions of supervised clients create one. Rule-based follow-ups never do.

---

## 5. Labels

### Decision status → badge

| `status` | Badge | Meaning |
|---|---|---|
| `awaiting_approval` | **Waiting approval** | An AI decision for a supervised client. The plan keeps running |
| `executed` | **Applied** | Applied automatically (rules, safety rules, or an AI decision for an autopilot client) |
| `approved` | **Approved** | Operations approved it. `decided_by_user`, `decided_at`, `decision_note` |
| `modified` | **Changed** | Operations changed it. The AI's original is in `outcome.ai_decision` |
| `rejected` | **Rejected** | Operations rejected it |
| `superseded` | **Replaced** | A newer decision replaced it, or the follow-up it lined up was cancelled before it went out |
| `expired` | **Expired** | It no longer applied when it was reviewed or swept. Reason in `outcome.expired_reason` (below) |
| `failed` | **Failed** | It couldn't be carried out. Reason in `outcome.error` |

`outcome.expired_reason`:

| Value | Show |
|---|---|
| `step_already_sent` | "The planned message already went out" |
| `no_pending_step` | "Nothing left to move" |
| `run_terminal` | "The case's workflow has ended" |
| `run_missing` | "The workflow run no longer exists" |

### Who decided (`decided_by`)

| Value | Show |
|---|---|
| `rules` | "Rule" |
| `safety` | "Safety rule" (paid in full, or a channel unreachable) |
| `ai` | "AI" |
| `operations` | "Operations" (a Modify) |

### Event types (`events[].type`)

| Type | Show |
|---|---|
| `email_delivered` / `sms_delivered` | "Email delivered" / "SMS delivered" |
| `email_opened` | "Email opened" ("Opened (mail app)" when `detail.machine_open`) |
| `email_clicked` | "Link clicked" |
| `payment_link_clicked` | "Payment link clicked" |
| `payment_page_opened` | "Payment page opened" |
| `payment_started` | "Payment started" (`detail.option_type`: `pay_in_full`, `partial`, `payment_plan`) |
| `payment_abandoned` | "Left the payment page" |
| `payment_failed` | "Payment failed" |
| `payment_partial` | "Part paid" |
| `payment_completed` | "Paid" |
| `plan_created` | "Payment plan set up" |
| `plan_instalment_missed` | "Plan instalment missed" |
| `email_bounced` / `email_unsubscribed` | "Email bounced" / "Unsubscribed from email" |
| `email_from_failover` | "Email sender changed" (the customer unsubscribed or marked an email as spam; emails continue from another sender address: `detail.active_email`) |
| `sms_failed` / `sms_opted_out` | "SMS failed" / "Opted out of SMS" |
| `email_not_opened` | "Not opened in time" (timer) |
| `opened_not_clicked` | "Opened, not clicked in time" (timer) |
| `clicked_not_paid` | "Payment not finished in time" (timer) |
| `no_activity` | "No activity" (timer) |
| `plan_setup_not_finished` | "Plan setup not finished" (timer) |
| `reassess_requested` | "Reassess Plan pressed" |

Show any unknown type as its text with `_` replaced by spaces, so new types never break the screen.

---

## 6. Rules the screens must follow

1. **Rule-based follow-ups never need approval.** Their text was approved with the workflow. Only AI decisions of supervised clients show Approve / Modify / Reject.
2. **Nothing is ever "on hold"** because of a waiting decision. The planned message goes out as planned unless the change is approved first.
3. **A waiting decision is not Human Review.** Only `to_operations` / `pause` put a case in Human Review.
4. **A follow-up replaces a planned message.** It never adds one. Its time is the planned message's time, or later after a failed payment (2 h) or a part payment (24 h).
5. **Don't hard-code** follow-up names, waiting times, versions or statuses. Use the calls above.

---

## 7. Checklist

1. Adaptive switch: `adaptive` on the workflow, saved with `/update`.
2. Follow-up list: types from `/adaptive/follow-up-types`, steps from the workflow (`config.follow_up_for`, `config.follow_up_level`), Written / Not written yet badges, nothing on the canvas.
3. "Write follow-ups" button: `/content` with `{"follow_ups_only": true}`, with the 400 info message.
4. Save: send follow-up steps back unchanged.
5. Follow-up pop-up: First Time / Repeat tabs (two steps), read-only Channel and Purpose, no Template; versions and Regenerate as in `MESSAGE_VERSIONS_FE.md`.
6. Case card: `execution.adaptive`; next action from `next_message` (and `lined_up` for a follow-up in its place); "Sent as planned unless the change is approved first."
7. Decision: fields per action only; preview only for `use_follow_up`; "Compliance is checked when the message is sent."
8. Approve / Modify (6 choices) / Reject, then reload; handle 409, `expired`, `failed`.
9. Usage table from `follow_ups`: Sent / Scheduled / Available / Not written yet, Exhausted, "last send failed".
10. History: `events` + `decisions` + `messages` merged by time, badges and labels from §5.
11. Reassess Plan: POST, then poll the card.
12. Bell: unread `adaptive_decision_awaiting` only; click opens the case's Adaptive card.
