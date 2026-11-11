# AI issues addressed — and how they were fixed

Branch `ai_changes` · 11 Nov 2026 · Full test suite: **564 passed**; the only 11 failures are the compliance tests from the latest pull (not related to this work).

---

## Ticket — Reject / Request Regeneration with history

**Issue:** When a reviewer rejected a workflow and asked for a new version, the old version's history did not show who rejected it, when, or that it was rejected, and the new version restarted at v1.

**Fix:**
- New workflow status **"Rejected"**. Reject / Regenerate sets the old workflow to **Rejected** — it stays in the workflows list (filter "Rejected") and is **closed**: no edit, review, approve, activate, write messages or regenerate (delete still possible). It never runs.
- Its history gets a row on top: **Rejected · name · date and time · remarks · new version**. The frontend's follow-up reject call no longer adds an "AI Proposed" row on top.
- The new workflow **continues the version number** (v1 → v2) and its first history row says **which workflow it was made from** (id, name, version, who rejected it, when, remarks).
- Links both ways for the frontend: `revision_of_id` (new → old) and new `replaced_by_id` (old → new).
- All saved together with the new workflow (both or nothing). Workflows rejected before this change keep their old status.
- These rows are kept out of the AI's learning (the remarks already reach it once).

---

## #1 — Plan customers were marked "Paid In Full" and their balance wiped

**Issue:** The AI's stop branch sent "paid in full **or** on a plan" to Outcome "Paid In Full". The worker then closed the case and set the balance to 0 — for a customer who had only set up a plan.

**Fix:**
- New stop branch: **paid in full → "Paid In Full"** · **on a plan → "Payment Plan Active"** · **paid part → Human Review**.
- Safety rule for every workflow: "Paid In Full" can only come right after "Payment Status Equals Paid In Full" = yes.
- Max steps per AI workflow raised from 18 to 20 (the branch grew by 2 steps).
- Tested with the worker's own code: a plan customer now keeps their balance.

---

## #2 — Payment checks did not match the worker

**Issue:**
- A plan customer was always "Payment Plan Active", so a **missed instalment was never noticed**.
- A customer who paid part **before** the workflow went straight to Human Review at the first check.

**Fix (worker + AI):**
- On a plan: Payment Status is **"Unpaid" while an instalment is missed** (plan Overdue / Defaulted, or an instalment Overdue / Failed), else "Payment Plan Active".
- "Partial Payment" now counts only payments made **since the workflow started** (each run stores what was already paid when it started — new column `recovered_at_start`, added automatically on startup).
- The AI prompt explains what Payment Status means.

---

## #6 — Timing fired at once for old accounts

**Issue:** The worker waits until "date + amount". A wait timed from "Case Received" or "Due Date" (dates in the past) ended at once, so messages went out back to back.

**Fix:** After the first message, AI workflows must time their waits from **"Previous Action"** (or Payment Link Sent / Promise-to-Pay Date). The validator enforces it and the prompt explains why.

---

## #7 — "Customer Response" was always "Received"

**Issue:** Every message PayFlow sends counts as contact, so "Customer Response = Received / None" said nothing about the customer.

**Fix:** AI workflows may only check **"Customer Response = Dispute Raised"**. (Branches on email / SMS delivery were already blocked.)

---

## #9 — Messages only in the client's language

**Issue:** The AI wrote one text per message in the client's language. French customers of an English client got English.

**Fix:**
- The AI writes the main text (client's language, fully checked), then **one translation per other language found among the accounts of the workflow's segment** — any language the backend allows.
- Each translation is checked without word lists: same `{{variables}}`, no new numbers (amounts, dates, phones, deadlines), no links, sensible length, SMS keeps "STOP" and translates the opt-out sentence.
- Saved on each message step as `translations` (e.g. `{"FR": {subject, body, sms_body}}`); the **worker sends the customer's language**, else the main text.
- **Editing a message re-translates it in the same save** (only the edited messages; no AI call when no text changed).
- A failed translation never blocks a save: those customers get the main text, and the "Languages" line says so.

---

## #10 — Message checker missed some things

**Issue:** Typed phone numbers, web / email addresses, "legal proceedings", threats to send the account to a collection agency, and deadlines such as "within 48 hours" got through.

**Fix:** All of these are now sent back to the AI to rewrite. Saying "we are a collection agency contacting you on behalf of …" is still allowed (agencies must say who they are).

---

## #11 — One AI hiccup failed the whole Suggest

**Issue:** Only "too many requests" (429) was retried.

**Fix:** Timeouts, connection errors and server errors (5xx) are now retried twice (after 2 s, then 5 s). Bad requests are still not retried.

---

## Smaller AI improvements

| Issue | Fix |
|---|---|
| Regenerate replaced the text a reviewer had written | The reviewer's text is **kept word for word** (and translated); only the other messages are written fresh. A "Reviewer text kept" line names them. |
| The "Languages" line went stale after "Write messages" or an edit | Rebuilt from the steps after every write / edit; the "Messages: not written" warning disappears once all messages have text. |
| Templates (used when a step has no own text) were English only | Translated templates in `message_template_translations.json` (French now), made by a script with the same AI translator and checks; the worker sends the customer's language. |
| 4 AI tests were out of date after teammates' changes | Updated to the current behaviour (human-made and client-level workflows can be regenerated; system rules apply only when listed on the client; lessons read from the version table). |

---

## Good to know

- **Old workflows** made before these fixes keep their old logic until regenerated or edited — delete or regenerate them, especially Active ones.
- **A new account language:** allow it in the import (`ACCOUNT_IMPORT_LANGUAGES`), then run
  `python -m app.modules.payflow.services.workflow_suggestion.template_translations` — messages in that language are then written automatically.
- **Please have a French speaker read** `message_template_translations.json` (one phrase in `em-firm` is clumsy).
- **Saves that edit message text** take a few seconds longer (the re-translation runs in the same request).
