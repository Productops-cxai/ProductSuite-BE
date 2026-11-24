# Adaptive AI Workflows — BE done (handover)

**Status:** BE foundation complete (B1–B6 + safety locks 77–82).  
**Not in this BE delivery:** real AI NBA brain, FE screens.  
**Out of scope (by design):** inbound customer reply handling.

Updates use **POST** (not PATCH/PUT), same as the rest of PayFlow.

---

## 1. What BE built (summary)

| Area | Done |
|---|---|
| Engagement events + dedupe | Yes |
| Silence / timer signals | Yes |
| Workflow `adaptive` flag + follow-up steps | Yes |
| Continuous reassess loop + safety rules | Yes |
| Apply decisions (swap / move / channel / pause / stop) + supersession | Yes |
| Approval APIs + notifications (outside Human Review queue) | Yes |
| Adaptive off = old behaviour | Yes |
| Webhooks/payments never break on event write | Yes (`safe_record_engagement_event`) |
| No auto CRM PTP / settlements / payment terms | Yes |

---

## 2. Where BE changed (files)

### New

| File | Role |
|---|---|
| `app/modules/payflow/services/engagement_service.py` | Record / dedupe engagement events; timer emitters; safe wrapper for hooks |
| `app/modules/payflow/services/adaptive_workflow_service.py` | Reassess, apply decisions, approve/modify/reject, adaptive snapshot |
| `app/modules/payflow/services/workflow_suggestion/next_action.py` | AI handover — **placeholder** `keep_plan` only |

### Models / DB (`app/infrastructure/database/models/__init__.py`)

| Table / columns | Purpose |
|---|---|
| `payflow_engagement_events` | Normalized engagement / payment signals |
| `payflow_adaptive_decisions` | Decision audit + approval fields |
| `payflow_strategies.adaptive` | Per-workflow on/off (default `true`) |
| `payflow_workflow_runs.engagement_state` | Ops label |
| `payflow_workflow_runs.preferred_channel` | After `switch_channel` |
| `payflow_workflow_runs.swaps` | `{planned_step_id: follow_up_step_id}` |
| `payflow_workflow_runs.last_reassessed_at` | Last reassess time |

> Schema sync / migrations: run whatever your env already uses for PayFlow (`create_all` / migrate). Ask BE before applying in shared envs.

### Wired into existing services

| File | Change |
|---|---|
| `communication_service.py` | SendGrid/Twilio → `safe_record_engagement_event` |
| `payment_portal_service.py` | Portal / payment signals → safe record |
| `email_preference_service.py` | Unsubscribe → safe record |
| `workflow_execution_service.py` | Pre-send reassess on adaptive runs; swap at send; payment → engagement; `workflow-execution` includes `adaptive` block |
| `workflow_queue.py` | `reassess_run` job type |
| `strategy_service.py` | `adaptive` field; `config.follow_up_for`; follow-up validation; stats `follow_ups`; follow-ups skipped in main traversal |
| `notification_service.py` | `notify_adaptive_decision_awaiting` |
| `routes.py` | New adaptive endpoints (below) |
| `schemas.py` | Workflow + adaptive request/response models |
| `app/shared/enums.py` | `ADAPTIVE_DECISION_AWAITING`, job/run helpers as needed |

---

## 3. AI gaps (for AI team)

BE calls one function only. **Do not put business rules elsewhere.**

**File:** `app/modules/payflow/services/workflow_suggestion/next_action.py`

```python
def decide_next_action(context: dict) -> dict:
    # PLACEHOLDER — always keep_plan
```

### Still missing (AI)

1. Real next-best-action from `context` (not always `keep_plan`).
2. Allowed actions only:  
   `keep_plan` | `use_follow_up` | `move_next` | `switch_channel` | `recheck_at` | `pause` | `to_operations` | `stop`
3. Return shape BE already normalizes:

| Field | Notes |
|---|---|
| `action` | required |
| `replace_step_id` / `follow_up_step_id` | for `use_follow_up` — must be valid step ids on the run |
| `at` | ISO datetime for `move_next` / `recheck_at` (optional on follow-up) |
| `channel` | `Email` or `SMS` for `switch_channel` |
| `reason` | short text for Ops |
| `confidence` | int or null |
| `decided_by` | `rules` \| `ai` \| `safety` \| `operations` |
| `needs_approval` | bool — supervised clients also force approval when `decided_by=ai` |

4. Context already excludes customer name / email / phone — keep it that way.
5. Invalid / raised → BE keeps plan (`failed` / `keep_plan`); worker must not die.
6. Follow-up **copy generation** (new message text) is AI/product later — BE only swaps to **already-approved** follow-up steps on the workflow.
7. Unexpected scenarios → prefer `pause` / `to_operations`, not free-form new branches.

Safety rules (paid in full, unreachable) already run **before** AI inside BE — do not duplicate / override those carelessly.

---

## 4. FE developer — APIs only

Keep UI logic thin. Call these contracts exactly. Do **not** invent PATCH/PUT. Do **not** route adaptive approvals through Human Review APIs.

### 4.1 Changed existing APIs (backward compatible extras)

#### Workflows — create / update / get / list

| Method | Path | Change |
|---|---|---|
| POST | `/payflow/workflows` | Body may include `adaptive` (default `true`) |
| POST | `/payflow/workflows/{id}/update` | Body may include `adaptive` (toggle alone does **not** force re-review) |
| GET | `/payflow/workflows` · `/payflow/workflows/{id}` | Response includes `adaptive: bool` |
| — | step `config` | Optional `follow_up_for` |

**`follow_up_for` allowed values (exactly):**

- `email_not_opened`
- `opened_not_clicked`
- `clicked_not_paid`
- `payment_failed` (added by the AI team: the customer's payment did not go through)
- `payment_partial`
- `no_activity`

Rules FE must respect when saving:

- Step must be `kind: Communication`
- Follow-up steps are **orphans** (not on the main `next` path)
- One follow-up situation per workflow (BE validates)
- Stats may include `follow_ups` count

#### Case workflow execution

| Method | Path | Change |
|---|---|---|
| GET | `/payflow/accounts/{account_id}/workflow-execution` | Response `execution` may include `adaptive` object (same shape as §4.2 snapshot) |

#### Workflow run detail

| Method | Path | Change |
|---|---|---|
| GET | `/payflow/workflow-runs/{run_id}` | May include `adaptive` object |

#### Notifications list (existing)

| Method | Path | Change |
|---|---|---|
| GET | `/payflow/notifications` | New `notification_type`: **`adaptive_decision_awaiting`** |

- `entity_type`: `adaptive_decision`
- `entity_id`: decision id (string)
- `link`: typically `/payflow/cases/{account_id}`
- This is **not** Human Review. Do not open the HR approve/reject UI for it.

#### Unrelated — do not confuse

| Method | Path | Note |
|---|---|---|
| POST | `/payflow/accounts/{account_id}/ai-reassess` | **Case-level** AI suggest (old). **Not** adaptive “Reassess plan”. |

---

### 4.2 New APIs

Base prefix: `/payflow`  
Auth: same PayFlow product session as other routes.

#### GET `/payflow/workflow-runs/{run_id}/adaptive`

Permission: `view_workflows` (+ client scope).

Returns adaptive snapshot:

```json
{
  "run_id": 1,
  "account_id": 10,
  "client_id": 2,
  "strategy_id": 5,
  "adaptive": true,
  "engagement_state": "Opened",
  "preferred_channel": "Email",
  "swaps": { "s2": "f1" },
  "last_reassessed_at": "2026-11-23T12:00:00+00:00",
  "next_scheduled": {
    "step_id": "s2",
    "kind": "Communication",
    "title": "Reminder",
    "at": "2026-11-24T09:00:00+00:00"
  },
  "events": [ /* newest first */ ],
  "decisions": [ /* newest first */ ]
}
```

#### POST `/payflow/workflow-runs/{run_id}/reassess`

Permission: **`create_edit_workflows`** for that run’s client.  
Button label: **Reassess plan**.

- No body required beyond auth.
- Records `reassess_requested` and queues job.
- Response:

```json
{
  "run_id": 1,
  "job_id": 99,
  "scheduled_at": "2026-11-23T12:00:01+00:00"
}
```

- `400` if run terminal / missing / workflow not adaptive.

#### GET `/payflow/adaptive-decisions`

Permission: `view_workflows` (scoped).

Query:

| Param | Notes |
|---|---|
| `status` | e.g. `awaiting_approval` |
| `client_id` | optional |
| `run_id` | optional |
| `limit` | 1–500, default 100 |

```json
{ "items": [ /* PayflowAdaptiveDecisionItem */ ] }
```

#### POST `/payflow/adaptive-decisions/{id}/approve`

Permission: **`create_edit_workflows`** for decision’s client.

Body:

```json
{ "note": "optional" }
```

#### POST `/payflow/adaptive-decisions/{id}/modify`

Permission: **`create_edit_workflows`** for decision’s client.

Body (all optional except send JSON object):

```json
{
  "note": "optional",
  "action": "use_follow_up",
  "replace_step_id": "s2",
  "follow_up_step_id": "f1",
  "at": "2026-11-25T10:00:00+00:00",
  "channel": "SMS"
}
```

- Omitted fields keep the pending decision’s values.
- `action` / `channel` / step ids must be valid or BE returns `400`.

#### POST `/payflow/adaptive-decisions/{id}/reject`

Permission: **`create_edit_workflows`** for decision’s client.

Body:

```json
{ "note": "optional" }
```

---

### 4.3 Decision item shape (list + approve/modify/reject response)

```json
{
  "id": 1,
  "run_id": 10,
  "account_id": 20,
  "strategy_id": 5,
  "client_id": 2,
  "trigger_event_ids": [1, 2],
  "decided_by": "ai",
  "action": "use_follow_up",
  "replace_step_id": "s2",
  "follow_up_step_id": "f1",
  "at": null,
  "channel": null,
  "reason": "Customer opened but did not click",
  "confidence": 72,
  "needs_approval": true,
  "status": "awaiting_approval",
  "decided_by_user": null,
  "decision_note": null,
  "decided_at": null,
  "human_review_id": null,
  "executed_at": null,
  "outcome": null,
  "created_at": "2026-11-23T12:00:00+00:00"
}
```

**`status` values FE may see:**

| Status | Meaning |
|---|---|
| `awaiting_approval` | Show approve / modify / reject |
| `approved` | Applied as proposed |
| `modified` | Applied with reviewer changes (`outcome.ai_decision` + reviewer) |
| `rejected` | Not applied |
| `executed` | Auto-applied (no approval) |
| `superseded` | Newer plan-changing decision won |
| `expired` | Approved/modified too late (e.g. step already sent) |
| `failed` | Could not apply |

**Important UX / API rules**

1. While `awaiting_approval`, the **run continues the normal plan** — do not treat the case as Human Review paused.
2. `pause` / `to_operations` still use the **real** Human Review queue — different feature.
3. Approve/modify/reject on a non-`awaiting_approval` row → **`409 Conflict`**.
4. Always send JSON body `{}` minimum on approve/reject if your client requires a body.
5. Do not call adaptive decision APIs with Human Review permission names; gate is **`create_edit_workflows`**.

---

### 4.4 Quick FE checklist (so BE doesn’t get blamed)

- [ ] Read `adaptive` on workflow get/list; send it on create/update when toggling
- [ ] Persist `config.follow_up_for` only with allowed enum values
- [ ] Case page: prefer `execution.adaptive` from workflow-execution (or GET `.../adaptive`)
- [ ] “Reassess plan” → `POST .../workflow-runs/{id}/reassess` (not `ai-reassess`)
- [ ] Inbox: handle `adaptive_decision_awaiting` separately from HR
- [ ] Queue: `GET /adaptive-decisions?status=awaiting_approval`
- [ ] Actions: POST `.../approve` | `.../modify` | `.../reject` only
- [ ] No PATCH/PUT for these updates

---

## 5. BE complete vs full story

| Layer | Status |
|---|---|
| **BE** (events → reassess → apply/approve → APIs → safety) | Complete |
| **AI** (`decide_next_action` real brain + content) | Gap — §3 |
| **FE** (editor + monitoring + approval UI) | Not started — use §4 |

When AI + FE land on top of this contract, story ACs can close end-to-end.
