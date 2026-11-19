# `replaced_by_id` and `revision_of_id`: how a rejected workflow links to its new version

## What happens on Reject / Regenerate

When a reviewer rejects a workflow and asks for a new version, the backend **keeps the old workflow** and **creates a new one** with a new id. The two point to each other:

```
 OLD workflow #18                          NEW workflow #37
 ┌───────────────────────────┐            ┌───────────────────────────┐
 │ status: "Rejected"        │            │ status: "AI Proposed"     │
 │ version: 1                │            │ version: 2  (old + 1)     │
 │ replaced_by_id: 37  ──────┼──────────► │ revision_of_id: 18        │
 │ revision_of_id: null      │ ◄──────────┼── (points back to old)    │
 └───────────────────────────┘            │ replaced_by_id: null      │
                                          └───────────────────────────┘
```

- **`replaced_by_id`** is on the **old** workflow. It is the id of its new version.
- **`revision_of_id`** is on the **new** workflow. It is the id of the workflow it replaced.

So `GET /payflow/workflows/{replaced_by_id}` returns the **new** workflow (#37), which is **not** rejected.

## The two workflows side by side

| | Old workflow (#18) | New workflow (#37) |
|---|---|---|
| `status` | `"Rejected"` | `"AI Proposed"` |
| `version` | unchanged (1) | old version + 1 (2) |
| `replaced_by_id` | `37` | `null` (until it is regenerated itself) |
| `revision_of_id` | unchanged | `18` |
| Shown in the workflows list | Yes, with the status filter "Rejected" | Yes, like any AI Proposed workflow |
| Can be opened / viewed | Yes | Yes |
| Can be edited, reviewed, approved, activated | **No** (read-only) | Yes, the normal flow: review → approve → activate |
| Can be regenerated / have messages written | **No** | Yes |
| Can be deleted | Yes | Yes |
| Runs for customers | **Never** | Only after it is approved and activated |
| Newest history row (`versions[0].note`) | "Rejected / regeneration requested by Platform Super Admin. New version: #37 (v2). Remarks: Wait a bit longer before the second reminder" | "Proposed by PayFlow AI as the next version of #18 Promise-to-Pay Follow-Up (v1), rejected by Platform Super Admin on 11 Nov 2026, 20:05 UTC. Remarks: Wait a bit longer before the second reminder" |

## If the old workflow is changed anyway

Every change action on a Rejected workflow returns **400**, with a message that names the new version:

| Call on the old workflow | Response |
|---|---|
| update, save draft, begin review, submit, approve, activate, deactivate | `400 {"detail": "This workflow was rejected and replaced by #37 - use the new version"}` |
| regenerate | `400 {"detail": "This workflow was rejected and replaced by #37 - regenerate the new version"}` |
| write messages (`/content`) | `400 {"detail": "This workflow was rejected and replaced by #37 - write the messages of the new version"}` |
| reject again | No error: nothing changes and the workflow comes back as it is |

## Suggested UI

**On the old workflow (status "Rejected"):**

- A red **Rejected** badge.
- Make the page read-only: hide or disable Edit, Approve, Activate, Regenerate and Write messages.
- A banner: **"Replaced by #37 (v2)"** that links to `/payflow/workflows/{replaced_by_id}`.

**On the new workflow:**

- A line: **"Revision of #18 (v1)"** that links to `/payflow/workflows/{revision_of_id}`.
- The reviewer's remarks are already in `ai_context` under the label `"Reviewer remarks"`.

**Workflows list:** add **"Rejected"** to the status filter. `STRATEGY_STATUSES` in `strategy-workflow.ts` doesn't list it yet.
