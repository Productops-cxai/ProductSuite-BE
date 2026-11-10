# Paid tier: restore the full Regenerate prompt

On 9 Nov 2026 (commit `d413b49`, "reject regenerate issue") the Regenerate prompt was cut down to fit the
Groq **free tier** limit of 8,000 tokens per request (bigger requests fail with `413 Request too large`).
Once we are on a paid tier, restore the full prompt with the patch in this folder.

## What the patch restores

| Sent to the AI on Regenerate | Now (free tier) | After the patch (original) |
|---|---|---|
| Example workflows | 1 branching example + at most 1 of the client's own | All generic examples, the client's own similar ones in place of some |
| Lessons from past reviews | up to 5 | up to 15 |
| Review history | 5 entries, max 4 changes each | 10 entries, all changes |
| Remarks / notes / changes | cut at 800 / 160 characters | full text |
| Retry after failed checks (Suggest too) | trimmed workflow (steps only) | the full rejected workflow |

Size on a sample with a long review history: about **4,000 → 5,700 tokens** for the first call.

## How to restore

From the backend root (`ProductSuite-BE`):

```bash
git apply --check docs/paid_tier/restore_full_regenerate_prompt.patch   # dry run
git apply docs/paid_tier/restore_full_regenerate_prompt.patch
venv\Scripts\python -m pytest tests/test_workflow_*.py -q                 # check
```

It changes only `workflow_suggestion/prompt.py` and `workflow_suggestion/service.py`.

**If the patch no longer applies** (the files changed since), the original code is at commit `6694269`
(the commit before `d413b49`). Copy back only the parts in the table above - do not replace the whole files,
they have newer changes (e.g. the "stop when the customer pays" rule):

```bash
git show 6694269:app/modules/payflow/services/workflow_suggestion/prompt.py
git show 6694269:app/modules/payflow/services/workflow_suggestion/service.py
```

## Also on a paid tier

- `.env`: `GROQ_SEGMENTS_PER_CALL=0` sends all segments in one AI call (faster Suggest).
