# Changes for the Backend Team

Changes the AI team made that touch backend code or APIs. **Newest first.**
#1–#5: ticket **"[AI] Capture Workflow Review Feedback & Support Proposal Refinement"**. #6: AI-written email / SMS text.
Everything is backward compatible unless marked **Behaviour change**.

| # | Date | Change | Type | Do you need to do anything? |
|---|---|---|---|---|
| 7 | 2026-10-28 | AI workflows no longer SMS-only when email consent is unknown | Fix; new segment stat + one new `ai_context` line | Optional: map a real consent column on import |
| 6 | 2026-10-28 | AI writes the email / SMS text of workflows | New optional step fields, new API, one new `ai_context` line | No. Read "If you change…" below |
| 5 | 2026-10-15 | Suggest learns from the client's past reviews | AI code only, one new `ai_context` line | No |
| 4 | 2026-10-15 | New column `revision_of_id` on workflows | New nullable column + response field | No (added automatically on startup) |
| 3 | 2026-10-15 | Regenerate sends the review history to the AI; reject also saves `reviewer_note` | New optional field | No |
| 2 | 2026-10-15 | "Human Modified" only on real changes | **Behaviour change** | Be aware |
| 1 | 2026-10-15 | Reviewer notes on approve and edit | New optional fields | No. Use them when ready |

---

## 7. AI workflows no longer SMS-only when email consent is unknown

**What you saw:** Suggest always gave SMS-centric workflows with no email.
**Why:** the UAT account files have **no email-consent column**, so import leaves `email_consent` empty for every account. Our segment stat `email_consent_pct` counted only a recorded **Yes** → **0%** for every segment, and the AI followed its rule "low email consent = favour SMS". But the worker only blocks email on a recorded **No** (`email_consent is False`) — these customers *can* get email.

**Fix (AI-team code):**
- New segment stat **`email_allowed_pct`** = has an email address **and** consent is not a recorded No (same rule as the worker). The AI now sees this instead of `email_consent_pct`.
- `GET …/portfolios/{id}/segments`: `stats` gets `email_allowed_pct`; `email_consent_pct` is **kept** (additive).
- AI workflows get one more `ai_context` line, after "Email consent":
  ```json
  { "label": "Email allowed", "value": "100%" }
  ```
- Live check (real AI, same data): before → 2 of 2 workflows SMS-only ("Email consent is 0, so we rely on SMS"); after → both start with email, SMS as follow-up / fallback; control with 90% recorded **No** → still SMS-only.

**Optional for you:** if your CRM has a real consent field, map it on import (`email_consent` / `contact_permission`) — then recorded **No**s are respected by both the worker and the AI.

---

## 6. AI writes the email / SMS text of workflows

Communication steps can now hold **their own text**. The AI writes it (one call per workflow), code decides each message's **tone** and checks the text.
**No worker change:** `_send_communication` already sends `config.subject` / `config.body` when present (variables filled per account by `_render`) and falls back to the template when not.

### New optional fields on a step's `config` (`StrategyNodeConfig`)

```json
"config": {
  "channel": "Email",
  "purpose": "Payment Reminder",
  "template_id": "em-reminder",
  "subject": "Friendly reminder about your balance",
  "body": "Hi {{first_name}}, ... {{payment_link}} ... {{brand_name}}",
  "tone": "Friendly"
}
```
- Returned in every workflow response (`null` when empty). **Before this change the API silently dropped them**, so text could never be saved.
- Blank (`""` / spaces) = no text → the template is sent. Max: subject 255, body 5,000, tone 32 characters (longer → `422`).
- `tone`: `Friendly`, `Clear`, `Firm`, `Final notice`, `Supportive` or `Neutral` — shown to reviewers, not used by the worker.

### New API: `POST /payflow/workflows/{id}/content`

| Body | What happens |
|---|---|
| none or `{}` | AI writes every email / SMS of the workflow (improves the current text if there is some) |
| `{"step_id": "s5"}` | Rewrites only that message; the others are context |
| `{"step_id": "s5", "remarks": "softer"}` / `{"remarks": "shorter"}` | Same, following the reviewer's remarks |

- Permission: Create / Edit Workflows. Only **Draft, AI Proposed, Under Review** → otherwise `400` (before any AI call). Unknown / non-message `step_id` → `400`. AI still invalid after 2 fixes → `502`, **nothing saved**.
- Response: the workflow (`PayflowStrategyItem`).

### `strategy_service.py` (additive)
- **New `save_ai_messages(user, strategy_id, steps, remarks=None)`** — saves AI-written text. **Status and origin stay as they are** (not "Human Modified": the AI wrote it). While `origin` is still AI Proposed, the text is also copied into `ai_proposal_snapshot`. History entry: `note = "Message text written by AI for <user>: <step titles> (remarks: …)"` with **`changes: []`** on purpose, so Suggest lessons and Regenerate history do not read the AI's own writing as a reviewer edit.
- `_diff_steps`: new change line `"<step title>: message text edited"` when a reviewer edits text through `/update`. Like any step change, that marks an AI workflow "Human Modified" (see #2).

### Suggest and Regenerate
- They write the text **before saving** (one extra AI call per workflow), so each workflow is saved once, with its text.
- Writing **never blocks a save**: on failure the workflow is saved without text (templates are sent) and gets one `ai_context` line:
  ```json
  { "label": "Messages", "value": "Not written, so the templates will be sent. Use Write messages to try again. (…)" }
  ```
- Your background-job Suggest (`ai_suggest_job_service.py`) calls `suggest_workflows`, so it gets this automatically.
- Message text is **kept out of the workflow-design prompts** (`compact_step` drops it), so those prompts did not grow.

### If you change…
| You change | Please also |
|---|---|
| Add a variable to `_render` (worker) | Add it to `VARIABLES` in `workflow_suggestion/message_validator.py`, otherwise the AI is not allowed to use it |
| Rename the client payment settings `allow_pay_in_full` / `allow_partial` / `allow_payment_plan` | Tell us — `load_payment_options` (`workflow_suggestion/context.py`) reads them via `PayflowPaymentPortalService._client_settings`, so messages only offer what the payment page offers |

### Tests
`tests/conftest.py` has a new **autouse** fixture `message_ai_off`: in tests the message writer's AI is always "unavailable" unless a test fakes it. Without it, every Suggest / Regenerate test would call the real Groq API (tests read `GROQ_API_KEY` from `.env`).

---

## 5. Suggest learns from the client's past reviews

**No change to your files, the API, request bodies or the database.** New AI-team file: `workflow_suggestion/learning.py`.

- Suggest now shows the AI up to 2 of the **same client's** Approved / Active workflows **for similar segments** (same situation) as examples, otherwise the generic ones, plus short "lessons" from its reviewers' notes (edits, rejects, approvals), similar segments first. They are guidance for the AI, not rules.
- Each new AI workflow gets one extra `ai_context` line:
  ```json
  { "label": "Learned from", "value": "examples #8, #12; 6 reviewer lessons" }
  ```
- Your background-job Suggest (`ai_suggest_job_service.py`) calls `suggest_workflows`, so it gets this automatically.
- Clients with no reviewed workflows: no change at all (same prompt as before).

---

## 4. New column `revision_of_id` on workflows

`payflow_strategies.revision_of_id` (integer, nullable): the workflow this one was **regenerated from**.

- **Set only by Regenerate.** Manual create and every other API leave it `null`. Request bodies are unchanged.
- **Returned** in every workflow response (`GET /payflow/workflows/{id}`, list, etc.):
  ```json
  { "id": 9, "revision_of_id": 8, "...": "..." }
  ```
- **No foreign key on purpose:** deleting the parent workflow never fails or touches the child. The id is simply left pointing at a deleted workflow.
- **Database:** added automatically by the startup schema sync (`ALTER TABLE … ADD COLUMN`). No manual migration. Existing rows get `null`.
- **Why:** Regenerate follows the chain back (#10 → #9 → #8) to send all earlier reviewer feedback to the AI, same client only. The "Revision of #8" text in `ai_context` is still kept for display.

---

## 3. Regenerate sends the review history to the AI; reject also saves `reviewer_note`

### Regenerate (AI-team code, no API change)
`POST /payflow/workflows/{id}/regenerate` now also sends the AI the workflow's **review history**: what reviewers changed, their reasons and reject notes (last 10 entries), so it does not undo what reviewers asked for. Request and response are unchanged.

### Reject: `POST /payflow/workflows/{id}/reject`
**Request body unchanged** (`{"note": "…"}` or `{}`). When a note is given, the history entry gets one extra key. `note` stays exactly as before:

```json
{ "note": "Too aggressive for this segment", "reviewer_note": "Too aggressive for this segment", "changes": [] }
```

**Why:** approve, edit and reject now all keep the reviewer's reason under the same key, which Regenerate reads.
No note → no `reviewer_note` (same as before).

---

## 2. "Human Modified" only on real changes (**Behaviour change**)

An AI workflow's `origin` becomes `"Human Modified"` only when its **steps** or **segment** really change.

| Action on an AI workflow | Before | Now |
|---|---|---|
| Save Draft | Human Modified | stays AI Proposed |
| Rename / edit summary | Human Modified | stays AI Proposed |
| Send the same steps back unchanged | Human Modified | stays AI Proposed |
| Change a step | Human Modified | Human Modified |
| Change the segment | Human Modified | Human Modified |

**Why:** now an approved workflow shows how it was approved.
- `origin: "AI Proposed"` → approved as the AI wrote it
- `origin: "Human Modified"` → approved after changes

**Not changed:** status flow (any edit still moves AI Proposed → Under Review), history, `ai_proposal_snapshot`, approve, reject, and hand-made workflows.
**Existing rows are not backfilled.** Workflows already marked "Human Modified" stay as they are.

---

## 1. Reviewer notes on approve and edit

Reviewers can say **why** they approved or edited a workflow. Both notes are optional.

### Approve: `POST /payflow/workflows/{id}/approve`

```jsonc
// Before: no body
// Now (optional): no body still works
{ "note": "Gentle tone fits early-stage customers" }
```

### Edit: `POST /payflow/workflows/{id}/update`

```jsonc
// Same body as before, plus one optional field:
{
  "steps": [ ... ],
  "note": "SMS was too early for this segment"
}
```

### Where the note appears

It is stored in the workflow's `versions` history as `reviewer_note`, next to the automatic change list:

```json
"versions": [
  {
    "version": 1,
    "note": "Updated by Sara",
    "changes": ["Wait 3 days: timing changed from 3 Days After Previous Email to 7 Days After Previous Email"],
    "reviewer_note": "SMS was too early for this segment"
  }
]
```

### Rules
- Optional, max **2,000** characters (longer → `422`).
- Trimmed. A blank note counts as no note.
- An edit that sends a note but changes no steps (e.g. only the summary) **still gets a history entry**, so the note is not lost.
- **Reject's request is unchanged** (see #3 for the added key).

---

## Files changed

| File | Changes |
|---|---|
| `app/infrastructure/database/models/__init__.py` | #4 (`revision_of_id` column) |
| `app/modules/payflow/services/strategy_service.py` | #1, #2, #3 (reject `reviewer_note`), #4, #6 (`save_ai_messages`, "message text edited" change line) |
| `app/modules/payflow/routes.py` | #1 (approve accepts an optional body), #6 (`POST /workflows/{id}/content`) |
| `app/modules/payflow/schemas.py` | #1 (`ApproveStrategyRequest`, `note` on update, `reviewer_note` on versions), #4 (`revision_of_id`), #6 (`subject` / `body` / `tone` on `StrategyNodeConfig`, `WriteWorkflowMessagesRequest`), #7 (`email_allowed_pct` on `PortfolioSegmentStats`) |
| `app/modules/payflow/services/workflow_suggestion/` | #3 (review history in Regenerate), #4 (following the chain), #5 (`learning.py`), #6 (`message_tone.py`, `message_prompt.py`, `message_validator.py`, `message_writer.py`; `load_payment_options`; Suggest / Regenerate write text), #7 (`segmenter.py` stat, `prompt.py`, `service.py` "Email allowed" line) — AI-team code |
| `tests/conftest.py` | #6 (`message_ai_off` fixture) |

## Tests
Full suite: `venv\Scripts\python.exe -m pytest` (208 passing on 2026-10-28).
3 older tests fail since AI-team files were changed on 15–23 Oct (`load_rules` only uses assigned system rules, `_lessons` signature, hand-made workflows can be regenerated) — waiting for confirmation that these changes are intended before the tests are updated.
New tests: `tests/test_workflow_review_notes.py`, `tests/test_workflow_human_modified.py`, `tests/test_workflow_learning.py`, plus additions to `tests/test_workflow_regenerate.py` and `tests/test_workflow_prompt_llm.py`.
#6: `tests/test_workflow_message_text.py`, `test_workflow_message_tone.py`, `test_workflow_message_validator.py`, `test_workflow_message_writer.py`, `test_workflow_message_api.py`, `test_workflow_message_auto.py`. #7: `tests/test_workflow_email_allowed.py`.
