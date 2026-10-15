# Changes for the Backend Team

Changes the AI team made for the ticket **"[AI] Capture Workflow Review Feedback & Support Proposal Refinement"**. **Newest first.**
Everything is backward compatible unless marked **Behaviour change**.

| # | Date | Change | Type | Do you need to do anything? |
|---|---|---|---|---|
| 5 | 2026-10-15 | Suggest learns from the client's past reviews | AI code only, one new `ai_context` line | No |
| 4 | 2026-10-15 | New column `revision_of_id` on workflows | New nullable column + response field | No (added automatically on startup) |
| 3 | 2026-10-15 | Regenerate sends the review history to the AI; reject also saves `reviewer_note` | New optional field | No |
| 2 | 2026-10-15 | "Human Modified" only on real changes | **Behaviour change** | Be aware |
| 1 | 2026-10-15 | Reviewer notes on approve and edit | New optional fields | No. Use them when ready |

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
| `app/modules/payflow/services/strategy_service.py` | #1, #2, #3 (reject `reviewer_note`), #4 |
| `app/modules/payflow/routes.py` | #1 (approve accepts an optional body) |
| `app/modules/payflow/schemas.py` | #1 (`ApproveStrategyRequest`, `note` on update, `reviewer_note` on versions), #4 (`revision_of_id`) |
| `app/modules/payflow/services/workflow_suggestion/` | #3 (review history in Regenerate), #4 (following the chain), #5 (`learning.py`) — AI-team code |

## Tests
Full suite: `venv\Scripts\python.exe -m pytest` (147 passing).
New tests: `tests/test_workflow_review_notes.py`, `tests/test_workflow_human_modified.py`, `tests/test_workflow_learning.py`, plus additions to `tests/test_workflow_regenerate.py` and `tests/test_workflow_prompt_llm.py`.
