# Adaptive workflows — backend work

Ticket: **AI adaptive, event-driven communication workflows** · From the AI team · 23 Nov 2026

This file is the backend part of the ticket. Build it first; the AI team then plugs in the AI part (section 6).

---

## 1. The problem

Today every customer in a workflow follows the same fixed flowchart, for example:

```
Email 1 ─ wait 3 days ─ Email 2 ─ wait 5 days ─ SMS ─ Human Review
```

The plan never changes, whatever the customer does. A customer who opened Email 1 but did not pay gets the same Email 2 as a customer who never opened it. A customer who clicked "Pay" yesterday waits five more days for the next message.

PayFlow already receives most of what customers do (SendGrid opens, clicks and bounces, Twilio delivery, payment-page visits, Stripe payments, unsubscribes), but none of it changes a customer's plan: `on_communication_event` / `on_payment_event` only move a waiting Condition or a held run.

**The ticket wants:** every time a customer does something (or does nothing for a while), check whether their plan still fits and change the next step if it does not.

Replies from customers (inbound email / SMS text) are **out of scope** — a separate ticket.

---

## 2. Our solution

The workflow's flowchart stays the main plan. A new **adaptive layer** on each customer's run can adjust it.

```
customer does something (open, click, payment, silence)
  → you save it as an event                                   (B1, B2)
  → you wake that customer's run: a "reassess_run" job        (B4)
  → you apply the safety rules first (paid in full → stop)    (B4)
  → decide_next_action(context)  ← the AI team's function     (section 5)
  → you check the decision: allowed? approved message? limits?
  → you carry it out, or send it for approval                 (B5, B6)
  → you record it                                             (B5)
```

What the adaptive layer can do to a plan:

```
Plan:      Email 1 ─ 3 days ─ Email 2 ─ 5 days ─ SMS ─ Review
Customer:  opens Email 1, does not click
Adaptive:  Email 1 ─ 3 days ─ [“opened, not clicked” follow-up] ─ 5 days ─ SMS ─ Review
```

- **Swap** the next planned message for an approved follow-up that fits (same place, same timing).
- **Move** the next planned step sooner or later.
- **Switch** the run to email or SMS.
- **Pause**, **send to Operations**, or **stop**.

Customers do not get extra messages: a follow-up takes the place of the next planned one.

---

## 3. Already decided

| Topic | Decision |
|---|---|
| On / off | Every workflow has an **Adaptive** switch, **on by default**. Off = the workflow runs exactly as today (AC16). The database is cleaned before release, so there are no older workflows to handle. |
| Follow-up messages | Stored **inside the workflow** as extra Communication steps that nothing links to, labelled with the situation they are for. They are approved and versioned with the workflow like every other step. |
| Timers | Not opened: **48 h** · opened, not clicked: **48 h** · clicked, not paid: **24 h** · no activity: **7 days**. In settings for now. |
| What runs on its own | **Safety** (paid in full, unreachable): always. **Routine** decisions with approved messages: yes. **Supervised AI** clients: AI decisions that change the plan need approval. **New text**: never — a person approves it, once per workflow (AI team, section 6). |
| Who writes follow-ups | The AI team (Suggest / Regenerate / Write messages). You only store and send them. |

---

## 4. What you build

Build in this order. Each part can be tested on its own.

### B1 — Events

**New table `payflow_engagement_events`**

| Column | Notes |
|---|---|
| `id`, `created_at` | |
| `client_id`, `account_id` | required |
| `run_id`, `strategy_id`, `communication_id` | nullable |
| `event_type` | one of the list below |
| `channel` | `Email` / `SMS` / null |
| `occurred_at` | when it happened (provider time when given) |
| `source` | `sendgrid` · `twilio` · `portal` · `stripe` · `plan` · `unsubscribe` · `timer` · `manual` |
| `detail` | JSON (e.g. `{"machine_open": true}`, the clicked URL, amount) |
| `dedupe_key` | **unique** — providers repeat events; the same event is saved once |
| `processed_at` | nullable — set when a reassessment has read it |

**Event types**

| Group | `event_type` |
|---|---|
| Email | `email_delivered`, `email_opened`, `email_clicked`, `payment_link_clicked`, `email_bounced`, `email_unsubscribed` |
| SMS | `sms_delivered`, `sms_failed`, `sms_opted_out` |
| Payment | `payment_page_opened`, `payment_started`, `payment_completed`, `payment_partial`, `payment_failed`, `payment_abandoned`, `plan_created`, `plan_instalment_missed` |
| Timers (B2) | `email_not_opened`, `opened_not_clicked`, `clicked_not_paid`, `no_activity` |
| Other | `reassess_requested` (the manual button) |

**One helper** that every hook calls, e.g. `services/engagement_service.py`:

```python
record_engagement_event(db, *, account_id, event_type, occurred_at=None, communication_id=None,
                        channel=None, source, detail=None, dedupe_key) -> event | None
```

It saves the event (does nothing if the `dedupe_key` exists), links it to a run, and queues a reassessment (B4).

- **Run link:** the communication's own `run_id` when there is one (messages already store it), else the account's open run.
- **Older messages still count:** an open or click on Email 1 after Email 2 was sent is still recorded and linked (FR-01). Webhooks already find the message by its id, so this works as long as nothing filters "old" messages out.
- **Email opens are a weak signal:** keep SendGrid's `sg_machine_open` in `detail.machine_open` (already read in `apply_provider_event`).

**Where to call it**

| Where it happens today | Event |
|---|---|
| `communication_service.apply_provider_event` (SendGrid + Twilio status) | `delivered` → `email_delivered` / `sms_delivered` · `open` → `email_opened` · click on the payment link → `payment_link_clicked`, other click → `email_clicked` · `bounce`, `dropped` → `email_bounced` · `failed`, `undelivered` → `sms_failed` · `unsubscribe`, `group_unsubscribe`, `spamreport` → `email_unsubscribed` |
| `communication_service.record_portal_visit` | `payment_page_opened` |
| `WorkflowExecutionService.on_payment_event` (every payment path already calls it) | `completed: true` and `case_settled: true` → `payment_completed` · `completed: true` and `case_settled: false` → `payment_partial` · `status: "plan_setup"` → `plan_created` · `status` `plan_missed` / `auto_charge_paused` → `plan_instalment_missed` · `status` failed → `payment_failed` |
| `payment_portal_service` — where the payment page creates a payment attempt (`PayflowPaymentAttemptModel(...)`, around line 859) | `payment_started`. Not the auto-charge attempts in `plan_auto_charge_service`: there the customer did nothing. |
| `payment_portal_service` — where an attempt becomes `Expired`, or `Cancelled` because the customer returned without paying | `payment_abandoned`. Not when PayFlow cancels an attempt because "another payment changed the balance". |
| `email_preference_service.unsubscribe_public`, `set_email_consent(False)` | `email_unsubscribed` |
| `communication_service.mark_sms_opt_out_by_phone` (Twilio STOP) | `sms_opted_out` |

Recording events must never fail the webhook or the payment: wrap it the way the existing hooks are wrapped.

### B2 — Timer events

A step in the worker sweep (`app/worker/loop.py → run_maintenance`) that creates the "nothing happened" events, **for open runs of adaptive workflows only**:

| Event | When | Once per |
|---|---|---|
| `email_not_opened` | an email of the run was sent ≥ 48 h ago and has no open (machine opens do not count). Skip emails that failed or were blocked, and emails without open tracking (`engagement_tracking = "not_available"`: the SMTP / log providers send no opens, so every email would look unopened). | message |
| `opened_not_clicked` | an email with the payment link was opened by a person ≥ 48 h ago (`opens_count > machine_opens_count`) and has no click | message |
| `clicked_not_paid` | the payment link was clicked ≥ 24 h ago and no payment was completed since | message |
| `no_activity` | no event from the customer for 7 days since the run's last message | run, per 7-day window |

The `dedupe_key` makes each one fire once (e.g. `timer:email_not_opened:comm:123`). Opens do not exist for SMS, so `email_not_opened` is for emails only.

New settings in `app/core/config.py`:

```
ADAPTIVE_NOT_OPENED_HOURS = 48
ADAPTIVE_OPENED_NOT_CLICKED_HOURS = 48
ADAPTIVE_CLICKED_NOT_PAID_HOURS = 24
ADAPTIVE_NO_ACTIVITY_DAYS = 7
```

### B3 — Adaptive switch and follow-up steps

**Switch**
- New column `payflow_strategies.adaptive` (bool, default `true`). Every new workflow (create API, AI Suggest, Regenerate) is adaptive unless the request sends `false`.
- In the create / update requests and in every workflow response.
- Can be changed in any status except Rejected, without sending the workflow back to review (turning it on only ever uses approved messages). Write a history row: "Adaptive turned on / off by {name}".

**Follow-up steps**
- A normal `Communication` step with **`config.follow_up_for`** set to one of: `email_not_opened`, `opened_not_clicked`, `clicked_not_paid`, `payment_partial`, `no_activity`.
- No `next` / `yes` / `no`, and no step links to it. It holds the same text fields as other message steps (`subject`, `body`, `sms_body`, `tone`, `versions`, `translations`, `brief`).
- **Add `follow_up_for` to `StrategyNodeConfig`** in `schemas.py` — the schema has `extra="ignore"`, so it is dropped otherwise.
- At most one follow-up step per situation per workflow (refuse a second one on save).
- The engine never reaches them through the flowchart; they are only sent through a swap (B5). They are in the approved version snapshot like any step, so version pinning (`_run_steps`) already covers them.
- `_step_stats`: count them as `follow_ups`, not as emails / SMS.
- `email_tracking.reminder_stage`: leave follow-up steps out when it counts the workflow's messages, or the planned last message is no longer the "Final reminder".

### B4 — The reassess job

**New job type** `reassess_run` (`PayflowWorkflowJobType.REASSESS_RUN`), handled in `app/worker/handlers.py` by a new `AdaptiveWorkflowService.reassess(run_id, job_id=...)`.

**Queued by** `record_engagement_event` when the account has an open run (not Completed / Cancelled / Failed / Superseded) whose workflow is adaptive. Also by the manual button (B6). `email_delivered` and `sms_delivered` are only recorded: every send produces one, so they do not wake the run.

**No pile-ups**
- At most **one pending** `reassess_run` job per run: when one is waiting, do not add another — it reads every new event when it runs.
- Important events (`payment_*`, `plan_*`, `email_unsubscribed`, `sms_opted_out`, `email_bounced`, `sms_failed`) → schedule it now. Other events → schedule it **5 minutes** later, so a burst (open + click + page visit) becomes one reassessment.
- Priority **45** — before a send (`execute_step`, 100) due at the same moment.

**What `reassess` does**
1. **Lock the run row** (`SELECT … FOR UPDATE`) — the same lock `execute_step` takes (B5), so a reassessment and a send never run at the same time for one run. The worker runs 8 threads and locks no run today. Load the run, account and workflow (its pinned steps). A run paused for human review: only mark the events read, change nothing.
2. Read the run's unread events (`processed_at` is null).
3. **Safety rules** (yours, always automatic, no approval):
   - nothing owed / `payment_completed` → **stop** (if the case is not already closed by the existing payment flow);
   - the customer cannot be reached on any channel (email unsubscribed or bounced **and** SMS opted out or no mobile) → **send to Operations**;
   - email unsubscribed or bounced but SMS possible → **switch** the run to SMS (and the other way round).
   When a safety rule decides, skip step 4.
4. Call `decide_next_action(context)` (section 5). Until the AI team delivers it, use the placeholder.
5. **Check the decision** (B5) and carry it out, or send it for approval (B6).
6. Save the decision (B5), set `processed_at` on the events, write a worker log row (`event_type="adaptive_decision"`).

**New run columns** on `payflow_workflow_runs`:

| Column | What |
|---|---|
| `engagement_state` | label for the screen, from the latest event: Not opened · Opened · Clicked · Payment started · Paid part · Paid · Unsubscribed · No activity |
| `preferred_channel` | `Email` / `SMS` / null — set by "switch channel" |
| `swaps` | JSON `{planned_step_id: follow_up_step_id}` — set by "use follow-up" |
| `last_reassessed_at` | |

### B5 — Carry out a decision

**Actions** (fixed list — anything else is refused):

| `action` | What you do |
|---|---|
| `keep_plan` | nothing |
| `use_follow_up` | `run.swaps[replace_step_id] = follow_up_step_id`. When the run reaches `replace_step_id`, send the follow-up step **in that place** — its own purpose, channel and text (with its versions / translations) — then continue from `replace_step_id`'s `next`. Record the communication with the **follow-up's `step_id`**, so email performance measures it as its own message. Remove the swap once used. With `at`: also move that step's scheduled job to `at`. |
| `move_next` | change the time of the run's pending scheduled job to `at`. Never earlier while a "Prevent Communication" rule matches the account (`RuleRuntimeService.first_blocking_action`): that rule **drops** a message, it does not delay it. |
| `switch_channel` | `run.preferred_channel = channel`. Contact steps (no fixed channel) try that channel first (`preferred_contact_channels`). Steps with a fixed channel are not changed. |
| `recheck_at` | queue a `reassess_run` job at `at`, nothing else |
| `pause` | stop automated contact: the existing `_pause_for_human_review`, reason "Adaptive: paused", until Operations releases it |
| `to_operations` | the existing `_pause_for_human_review`, with the decision's `reason` as the review's explanation |
| `stop` | cancel the run's pending jobs, run → Completed, note "Stopped by adaptive decision: {reason}". **Never** apply an Outcome to the account (no "Paid In Full", no balance change). |

**Checks before you carry it out**
- the action is in the list; `replace_step_id` is a planned Communication step of the run's pinned steps that was not sent yet; `follow_up_step_id` is a follow-up step of the same pinned steps;
- `at` is not in the past and not more than 30 days ahead (move it into that range);
- `channel` is enabled for the client and allowed for the customer;
- a decision that fails a check is saved with status `failed` and the reason; the plan goes on unchanged.

Compliance, send windows, frequency limits and suppression are **not** checked here — the existing send gate (`evaluate_send_eligibility`, `_compliance_gate`) already checks every message when it is sent (AC10).

**Newer events win (FR-07)**
- A new decision that changes the plan replaces the run's earlier one that is not done yet: status `superseded`, its scheduled job cancelled, its swap removed.
- Every job created by a decision carries `decision_id` in its payload. A job whose decision is `superseded` does nothing.

**Right before a send (FR-02, FR-10)**
- In `execute_step`, for **every** Communication step of an adaptive run: lock the run row (as in B4), then run `reassess` with `trigger: "message_due"` in the same job — also when there are no new events (the ticket asks for a reassessment whenever a scheduled message becomes due). If that cancelled or moved this step, stop; otherwise send as usual. When nothing changed, the AI team's function answers `keep_plan` at once, without an AI call.

**No duplicates (AC9)**
- The run-row lock; one pending `reassess_run` per run (B4); one send per planned step (the existing step idempotency); a swap is used once.

**New table `payflow_adaptive_decisions`** (the audit trail, FR-11)

| Column | Notes |
|---|---|
| `id`, `created_at` | |
| `run_id`, `account_id`, `strategy_id` | |
| `trigger_event_ids` | JSON list |
| `decided_by` | `safety` · `rules` · `ai` · `operations` |
| `action`, `replace_step_id`, `follow_up_step_id`, `at`, `channel` | the decision |
| `reason`, `confidence` | shown to Operations |
| `needs_approval` | bool |
| `status` | `executed` · `awaiting_approval` · `approved` · `modified` · `rejected` · `superseded` · `expired` · `failed` |
| `decided_by_user`, `decision_note`, `decided_at` | approval (B6) |
| `human_review_id` | the case review opened by `pause` / `to_operations` |
| `executed_at`, `outcome` | what really happened (JSON) |
| `context` | JSON: what `decide_next_action` was given |

### B6 — Approval and APIs

**When a decision needs approval**
- never for safety decisions, `keep_plan`, `recheck_at`, `pause`, `to_operations` (sending to a person is always safe);
- when `decide_next_action` returns `needs_approval: true`;
- when the client's `ai_mode` is `supervised_ai` and `decided_by` is `ai` (any other action).

**Not through the Human Review queue.** It does not fit: an account has only one open review (`create_review` returns the existing one), and approving a review changes the case status (`_apply_case_decision`) and can queue enrollments (`on_human_review_decided`). Instead the decision waits in `payflow_adaptive_decisions` with status `awaiting_approval`, and the client's reviewers get an in-app notification (`PayflowNotificationService`). `pause` and `to_operations` still use the real review queue — they are real case reviews.

**While it waits:** the run **carries on with its normal plan**.

| Reviewer action | What you do |
|---|---|
| **Approve** | carry the decision out if it still applies (not superseded, the step it changes not sent yet); else status `expired` |
| **Modify** (FR-08) | approve with changes — another `action`, `at`, `follow_up_step_id` or `channel`. Check it like any decision (B5), carry it out, status `modified`; keep both the AI's version and the reviewer's |
| **Reject** | status `rejected` with the reviewer's note |

**APIs for the frontend**

| API | What |
|---|---|
| `GET /payflow/workflow-runs/{run_id}/adaptive` | engagement state, events (newest first), decisions (action, why, confidence, status), the next scheduled step and its time, current swaps and preferred channel |
| `POST /payflow/workflow-runs/{run_id}/reassess` | the manual "Reassess plan" (FR-02) — records `reassess_requested` and queues the job; needs Create / Edit Workflows |
| `GET /payflow/adaptive-decisions?status=awaiting_approval&client_id=…` | decisions waiting for a reviewer |
| `POST /payflow/adaptive-decisions/{id}/approve` · `/modify` · `/reject` | body `{"note": "..."}` (optional); `/modify` also takes the changed fields; needs Create / Edit Workflows for the client |
| workflow create / update / get | `adaptive` field |

Call the button "Reassess plan": the case page already has an "AI reassess" button (`/accounts/{id}/ai-reassess`, a different, case-level feature).

Add the adaptive block to `GET /payflow/accounts/{id}/workflow-execution` too, so the case page shows it.

---

## 5. The handover function

The one place the AI plugs in. **You call it and ship a placeholder; the AI team replaces the body.** Do not put AI or business rules in it.

File: `app/modules/payflow/services/workflow_suggestion/next_action.py`

```python
def decide_next_action(context: dict) -> dict:
    """Placeholder until the AI team delivers the real one: never changes the plan."""
    return {"action": "keep_plan", "reason": "No change", "decided_by": "rules",
            "confidence": None, "needs_approval": False}
```

**`context` — what you pass** (no customer name, email address or phone number):

```python
{
  "trigger": "event" | "timer" | "message_due" | "manual",
  "run": {"id", "status", "current_step_id", "started_at", "strategy_version",
          "preferred_channel", "swaps"},
  "workflow": {"id", "name", "segment", "entry_node_id",
               "steps": [...],                    # the run's pinned steps, follow-up steps included
               "follow_ups": {"email_not_opened": "f1", ...}},   # situation → step id
  "next_planned": {"step_id", "kind", "at"} | None,   # the run's pending scheduled job
  "account": {"days_past_due", "outstanding_balance", "original_balance", "recovered_balance",
              "collection_status", "payment_status",   # workflow_condition_runtime.payment_status(..., run=run)
              "plan": {"status", "missed_instalments"} | None,
              "language", "email_ok", "sms_ok"},
  "client": {"id", "ai_mode", "channels"},
  "new_events": [{"id", "type", "at", "channel", "step_id", "detail"}],   # this reassessment's trigger
  "recent_events": [...],                           # same shape, the run's last 30 days
  "messages_sent": [{"step_id", "channel", "sent_at", "status", "opened", "clicked"}],
  "decisions": [{"action", "status", "decided_by", "at"}],                # the run's last 10
  "limits": {"max_messages_per_7_days", "messages_sent_7_days"}
}
```

**What it returns**

```python
{
  "action": "keep_plan" | "use_follow_up" | "move_next" | "switch_channel"
            | "recheck_at" | "pause" | "to_operations" | "stop",
  "replace_step_id": str | None,     # use_follow_up
  "follow_up_step_id": str | None,   # use_follow_up
  "at": "ISO datetime" | None,       # move_next, recheck_at, optional for use_follow_up
  "channel": "Email" | "SMS" | None, # switch_channel
  "reason": str,                     # one or two plain sentences for Operations
  "confidence": int | None,          # 0-100
  "decided_by": "rules" | "ai",
  "needs_approval": bool
}
```

If it raises or returns something invalid: save the decision as `failed` and keep the plan. A broken AI must never stop the worker.

Ignore keys you do not know in the result: the AI team adds a field for draft text later (section 6).

For your tests, replace it with a fake that returns each action.

---

## 6. Where the AI team takes over

When B1–B6 are merged, we build:

1. **The real `decide_next_action`:** code rules for the known situations (not opened, opened not clicked, clicked not paid, paid part, no activity) and an AI call only for unclear combinations (an old email opened late, several messages clicked, a part payment after a follow-up was scheduled). It returns the reason and confidence; low confidence → `to_operations`.
2. **Follow-up steps written by the AI:** Suggest, Regenerate and Write messages also write the follow-up steps (`follow_up_for`), with the same tone rules, checks, age versions, translations and compliance wording as the other messages — approved with the workflow.
3. **New text when no follow-up fits** (e.g. a hand-made workflow without follow-ups): the AI drafts it, a person approves it once, and it is saved into the workflow as a follow-up step for everyone after.
4. **Learning later:** which decisions led to payments.

Please tell us when B1–B6 are on `develop`.

---

## 7. Done when

- [ ] The existing test suite passes: workflows are now adaptive by default, and the placeholder always keeps the plan.
- [ ] Adaptive **off**: the workflow behaves exactly as before.
- [ ] Every event in the B1 table is recorded once (repeated webhooks are saved once) and linked to the run — including events from older messages.
- [ ] Each timer event fires once, only for adaptive runs, at the configured time — never for emails without open tracking, or that failed.
- [ ] One pending `reassess_run` per run; a burst of events gives one reassessment; "delivered" events do not wake the run.
- [ ] A reassessment and a send never run at the same time for one run (row lock); every send of an adaptive run is reassessed first.
- [ ] Each action in B5 works; a swap sends the follow-up in the planned step's place, once, recorded under the follow-up's `step_id`, and the plan continues after it.
- [ ] Paid in full stops the run without touching the account's outcome or balance.
- [ ] A newer decision supersedes an older one; a superseded job does nothing.
- [ ] A decision waiting for approval does not pause the run or change the case; approve carries it out, modify carries out the reviewer's version, reject records it.
- [ ] A failing or invalid `decide_next_action` keeps the plan and records `failed`.
- [ ] The new APIs return the events, decisions and next step for a run.

---

## 8. Not in this ticket

- Inbound email / SMS replies and answering customers (separate ticket).
- Changing payment terms, settlements or CRM Promise-to-Pay records.
- Treating a missing open as proof the email was not read.
- The frontend screens (FE team, after this).
