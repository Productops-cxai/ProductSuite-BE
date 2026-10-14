# AI Workflow Suggestion

PayFlow can suggest **collection workflows** for a portfolio using an LLM. Code groups the portfolio's accounts into segments, the AI designs one workflow per segment, code checks every workflow, and the result is saved as **"AI Proposed"** for people to review, approve and activate.

| | |
|---|---|
| **Owner** | AI team |
| **Code** | `app/modules/payflow/services/workflow_suggestion/` |
| **APIs** | `GET /payflow/clients/{client_id}/portfolios/{portfolio_id}/segments` · `POST /payflow/workflows/suggest` · `POST /payflow/workflows/{id}/regenerate` |
| **LLM** | Groq · `openai/gpt-oss-120b` |
| **Build plan / history** | [`WORKFLOW_SUGGESTION_PLAN.md`](WORKFLOW_SUGGESTION_PLAN.md) |

---

## 1. Overview

### The problem

A portfolio can hold thousands of accounts, each in a different situation (never contacted, partly paid, promised to pay, 90 days late…). One workflow per account is too costly and impossible to approve; one workflow per portfolio is too generic.

### The solution

```
Portfolio accounts
      │
      ▼
 SEGMENT  (code)   →  3–8 groups of similar accounts, no overlaps, no gaps
      │
      ▼
 SUGGEST  (AI)     →  one workflow per segment
      │
      ▼
 VALIDATE (code)   →  broken workflows are sent back to the AI to fix
      │
      ▼
 SAVE              →  "AI Proposed" (existing workflow create path)
      │
      ▼
 REVIEW   (people) →  begin-review → approve → activate  (existing APIs)
```

### Key ideas

- **Code decides the groups, the AI designs the strategy.** Segments are built by code, so they are repeatable and every account fits exactly one.
- **Rules are handled by code, not the AI.** Rule text never goes into the prompt (see [§4](#4-how-rules-are-handled)).
- **Nothing broken is ever saved.** Every workflow passes the validator, or the request fails and saves nothing. All workflows of a request are saved together — if one fails to save, none are.
- **Existing code is reused.** Saving uses the normal workflow create path, so AI workflows behave like any other workflow.

---

## 2. How it works — step by step

Example numbers below come from the demo client **Maple Energy → Small Business** (150 accounts).

### Step 1 — Check who is asking
Same checks as creating a workflow by hand:
1. The user's PayFlow role has **Create / Edit Workflows**.
2. The user may work on **this client** (Ops Admin: all clients; supervisors: assigned clients).

Fail → `403`, the AI is never called.

### Step 2 — Load the data (`context.py`)

| What | Notes |
|---|---|
| Client | Type, AI mode, language, currency, **enabled channels**, governance toggles |
| Portfolio + accounts | Portfolio must belong to the client, else `404` |
| Rules | **Active** system rules + **Active** client rules of this client. Draft, Inactive and other clients' rules are skipped |

### Step 3 — Make segments (`segmenter.py`, no AI)

1. **Filter** — drop accounts that need no workflow: closed, resolved, in human review, on provincial hold, nothing owed. *(150 → 130)*
2. **Label** each account:

   | Label | From | Values |
   |---|---|---|
   | Situation | `collection_status` | Active / Promise to Pay / Payment Plan |
   | Days-late band | `days_past_due` | 1–29 / 30–59 / 60–89 / 90+ |
   | Contact state | contact dates + amount recovered | Never contacted / Contacted, nothing paid / Partly paid |
   | Balance band | `outstanding_balance` | Under $500 / $500–1,500 / $1,500–5,000 / $5,000+ |

   Promise to Pay and Payment Plan accounts are one group each.
3. **Group** accounts with the same labels.
4. **Merge small groups** (< 20 accounts) by widening one label to "All …", in this order: **balance → contact state → days late** (days late is kept longest — it matters most). Situations are never merged. More than 8 groups → the minimum size grows 20% at a time until it fits.
5. **Describe** — each group becomes a segment with an id (A, B…), a label, the UI's "Who it applies to" values, and stats for the AI. *(→ 6 segments)*

No eligible accounts → `400`.

### Step 4 — Turn rules into design limits (`rules.py`, no AI)

| Limit | From |
|---|---|
| Allowed channels | Client settings (Email / SMS) |
| Max messages per 7 days | "Prevent Communication when Messages Sent (7 days) ≥ N" rules — the strictest wins |
| 3–15 steps, max 5 messages, a Wait between messages | Config |

### Step 5 — Batches (free tier only)
The Groq free tier allows 8,000 tokens/minute, so segments are sent `GROQ_SEGMENTS_PER_CALL` at a time (e.g. 2 → `[A,B] [C,D] [E,F]`). `0` = all in one call.

### Step 6 — Ask the AI and check the answer (per batch)

**6a. Prompt (`prompt.py`)**
- *System:* role · flowchart rules (unique ids, Trigger first, `next` / `yes` / `no`, which steps may end a path, no loops) · what each step kind needs · allowed values from the workflow catalog (only enabled channels and their templates) · limits from Step 4 · strategy guidance (incl. branching on delivery, link clicks, disputes, high balances) · 3 example workflows, one with real branches (email bounced → SMS, dispute → hold).
- *User:* client summary · portfolio · the batch's segments (label + stats).

**6b. Groq call (`llm.py`)** — the AI returns each workflow with a structured `reasoning` object (`segment_fit`, `channels`, `timing`, `escalation`, and `changes_made` on regenerate), each 1–2 sentences quoting the segment's numbers. Low reasoning effort, JSON schema with the catalog's allowed values (`segment_id` limited to the batch). Rate limited (429) → waits as told and retries (max 5). Other failures → `502`.

**6c. Validate (`validator.py`)** — structure, flowchart, allowed values, step settings (incl. the save step's rule that timing needs a reference event), limits (see [§6](#6-code-map)). Returns plain errors such as `Workflow B: has 16 steps (allowed 3-15)`.

**6d. Fix** — good workflows are kept; broken ones go back to the AI **with the error list** (max 2 fix attempts per batch). Still broken → `502`, nothing saved.

All batches must pass before anything is saved.

### Step 7 — Save (`service.py` → existing `create_strategy`)

| Field | Value |
|---|---|
| `status`, `origin` | `AI Proposed` |
| `source` | `AI Generated` |
| `segment` | UI dropdown values + `situation` + `contact_state` |
| `steps`, `entry_node_id` | The AI flowchart (timing labels added automatically) |
| `cases_covered` | Accounts in the segment |
| `ai_context` | Segment, accounts, avg owed, avg days late, email consent, the AI's **reasoning** ("Why it fits", "Channels", "Timing", "Escalation"; plus "Changes made" on regenerate), one **"Governed by"** line per rule |
| `ai_proposal_snapshot` | Frozen copy of the AI original (kept after human edits) |

All workflows are saved in **one transaction** (`create_strategy(..., commit=False)`, then one commit): if any save fails, it is rolled back and nothing is saved. Ops Admins and the client's supervisors are notified **after** the commit, one notification per workflow (existing behaviour) — notifications commit on their own, so sending them earlier would save half a result.

### Step 8 — Response
The saved workflows, in the same shape as `GET /payflow/workflows/{id}`.

### After the API — people (existing APIs)

```
AI Proposed ──begin-review / edit──► Under Review ──approve──► Approved ──activate──► Active
     ▲                                                                                  │
     └──── reject ("regeneration requested")                           deactivate ──► Inactive
```
Editing an AI workflow marks it **Human Modified**; the AI original stays in `ai_proposal_snapshot`.
To ask the AI for an improved version instead, a reviewer calls **Regenerate** with remarks ([§3.3](#33-regenerate-a-workflow)) — a new linked "AI Proposed" workflow is created and the original is left as it is.

---

## 3. API reference

Both APIs need `Authorization: Bearer <access_token>` (from `POST /auth/login`). Both are listed in Swagger (`/docs`).

### 3.1 Preview segments

```
GET /payflow/clients/{client_id}/portfolios/{portfolio_id}/segments
```

Shows how a portfolio would be segmented. **No AI, nothing saved, < 1 s.** Useful before spending AI calls.

| | |
|---|---|
| Permission | PayFlow access + allowed to **view** workflows for the client |
| Response | `PortfolioSegmentsResponse` |

Real response (trimmed to 2 segments):

```json
{
  "client_id": 4,
  "portfolio_id": 3,
  "portfolio_name": "Small Business",
  "total_accounts": 150,
  "eligible_accounts": 130,
  "excluded": { "closed": 0, "resolved": 9, "human_review": 7, "on_hold": 4, "no_balance": 0 },
  "segments": [
    {
      "segment_id": "A",
      "label": "Active · 1 – 29 days past due · Any contact history · All balances",
      "segment": {
        "age_band": "All ages", "postal_region": "All regions", "balance_band": "All balances",
        "delinquency": "1 – 29 days past due", "language": "All languages", "tenure": "All customers",
        "situation": "Active", "contact_state": "any"
      },
      "stats": {
        "accounts": 37, "total_outstanding": 451305.09, "avg_outstanding": 12197.43,
        "avg_days_past_due": 11, "with_email_pct": 100, "with_mobile_pct": 73,
        "email_consent_pct": 89, "languages": { "EN": 34, "FR": 3 }
      }
    },
    {
      "segment_id": "B",
      "label": "Active · 30 – 59 days past due · Any contact history · All balances",
      "segment": { "...": "same fields as above" },
      "stats": { "accounts": 23, "avg_days_past_due": 41, "...": "..." }
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `excluded` | Accounts left out, by reason |
| `segment` | Same keys as a workflow's "Who it applies to" panel, plus `situation` and `contact_state` |
| `stats` | What the AI sees about the segment |

### 3.2 Suggest workflows

```
POST /payflow/workflows/suggest
```

```json
{ "client_id": 4, "portfolio_id": 3 }
```

| | |
|---|---|
| Permission | **Create / Edit Workflows** for the client |
| Response | `SuggestWorkflowsResponse` — `client_id`, `portfolio_id`, `portfolio_name`, `workflows[]` |
| Time | Free tier **1–5 min** · paid tier ~20–40 s (estimate) |
| Re-running | Adds new AI Proposed workflows next to existing ones (nothing is deleted) |

Each item in `workflows` is a normal workflow (`PayflowStrategyItem`). Real example from a live run (trimmed):

```json
{
  "id": 1,
  "code": "early-stage-friendly-email-sms-link",
  "name": "Early Stage Friendly Email + SMS Link",
  "status": "AI Proposed",
  "origin": "AI Proposed",
  "source": "AI Generated",
  "version": 1,
  "summary": "First email reminder, then SMS payment link if unpaid, with supervisor review as fallback.",
  "cases_covered": 28,
  "segment": {
    "delinquency": "1 – 29 days past due", "balance_band": "All balances",
    "situation": "Active", "contact_state": "never_contacted", "...": "other dropdowns = All …"
  },
  "entry_node_id": "s1",
  "steps": [
    { "id": "s1", "kind": "Trigger", "title": "Case received",
      "config": { "reference_event": "Case Received" }, "next": "s2" },
    { "id": "s2", "kind": "Communication", "title": "Email reminder",
      "config": { "channel": "Email", "purpose": "Payment Reminder", "template_id": "em-reminder",
                  "amount": 0, "unit": "Days", "direction": "After", "reference_event": "Case Received" },
      "next": "s3", "timing": "Immediately when case received occurs" },
    { "id": "s3", "kind": "Wait", "title": "Wait 3 days",
      "config": { "amount": 3, "unit": "Days", "direction": "After", "reference_event": "Previous Email" },
      "next": "s4", "timing": "3 Days After Previous Email" },
    "... 6 more steps (Condition → SMS link → Wait → Condition → Human Review / Outcome)"
  ],
  "stats": { "steps": 9, "branches": 2, "emails": 1, "sms": 1 },
  "ai_context": [
    { "label": "Segment", "value": "Active · 1 – 29 days past due · Never contacted · All balances" },
    { "label": "Accounts", "value": "28" },
    { "label": "Avg outstanding", "value": "7,238.13 CAD" },
    { "label": "Avg days past due", "value": "14" },
    { "label": "Email consent", "value": "79%" },
    { "label": "Why it fits", "value": "Accounts are 1-29 days past due with balances $1,500-$5,000; early-stage cases respond well to gentle reminders and an easy payment option." },
    { "label": "Channels", "value": "Both email and SMS have 100% reach, so we start with email (high consent) and follow with SMS for a quick payment link." },
    { "label": "Timing", "value": "Wait 3 days after the email to give time for payment, then 5 days after the SMS link before escalating." },
    { "label": "Escalation", "value": "If no payment after the SMS link, a Human Review step lets a supervisor decide on further action." },
    { "label": "Governed by", "value": "Quiet Period Guard (system rule): IF Messages Sent (7 days) greater than or equal to 4 THEN Prevent Communication" }
  ]
}
```

> In real responses every step's `config` lists all fields, unused ones as `null`.

### 3.3 Regenerate a workflow

```
POST /payflow/workflows/{strategy_id}/regenerate
```

```json
{ "remarks": "Less aggressive: no SMS in the first 7 days, and wait at least 5 days between messages." }
```

Creates an **improved version** of one AI workflow, guided by a reviewer's remarks.

| | |
|---|---|
| Permission | **Create / Edit Workflows** for the client |
| Allowed for | AI-generated workflows with status **AI Proposed** or **Under Review** (not Approved / Active / Inactive, not human-made) that belong to a portfolio — old workflows from before the workflow rework may have none and are refused before the AI is called |
| `remarks` | Required, 3–2,000 characters |
| Response | The new workflow (`PayflowStrategyItem`), status **AI Proposed** |
| Time | One AI call — ~10–40 s on the free tier |

How it works: the current workflow (including any human edits) + its stored segment and stats + the remarks go into the same prompt as Suggest → AI → same validator and retries → saved as a **new row** with a **new id**. The **original is not changed**. The new workflow's `ai_context` links them:

```json
{ "label": "Revision of", "value": "#1 Early Stage Friendly Email & SMS Link (v1)" },
{ "label": "Reviewer remarks", "value": "Less aggressive: no SMS in the first 7 days, and wait at least 5 days between messages." },
{ "label": "Changes made", "value": "Removed the SMS 3 days after the email; added a 5-day wait; second contact is now an email payment link; SMS moved after day 7." }
```

Live example: original *Email day 0 → SMS day 3* became *Email day 0 → Email day 5 → SMS day 10*. After regenerating, both are "AI Proposed" — the reviewer approves one and rejects / deletes the other.

### 3.4 Errors

Errors use the project's normal format: `{ "detail": "<message>" }`.

| Code | When | Example `detail` |
|---|---|---|
| 401 | Not logged in / bad token | `Not authenticated` |
| 403 | Missing Create / Edit Workflows, or no access to the client | `Permission required: create_edit_workflows` |
| 404 | Client, portfolio or workflow not found, or portfolio of another client | `Portfolio not found for this client` |
| 400 | No eligible accounts (suggest); not an AI workflow, wrong status or no portfolio (regenerate) | `Only AI Proposed or Under Review workflows can be regenerated` |
| 422 | Request body invalid (e.g. empty `remarks`) | FastAPI validation detail |
| 502 | AI not configured / unreachable / reply cut off / still invalid after 2 fixes — **nothing saved** | `AI could not produce valid workflows: Workflow B: has 16 steps (allowed 3-15); …` |

---

## 4. How rules are handled

Rules are IF/THEN logic, so **code handles them**. Rule text is **not** in the prompt.

| Kind of rule | Example | What happens |
|---|---|---|
| Limits a workflow's shape | Messages Sent (7 days) ≥ 3 → Prevent Communication | Code turns it into **one limit line** in the prompt ("At most 2 messages in any 7 days") and the validator enforces it |
| Everything else | Balance > 10,000 → Require Human Review; Dispute → Hold Action | Listed on each saved workflow as **"Governed by"** for reviewers; to be enforced **while workflows run** (backend, future) |

- Rules are never copied into workflow steps — copies would go stale when a rule changes.
- New rules built from the existing fields / actions need no code change. A rule code cannot turn into a design limit is automatically "run time".
- Test data: of 9 AI mistakes caught in two live runs, only 1 was rule-related (and that limit was already in the prompt). Adding rule text to the prompt would not reduce retries.

---

## 5. Configuration

`.env` (see `.env.example`):

| Setting | Default | Notes |
|---|---|---|
| `GROQ_API_KEY` | — | Required. Missing → `502` |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | |
| `GROQ_TIMEOUT_SECONDS` | `60` | Per AI call |
| `GROQ_REASONING_EFFORT` | `low` | `low` keeps "thinking" small (~50–900 tokens) |
| `GROQ_SEGMENTS_PER_CALL` | `3` | **Free-tier batching.** Use `2` for reliability on the free tier; `0` = all segments in one call (paid tier) |

Code constants:

| Constant | Default | File |
|---|---|---|
| `MIN_SEGMENT_SIZE` / `MAX_SEGMENTS` | 20 / 8 | `workflow_suggestion/config.py` |
| `MIN_STEPS` / `MAX_STEPS` / `MAX_COMMUNICATIONS` | 3 / 15 / 5 | `workflow_suggestion/config.py` |
| Band meanings (days late, balance, age, tenure, region, language) | catalog bands | `workflow_suggestion/config.py` |
| `MAX_FIX_ATTEMPTS` | 2 | `workflow_suggestion/service.py` |
| `RATE_LIMIT_RETRIES` | 5 | `workflow_suggestion/llm.py` |

### Removing free-tier batching (paid customers)
Set `GROQ_SEGMENTS_PER_CALL=0` in `.env` — one AI call per portfolio, no code change. To remove the code entirely, make `_batches()` in `service.py` return `[segments]`.

---

## 6. Code map

All in `app/modules/payflow/services/workflow_suggestion/` unless noted.

| File | Main functions | Job |
|---|---|---|
| `config.py` | constants | Band meanings, limits, segment sizes |
| `context.py` | `load_client_context`, `load_rules`, `load_portfolio_accounts` | Load data |
| `segmenter.py` | `build_segments`, `filter_eligible`, `label_account`, `group_accounts`, `merge_small_groups`, `describe_segment`, **`match_account_to_segment`** | Segments; matching accounts to segments |
| `rules.py` | `design_limits`, `governing_rules` | Rules → limits; "Governed by" text |
| `prompt.py` + `prompts/workflow_examples.json` | `build_messages`, `build_feedback`, `build_revision` | Prompt, retry feedback, regenerate text, examples |
| `llm.py` | `build_output_schema`, `call_llm` | Groq call, JSON schema, rate-limit waits |
| `validator.py` | `validate_all` (+ `validate_structure`, `validate_flowchart`, `validate_catalog_values`, `validate_step_config`, `validate_limits`) | Checks AI workflows |
| `service.py` | `WorkflowSuggestionService.preview_segments`, `.suggest_workflows`, `.regenerate_workflow`; `_batches` | Orchestration, retries, saving |
| `app/modules/payflow/routes.py` | `preview_portfolio_segments`, `suggest_workflows`, `regenerate_workflow` | Routes ("Workflow suggestions (AI)" section) |
| `app/modules/payflow/services/strategy_service.py` | `create_strategy(..., commit=False)`, `notify_awaiting` | Existing save path; `commit=False` + `notify_awaiting` let Suggest save all workflows in one transaction |
| `app/modules/payflow/schemas.py` | `PortfolioSegmentsResponse`, `SuggestWorkflowsRequest`, `SuggestWorkflowsResponse`, `RegenerateWorkflowRequest` | Request / response models |
| `app/core/exceptions.py` | `ServiceUnavailableError` | AI failures → `502` |
| `app/core/config.py` | `GROQ_*` settings | |

---

## 7. Testing

### Automated tests (no network — the AI is faked)

```bat
venv\Scripts\python.exe -m pytest
```

| File | Tests | Covers |
|---|---|---|
| `tests/test_workflow_config.py` | 4 | Band meanings match the catalog; no gaps / overlaps |
| `tests/test_workflow_context.py` | 5 | Loaders; only Active + own-client rules |
| `tests/test_workflow_segmenter.py` | 14 | Filtering, labels, merge order, every account in exactly one segment, determinism, matching |
| `tests/test_workflow_prompt_llm.py` | 17 | Limits from rules, prompt content, examples valid, schema, Groq errors, rate-limit waits |
| `tests/test_workflow_validator.py` | 31 | One failing case per check; prompt examples pass |
| `tests/test_workflow_suggestion_api.py` | 15 | Preview + suggest: success, retry-then-pass (incl. timing without a reference event), give-up (nothing saved), all-or-nothing save, notifications after saving, batching, 400 / 403 / 404 / 502 |
| `tests/test_workflow_regenerate.py` | 17 | Regenerate: new linked version, original unchanged, retry, give-up, allowed / refused statuses, no portfolio (AI not called), 400 / 403 / 404 / 422 / 502 |

### Manual testing with Postman

1. Start the backend: `venv\Scripts\python.exe -m uvicorn app.main:app --reload`
2. Import [`docs/postman/PayFlow_AI_Workflows.postman_collection.json`](postman/PayFlow_AI_Workflows.postman_collection.json).
3. Run **1. Login** (saves the token), then the other requests in order.
4. Keep Postman's request timeout at `0` — Suggest takes minutes on the free tier.

The collection's variables point at the demo client **Maple Energy (Demo)** (client 4, portfolios 2 and 3) seeded into a local DB — adjust the ids for your data. Any portfolio with imported accounts works.

### Live test results (free tier, 600 accounts, 23 active rules)

| | Run A (Email + SMS, max 3 msgs / 7 days) | Run B (Email only, max 2 msgs / 7 days) |
|---|---|---|
| Result | 7 valid workflows | 7 valid workflows |
| Time | 249 s (204 s waiting on rate limits) | 270 s (221 s waiting) |
| AI calls | 7 (3 retries) | 8 (4 retries) |
| Mistakes caught & fixed | 4 (all "too many steps") | 5 (3 too many steps, 1 message cap, 1 wrong template) |

---

## 8. Handover — backend team

### Assigning accounts to workflows (not built yet)
Accounts should follow the **Active** workflow of their portfolio whose segment they match. Nothing assigns accounts today — not for AI or hand-made workflows (`account.current_workflow` is never set).

The matching logic already exists and is tested:

```python
from app.modules.payflow.services.workflow_suggestion.segmenter import (
    filter_eligible, match_account_to_segment,
)

active = [w for w in portfolio_workflows if w["status"] == "Active"]   # serialized workflows
eligible, _ = filter_eligible(accounts)
for account in eligible:
    workflow = match_account_to_segment(account, active)   # most specific match, or None
```

- Works for AI and hand-made workflows (missing segment keys = "All").
- Most specific segment wins; segments built by the segmenter never tie.
- **Recommendation:** add `current_workflow_id` (FK → `payflow_strategies.id`) to accounts — names are not unique and change when edited. Keep `current_workflow` (name) for the existing UI and filters.
- Re-run assignment after each daily import and when a workflow is activated / deactivated.

### Rules at run time (not built yet)
Most rules (e.g. Balance > 10,000 → Human Review, Dispute → Hold) can only be checked while a workflow runs. A future workflow engine should check the **current** Active rules before each step. Each AI workflow lists its governing rules in `ai_context` ("Governed by").

### Long requests
`POST /payflow/workflows/suggest` takes 1–5 minutes on the free tier (~20–40 s expected on a paid tier). Typical proxies / load balancers cut requests at 30–100 s. **Recommendation:** run it as a background job (`POST` returns a job id → poll for the result). A DB job table + FastAPI background tasks is enough to start; Celery + Redis when running several servers.

---

## 9. Handover — frontend team

- **Preview segments** button on a portfolio → show segment labels and stats (fast).
- **Suggest workflows** button → long-running; show progress and do not time out. Handle `502` (show `detail`; nothing was saved) and `400` (no eligible accounts).
- New workflows appear in the normal workflows list with status **AI Proposed** and `source = "AI Generated"`.
- Worth showing on workflow detail: the segment ("Who it applies to" — `segment`), `cases_covered`, the AI's **reasoning** lines ("Why it fits", "Channels", "Timing", "Escalation", "Changes made") and **"Governed by"** lines (`ai_context`), and the AI original vs edited version (`ai_proposal_snapshot`).
- `segment` has two extra keys the dropdowns don't know (`situation`, `contact_state`) — show them as text or ignore them; do not drop them when saving edits.

---

## 10. Known issues & recommendations

| # | Issue | Recommendation |
|---|---|---|
| 1 | With 3 segments per call the AI reply can be **cut off** (Groq's default reply limit is 3,072 tokens) → `502` | Set `max_completion_tokens` in `call_llm` (~4,500 on the free tier, higher on paid). Until then use `GROQ_SEGMENTS_PER_CALL=2` |
| 2 | ~~Most retries were the AI using 13–15 steps (max 12)~~ | **Done:** `MAX_STEPS` raised to 15, plus branching guidance and a branching example. Live run: real two-path branches went from 5% to 37% of branches. New small one: the AI sometimes writes balance `5000` instead of `5,000` — normalise numbers in the validator to save a retry |
| 3 | Retry budget (2 fixes) was sometimes fully used — one live run failed with `502` ("no Wait between messages" not fixed in 2 tries) | Allow 3, or fix #2 |
| 4 | Free tier: 1–5 min per request, ~40k tokens per portfolio | Development only. Paid tier + `GROQ_SEGMENTS_PER_CALL=0` for customers |
| 5 | Request too long for normal HTTPS limits | Background job (see [§8](#long-requests)) |
| 6 | Pre-existing (not this feature): email logging prints `→`, which crashes when the server's output is redirected on Windows (cp1252) | Set `PYTHONIOENCODING=utf-8`, or remove the special character from the log message |
