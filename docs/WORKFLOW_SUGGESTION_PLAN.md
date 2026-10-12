# AI Workflow Suggestion — Plan

Suggest collection workflows for a portfolio using an LLM, so every account gets a workflow that fits its situation, while always following system rules, client rules and the allowed workflow options.

> **Status:** segments A–F and H done; G handed to the backend team. The user-facing guide is [`AI_WORKFLOW_SUGGESTION.md`](AI_WORKFLOW_SUGGESTION.md) — this file is the build plan and history.
>
> **Revision 2** — updated after the workflow rework on `develop` (commits `db93cef`, `491314a`, `4b29440`). See [§8 What changed in revision 2](#8-what-changed-in-revision-2).

---

## 1. Problem statement

### The data we have

```
CLIENT          the company PayFlow collects for (e.g. Northstar Utilities)
  │             owns the rules: client type, channels, governance rules, AI mode
  │
  └── PORTFOLIO / SUB-CLIENT     a group of the client's debts (e.g. Residential)
        │                        every account MUST belong to one
        │                        every workflow MUST belong to one
        │
        └── ACCOUNT              one person who owes money = one case
                                 arrives daily from the CRM file, with history
```

Accounts arrive in **different situations**, not fresh:

| Situation | Example |
|---|---|
| Never contacted | Owes $240, 12 days late, no contact yet |
| Contacted, nothing paid | Emailed twice, still owes everything |
| Partly paid | Paid $1,150 of $5,400 |
| Promise to Pay | Promised $500 by the 20th |
| Payment Plan | Paying monthly instalments |
| Resolved / Closed / On hold | Needs no workflow |

### The problem

- Workflows are created by hand only. Nothing links an account to a workflow (`account.next_action = "Assign workflow"` is never acted on).
- Each account needs treatment that fits its situation.
- **One workflow per account** → thousands of LLM calls daily, thousands of approvals. Not workable.
- **One workflow per portfolio** → too generic.
- Workflows must always respect **system rules**, **client rules**, and the **allowed workflow options**. An LLM alone cannot guarantee that.

---

## 2. Solution in one line

**Code groups the portfolio's accounts into a few segments → the LLM designs one workflow per segment → code validates it → a human reviews, approves and activates → code assigns every account to its segment's workflow.**

```
Portfolio accounts
      │
      ▼
 [1] SEGMENT (code)  ──►  3–6 segments, no overlaps, no gaps
      │
      ▼
 [2] SUGGEST (LLM)   ──►  one workflow (flowchart) per segment
      │
      ▼
 [3] VALIDATE (code) ──►  catalog + rules + limits enforced; retry on failure
      │
      ▼
 [4] SAVE            ──►  existing create API, source "AI Generated" → status "AI Proposed"
      │
      ▼
 [5] HUMAN           ──►  existing APIs: review → approve → activate → "Active"
      │
      ▼
 [6] ASSIGN (code)   ──►  each account → matching Active workflow (after every import)
```

**Key decisions**

| Decision | Choice | Why |
|---|---|---|
| Unit of suggestion | Portfolio, split into segments | Cheap, consistent, approvable |
| Who builds segments | Code, not the LLM | No overlaps/gaps, repeatable |
| Who designs workflow steps | LLM | That is where judgement helps |
| How rules are enforced | Prompt (guidance) **and** validator (guarantee) | LLM can ignore instructions |
| Allowed values source | The existing workflow **catalog** (`_catalog()` in `strategy_service.py`) | Already used by the UI; one source of truth |
| Segment bands | The catalog's official bands (same as the UI dropdowns) | Segments show correctly in the UI |
| Examples in prompt | 1–2 short ones, stored in a file | Better quality; not required for format |
| Segmentation attributes | Operational & geographic only | Matches the UI "Who it applies to" panel |

---

## 3. The workflow system we build on (current `develop`)

### Workflow lifecycle

```
Human created:  Draft ──submit-review──► Under Review ──approve──► Approved ──activate──► Active ⇄ Inactive
AI generated:   AI Proposed ──begin-review / edit──► Under Review ──► (same as above)
Reject:         AI workflow → back to AI Proposed ("regeneration requested")
                Human workflow → back to Draft
```

- **Approve gives "Approved", not "Active".** Activate is a separate step.
- Creating with `source: "AI Generated"` (or `status: "AI Proposed"`) automatically sets `status = AI Proposed`, `origin = AI Proposed`, `source = AI Generated`, and stores `ai_proposal_snapshot` (the AI's original, kept after human edits).
- Editing an AI workflow marks it **Human Modified**; the version history records what changed.

### Workflow shape (steps are a flowchart)

```json
{
  "name": "...", "client_id": 1, "portfolio_id": 1,
  "summary": "...", "cases_covered": 640,
  "segment": { "delinquency": "1 – 29 days past due", "balance_band": "Under $500", "...": "..." },
  "entry_node_id": "s1",
  "steps": [
    { "id": "s1", "kind": "Trigger", "title": "Case received",
      "config": { "reference_event": "Case Received" }, "next": "s2" },
    { "id": "s2", "kind": "Communication", "title": "Friendly reminder",
      "config": { "channel": "Email", "purpose": "Payment Reminder", "template_id": "em-reminder",
                  "reference_event": "Due Date", "amount": 3, "unit": "Days", "direction": "After" },
      "next": "s3" },
    { "id": "s3", "kind": "Condition", "title": "Paid?",
      "config": { "attribute": "Payment Status", "operator": "Equals", "value": "Paid In Full" },
      "yes": "s4", "no": "s5" }
  ],
  "ai_context": [{ "label": "...", "value": "..." }],
  "source": "AI Generated"
}
```

- Each step: `id`, `kind`, `title`, `config {…}`, and `next` — or `yes` / `no` for Condition.
- Timing must use an **event** ("3 Days After Due Date"), never "Day 3".

### Catalog of allowed values (`_catalog()` in `strategy_service.py`, also returned by `GET /payflow/workflows` as `catalog`)

| Group | Values (summary) |
|---|---|
| Step kinds | Trigger, Communication, Wait, Condition, Payment Action, Case Action, Human Review, AI Reassessment, Outcome |
| Channels | **Email, SMS** (no WhatsApp) |
| Message purposes | Payment Reminder, Firm Reminder, Payment Link, Promise-to-Pay Reminder, Payment Plan Reminder, Contact Details Update Request |
| Reference events / units / directions | Case Received, Due Date, Previous Action, … · Hours/Days/Weeks · After/Before |
| Condition attributes / operators / values | Payment Status, Email Delivery, … · Equals/Not Equals/Greater Than/Less Than · per-attribute values |
| Case actions / Payment actions / Outcomes | fixed lists |
| Message templates | id, name, channel, purpose, subject, body |
| Segment bands | age, postal region, balance, delinquency, language, tenure (+ "All …" for each) |
| Excluded from targeting | Ethnicity, Religion, Gender, Health status, Marital status |

> ⚠️ The old enums `PayflowCommChannel` (still has WhatsApp) and `PayflowCommPurpose` **do not match** the catalog. Always use the catalog.

> ⚠️ The backend only checks step **kind**, step ids and links. It does **not** check config values (channel, purpose, action, outcome, template…) against the catalog. Our validator must.

---

## 4. Stages of the solution

### Stage 1 — Config

`app/modules/payflow/services/workflow_suggestion/config.py` holds **only what the catalog does not**:

- **Band ranges** — numeric meaning of each catalog band label, e.g. `"30 – 59 days past due" → (30, 59)`, `"$500 – $1,500" → (500, 1500)`, `"Quebec (H, J, G)" → postal prefixes H/J/G`. Labels themselves are imported from `strategy_service.py`, never retyped.
- **Limits**: `MIN_STEPS`, `MAX_STEPS`, `MAX_COMMUNICATIONS`, entry step must be `Trigger`, every path ends in `Outcome`, a timing gap between two communications.
- **Segment sizes**: `MIN_SEGMENT_SIZE` (default 20), `MAX_SEGMENTS` (default 8 = Promise to Pay + Payment Plan + up to 6 Active).
- **Never used for segmentation**: the catalog's excluded list, plus `employment_status`, `income_band`, `education_level`, `customer_segment`.

### Stage 2 — Load context

- **Client**: type, enabled channels (`channel_email`, `channel_sms`), `governance_rules`, `ai_mode`, language, currency.
- **Rules**: active `payflow_rules` where `rule_type = "System Rule"`, plus active `"Client Rule"` for this client.
- **Accounts**: all accounts of the portfolio.
- **Catalog**: `_catalog()` from `strategy_service.py`.

### Stage 3 — Segment (code only)

1. **Filter** — drop `CLOSED`, `Resolved`, `provincial_hold = true`, `Human Review`.
2. **Label** each account:

   | Label | From | Values |
   |---|---|---|
   | Situation | `collection_status` | Active / Promise to Pay / Payment Plan |
   | Contact state | contact dates, `recovered_balance` | never_contacted / contacted_no_payment / partly_paid |
   | Delinquency band | `days_past_due` | catalog `delinquency_bands` |
   | Balance band | `outstanding_balance` | catalog `balance_bands` |

   Promise to Pay and Payment Plan are **one bucket each** (not split further).
   Age, region, language, tenure stay **"All …"** unless a rule needs them.
3. **Group** — key = `(situation, delinquency, contact_state, balance_band)`.
4. **Merge** — the UI dropdowns hold **one value each**, so a merge **widens a field to "All …"** (not to a custom range like "1–59"):
   - while a bucket < `MIN_SEGMENT_SIZE`: set its balance band to "All balances" and merge with siblings → then contact state to "any" → then delinquency to "All stages". Delinquency is kept longest (it matters most for collections).
   - Never merge across situations. Over `MAX_SEGMENTS` → raise the minimum size 20% at a time and merge again until it fits.
5. **Describe** — each bucket becomes:

```json
{
  "segment_id": "A",
  "label": "Active · 1 – 29 days past due · Never contacted · Under $500",
  "segment": {
    "age_band": "All ages", "postal_region": "All regions",
    "balance_band": "Under $500", "delinquency": "1 – 29 days past due",
    "language": "All languages", "tenure": "All customers",
    "situation": "Active", "contact_state": "never_contacted"
  },
  "stats": { "accounts": 640, "total_owed": 182000, "avg_dpd": 14,
             "email_consent_pct": 92, "languages": { "EN": 80, "FR": 20 } }
}
```

`segment` = saved on the workflow and used for assignment (catalog labels → UI dropdowns work). `stats` = context for the LLM.

### How rules are handled (decided)

Rules are IF/THEN logic, so **code handles them, not the AI**. Rules stay a **separate layer** and are never copied into workflows (copies would go stale when a rule changes).

| Kind of rule | Example | Handled by |
|---|---|---|
| Limits the workflow's shape | Quiet Period: ≥ 4 messages in 7 days → Prevent Communication | `rules.py` turns it into a design limit ("max 3 messages per 7 days") → one line in the prompt + checked by the validator |
| Everything else | Balance > 10,000 → Human Review; dispute → Hold | Listed on the saved workflow as "governed by" for reviewers; enforced while workflows run (future run-time check — outside this feature) |

New rules built from existing fields/actions need no code change. A rule the code cannot turn into a design limit is automatically "run time".

### Stage 4 — Prompt (`prompt.py`) ✅

- **System prompt**: role, flowchart rules, step kinds + config, catalog values (only the client's enabled channels and their templates), design limits, strategy guidance. **No rule text.**
- **User prompt**: client summary + segments (label + stats), compact JSON.
- **Examples**: 2 short flowchart workflows in `prompts/workflow_examples.json`, marked "illustrative only" (a test keeps them valid against the catalog).
- **Path ends**: a step without `next` ends its path; only Outcome, Human Review or Case Action may end a path (Human Review / Case Action hand over to a person or treatment).

### Stage 5 — LLM call (`llm.py`) ✅

- Groq `openai/gpt-oss-120b` over `httpx`, `reasoning_effort = low` (cuts "thinking" from ~2,900 to ~50 tokens).
- JSON schema built from the catalog (enums for kind, channel, purpose, template, events, actions, outcomes; `segment_id` limited to the batch's ids). Unused config fields and links are **left out** (non-strict schema) to keep replies short.
- On 429 (rate limit) waits as told by `retry-after` and retries (max 5). Other failures → `ServiceUnavailableError` (→ 502).
- **Free-tier batching**: the free tier allows 8,000 tokens/min, so `generate_workflows` sends `GROQ_SEGMENTS_PER_CALL` (default 3) segments per call. Measured: ~3,100 prompt + ~750 per workflow; 8 segments in ~76 s. **To remove** on a paid tier: set `GROQ_SEGMENTS_PER_CALL=0` (one call) or delete the marked loop.

Output shape:

```json
{
  "workflows": [
    {
      "segment_id": "A", "name": "string", "summary": "string", "rationale": "string",
      "entry_node_id": "s1",
      "steps": [
        { "id": "s1", "kind": "Trigger", "title": "Case received",
          "config": { "reference_event": "Case Received" }, "next": "s2" }
      ]
    }
  ]
}
```

### Stage 6 — Validate (code)

Returns a list of human-readable errors. Checks only what can be checked on a design:

- **Structure**: one workflow per segment_id, no unknown segment_ids.
- **Flowchart**: unique ids; `entry_node_id` exists and is a Trigger; all `next`/`yes`/`no` point to real steps; Condition has `yes` and `no`; every step reachable; paths end only in Outcome / Human Review / Case Action; no loops.
- **Catalog**: kind, channel, purpose, template (template channel + purpose match the step), reference event, unit, direction, condition attribute/operator/value, case/payment action, outcome.
- **Step config**: Communication has channel + purpose + template + timing; Wait has timing; Condition has attribute/operator/value; actions/outcome set.
- **Limits**: step count, communication count, a Wait between two Communications.
- **Design limits from rules + client**: channel enabled for the client; max messages per 7 days.

On errors → send the AI its own failing workflows + the error list, retry (max 2 retries = 3 calls per batch). Still failing → 502 with the errors, save nothing.

### Stage 7 — Save

Reuse the **existing** `PayflowStrategyService.create_strategy` — no change needed:
- `source = "AI Generated"` → status/origin "AI Proposed" + `ai_proposal_snapshot` set automatically.
- `portfolio_id` (required), `segment`, `entry_node_id`, `steps`, `summary`.
- `cases_covered` = segment account count.
- `ai_context` = key segment stats + LLM rationale + **"Governed by"** lines (active system/client rules and client governance toggles), as `{label, value}`.
- Supervisors are notified automatically (existing behaviour).

Human then uses existing APIs: `begin-review` / `update` → `approve` (Approved) → `activate` (Active), or `reject`.

### Stage 8 — Assign accounts (code)

For each eligible account in the portfolio: find the **Active** workflow (same portfolio) whose `segment` matches → set `current_workflow` and `next_action`. Most specific match wins (a band beats "All …"). No match → leave `next_action = "Assign workflow"`. Runs via API and automatically after each account import.

---

## 5. APIs

| # | API | Type | Required |
|---|---|---|---|
| 1 | `POST /payflow/workflows/suggest` `{client_id, portfolio_id}` | Action, calls LLM | ✅ |
| 2 | `POST /payflow/workflows/assign` `{portfolio_id}` | Action, no AI | ✅ |
| 3 | `GET /payflow/clients/{cid}/portfolios/{pid}/segments` | Read, no AI (preview) | Optional |
| 4 | `POST /payflow/workflows/{id}/regenerate` | Action, calls LLM for one rejected AI workflow | Optional, later |

- The catalog needs **no new API** — `GET /payflow/workflows` already returns it.
- Review / approve / activate / reject use the **existing** workflow APIs.
- Permissions: `create_edit_workflows` per client (same `_assert_manage` check as create); preview uses `view_workflows`.
- Style: thin route, `try / except AppError → _map_error`, POST for actions.

---

## 6. Files

| File | Contents |
|---|---|
| `app/modules/payflow/services/workflow_suggestion/config.py` | Band meanings, limits, segment sizes (Stage 1) |
| `app/modules/payflow/services/workflow_suggestion/__init__.py` | Package |
| `.../workflow_suggestion/context.py` | `load_client_context`, `load_rules`, `load_portfolio_accounts` |
| `.../workflow_suggestion/segmenter.py` | `filter_eligible`, `label_account`, `group_accounts`, `merge_small_buckets`, `describe_segment`, `build_segments`, `match_account_to_segment` |
| `.../workflow_suggestion/prompt.py` | `build_system_prompt`, `build_user_prompt`, `load_examples` |
| `.../workflow_suggestion/llm.py` | `build_output_schema` (from catalog), `call_llm` |
| `.../workflow_suggestion/validator.py` | `validate_structure`, `validate_flowchart`, `validate_catalog_values`, `validate_client_rules`, `validate_system_rules`, `validate_all` |
| `.../workflow_suggestion/service.py` | `WorkflowSuggestionService`: `preview_segments`, `suggest_workflows`, `save_suggestions`, `assign_accounts` |
| `.../workflow_suggestion/prompts/workflow_examples.json` | 1–2 example flowchart workflows |
| `app/modules/payflow/schemas.py` | Request/response models |
| `app/modules/payflow/routes.py` | New routes |
| `tests/test_workflow_segmenter.py`, `tests/test_workflow_validator.py`, `tests/test_workflow_suggest.py` | Tests (LLM mocked) |

**Changes to existing code**

- `import_service.process_account_file` — call `assign_accounts` after a successful run (a failure there must not fail the import).
- `app/core/config.py` + `.env.example` — `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_TIMEOUT_SECONDS`.
- `requirements.txt` — no change (Groq is called with `httpx`, already a dependency).
- **No change** to `create_strategy` (already supports AI-generated workflows).

---

## 7. Open decisions (confirm before/while building)

1. ~~LLM provider and model~~ — **decided**: Groq, `openai/gpt-oss-120b` (free tier for now).
2. **Band boundaries** — confirm the catalog bands are the ones to use (e.g. is $500 "Under $500" or "$500 – $1,500"?).
3. **Customer tenure** — no account field. Keep "All customers", or derive from `date_listed`?
4. **Postal region** — derive from first letter of `postal_code`; fall back to `province_state`?
5. **`MIN_SEGMENT_SIZE`, `MAX_SEGMENTS`, `MIN_STEPS`, `MAX_STEPS`, `MAX_COMMUNICATIONS`.**
6. **Re-running suggest** for a portfolio — what happens to its existing AI Proposed workflows (keep, replace, or reject)?
7. ~~Where to store `situation` and `contact_state`~~ — **decided**: extra keys inside `segment` (UI ignores them; assignment uses them).
8. ~~What counts as "contacted"~~ — **decided**: any of `last_contact_date` / `last_email_sent_date` / `last_sms_sent_date` is set. Any recovered amount → "partly paid" (checked first).

---

## 8. What changed in revision 2

| Area | Before | Now |
|---|---|---|
| Lifecycle | approve → Active | approve → **Approved** → activate → **Active** |
| Saving AI workflows | needed a change to `create_strategy` | **already supported** via `source: "AI Generated"` |
| Portfolio | optional, client-level fallback | **required** |
| Steps | flat list | **flowchart** (`config`, `next`, `yes`/`no`, `entry_node_id`, event timing) |
| Allowed values | from enums | from the **catalog** (enums are out of date) |
| Channels | Email, SMS, WhatsApp | **Email, SMS** |
| Bands | invented by us | **official catalog bands** (UI dropdowns) |
| Merging segments | widen to a custom range | widen a field to **"All …"** (dropdowns are single-value) |
| Config API | planned | **dropped** — `GET /workflows` returns the catalog |
| Validator | basic | also checks **flowchart** and **every config value** |
| Regenerate | — | optional later API (reject sends AI workflows back to AI Proposed) |

---

## 9. Step-by-step execution (for Claude)

Work on branch `ai_changes`. After each segment: run tests, show the result, wait for the go-ahead before the next segment. Match existing code style (see `strategy_service.py`, `routes.py`). Import catalog lists from `strategy_service.py` — never retype them.

**Ground rules**
- Do not break existing APIs or behaviour — the full test suite must keep passing.
- All new logic lives in `app/modules/payflow/services/workflow_suggestion/`. New routes go in the existing `routes.py`, in the same thin style.
- Keep code clean and simple — no extra layers or abstractions.
- Use the project's error handling: raise `AppError` subclasses (`ValidationAppError`, `NotFoundError`, `ForbiddenError`…) in services; routes map them with `_map_error`.

### Segment A — Foundations
1. Create `workflow_suggestion/config.py`: band label → meaning maps (keyed by the catalog labels), limits, segment sizes. Test that every catalog band label has a definition. ✅ done
2. Add `GROQ_*` settings to `app/core/config.py` and `.env.example`. ✅ done
3. **Check**: app starts, existing tests pass.

### Segment B — Context + Segmenter
1. `context.py`: load client, active system + client rules, portfolio accounts. ✅ done
2. `segmenter.py`: filter → label → group → merge (widen to "All …") → describe; `match_account_to_segment`. ✅ done
3. Unit tests with hand-built accounts: filtering, labelling, merge order, no overlaps/gaps, every eligible account in exactly one segment, determinism, all segment values are valid catalog labels. ✅ done (`tests/test_workflow_segmenter.py`, `tests/test_workflow_context.py`)
4. **Check**: segmenter tests pass. ✅ 39/39 tests pass

### Segment C — Preview API
1. `service.preview_segments(user, client_id, portfolio_id)` with access checks (`_assert_view` pattern). ✅ done
2. Route `GET /payflow/clients/{cid}/portfolios/{pid}/segments` + response schema. ✅ done
3. **Check**: live run on a temporary DB with 200 imported accounts → 200 OK, sensible segments, 404 for unknown portfolio. ✅ done (43/43 tests pass)

### Segment D — Prompt + LLM client ✅
1. `rules.py`: rules → design limits (enabled channels, max messages per 7 days). ✅
2. `prompt.py` + `prompts/workflow_examples.json` (flowchart format, valid catalog values, no rule text). ✅
3. `llm.py`: schema from the catalog; `call_llm` (low reasoning, rate-limit wait, clear errors); `generate_workflows` with free-tier batching. ✅
4. `ServiceUnavailableError` added to `app/core/exceptions.py`; `GROQ_REASONING_EFFORT`, `GROQ_SEGMENTS_PER_CALL` settings. ✅
5. **Check**: live run — 8 segments → 8 valid-looking workflows in ~76 s on the free tier; unit tests pass. ✅

### Segment E — Validator ✅
1. `validator.py`: `validate_all` → `validate_structure`, `validate_flowchart`, `validate_catalog_values`, `validate_step_config`, `validate_limits` → list of plain error messages. Internal step, no API of its own. ✅
2. Unit tests: the prompt examples pass; one failing case per check (`tests/test_workflow_validator.py`). ✅
3. **Check**: the 8 live AI workflows pass with 0 errors; an older cut-off reply is caught (3 broken links); SMS-off client flags every SMS step. ✅
4. Message spacing rule tightened to "a **Wait** between two Communications" (a Condition takes no time). ✅

### Segment F — Suggest API ✅
1. `service.suggest_workflows`: access check → context → segments → batches → AI → validate → retry → save. ✅
2. Retries are **per batch, failing segments only**: the AI gets its own workflow + the error list (max 2 retries). Still failing → `ServiceUnavailableError` → **502**, nothing saved. ✅
3. Batching moved into the service (`_batches`, clearly marked; `llm.py` is now a plain Groq client). ✅
4. `save` reuses the existing `create_strategy` with `source = "AI Generated"`, `segment`, `entry_node_id`, `steps`, `cases_covered`, `ai_context` (segment stats + AI rationale + "Governed by" rules). ✅
5. Route `POST /payflow/workflows/suggest` + schemas; `service_unavailable → 502` added to `_map_error`. No eligible accounts → 400. Re-running adds new AI Proposed workflows next to old ones. ✅
6. Tests with a fake AI: happy path, retry-then-pass, give-up (nothing saved), batching (3 per call → 2 calls; 0 → 1 call), AI down → 502, no eligible accounts → 400, no permission → 403. ✅
7. **Check**: live on a temporary DB — 200 imported accounts → 8 AI Proposed workflows in 78 s (real Groq, free tier); one taken through begin-review → approve → activate. ✅

### Segment G — Assignment → handed to the backend team
Decided: the AI team stops at creating workflows. Assigning accounts to **Active** workflows belongs to the backend team.
Ready for them: `match_account_to_segment()` in `segmenter.py` (tested), each workflow's stored `segment`, and the recommendation to add `current_workflow_id` to accounts. See `docs/AI_WORKFLOW_SUGGESTION.md` §8.

### Segment H — Docs ✅
1. `docs/AI_WORKFLOW_SUGGESTION.md`: overview, step-by-step flow, API reference with real examples, rules, settings, code map, testing, handover notes (backend / frontend), known issues. ✅
2. `docs/postman/PayFlow_AI_Workflows.postman_collection.json` for manual testing. ✅
3. Short pointers added to `ARCHITECTURE.md` and `docs/samples/README.md`. ✅
4. **Check**: full test suite passes (97/97). ✅

### Later (optional)
- `POST /payflow/workflows/{id}/regenerate` — regenerate one rejected AI workflow using the reviewer's note.
