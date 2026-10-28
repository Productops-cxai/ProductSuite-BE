# Changes for the Frontend Team

Backend changes the AI team made that the UI can use. **Newest first.** All backward compatible: nothing breaks if the UI does nothing.

| # | Date | Change | Do you need to do anything? |
|---|---|---|---|
| 2 | 2026-10-28 | New segment stat `email_allowed_pct` + "Email allowed" line on AI workflows | Optional: show it next to email consent |
| 1 | 2026-10-28 | Email / SMS steps now carry their own text (written by AI, editable) | To show / edit it. Until then the old templates are sent, as today |

---

## 2. "Email allowed" next to "Email consent"

- `GET /payflow/clients/{client_id}/portfolios/{portfolio_id}/segments` → each segment's `stats` has a new `email_allowed_pct` (`email_consent_pct` is still there).
- AI workflows get one more `ai_context` line right after "Email consent": `{ "label": "Email allowed", "value": "100%" }`.

| Stat | Counts |
|---|---|
| Email consent | Accounts with a recorded **Yes** |
| Email allowed | Accounts with an email address whose consent is **not a recorded No** — who can really get email |

They differ when the account file has no consent column (consent unknown): e.g. Email consent 0%, Email allowed 100%. The AI now uses "Email allowed", so such portfolios are no longer given SMS-only workflows.

---

## 1. Email / SMS text on workflow steps

Suggest and Regenerate now **write the text** of every email / SMS step. Each account gets the same text with its own details filled in when the message is sent.

### What the API returns

Every Communication step's `config` has three new fields (`null` when empty):

```json
{
  "id": "s2",
  "kind": "Communication",
  "title": "Friendly email reminder",
  "config": {
    "channel": "Email",
    "purpose": "Payment Reminder",
    "template_id": "em-reminder",
    "subject": "Friendly reminder about your balance",
    "body": "Hi {{first_name}},\n\nOur records show a balance of {{outstanding_balance}} ...\n\n{{payment_link}}\n\n{{brand_name}}",
    "tone": "Friendly"
  }
}
```

| Field | Meaning |
|---|---|
| `subject` | Email subject. SMS steps have none |
| `body` | The message. `{{…}}` are variables, filled per account when sent |
| `tone` | `Friendly`, `Clear`, `Firm`, `Final notice`, `Supportive` or `Neutral` — decided by code from the segment and the step's place in the workflow |

**No text** (`null`) → the step's template (`template_id`) is sent, exactly as today.

### Suggested UI (on each email / SMS step)
1. **Subject** (emails) and **body** text boxes, editable.
2. A **tone badge**.
3. A **preview** with sample values (table below).
4. A **"Rewrite with AI"** button + optional remarks box → `POST /content` with `step_id` (+ `remarks`).
5. On the workflow: **"Write all messages with AI"** → `POST /content` with `{}` (or `{"remarks": "…"}`) — useful for hand-made workflows.
6. Keep the **template dropdown**: it is the fallback when the text is empty.

Show the AI buttons only for **Draft, AI Proposed, Under Review** workflows (the API refuses others with `400`).

### Saving a reviewer's edits — existing `POST /payflow/workflows/{id}/update`
- Put the edited text in `config.subject` / `config.body` of the steps you send.
- **Send back what you loaded**, including `subject` / `body` / `tone`. Today's editor already copies the whole `config`, so this works as is.
- **Empty box** → send `""` or `null`: no text, the template is sent.
- **Channel or purpose changed** (e.g. Email → SMS): clear `subject` / `body` / `tone` (or call "Rewrite with AI"), otherwise the email text stays on the SMS step.
- Max: subject 255, body 5,000 characters (`422` above that).
- A text edit is a real change: the workflow becomes **Human Modified** and its history shows `"<step title>: message text edited"`.

### New API: `POST /payflow/workflows/{id}/content`

| Body | What happens |
|---|---|
| none or `{}` | AI writes **every** email / SMS (improves the current text if there is some) |
| `{"step_id": "s5"}` | Rewrites **only** that message |
| `{"step_id": "s5", "remarks": "softer, mention the payment plan"}` | Same, following the remarks (`remarks` max 2,000) |
| `{"remarks": "shorter"}` | Rewrites all messages, following the remarks |

- **Response:** the updated workflow (same shape as `GET /payflow/workflows/{id}`). Status and origin do not change (the AI wrote it, so not "Human Modified").
- **Time:** one AI call, ~5–20 s (longer on the Groq free tier). Show a spinner.
- **Errors** (`{"detail": "…"}`): `400` wrong status / no email or SMS steps / bad `step_id`; `403` no permission; `404` workflow not found; `502` AI failed (nothing saved — show `detail`, let the user retry).
- Suggested FE function: `writePayflowWorkflowMessages(strategyId, payload?: { step_id?: string; remarks?: string })`, and add `subject`, `body`, `tone` to `PayflowStrategyStepConfig` in `src/types/index.ts`.

### When the AI could not write the text
Suggest / Regenerate still save the workflow (templates are used) and add this `ai_context` line — show it with a **"Write messages"** button:

```json
{ "label": "Messages", "value": "Not written, so the templates will be sent. Use Write messages to try again. (…)" }
```

### Variables (for the preview)

| Variable | Meaning | Sample value |
|---|---|---|
| `{{first_name}}` | Customer's first name | Alexandra |
| `{{outstanding_balance}}` | Current balance | $1,250.00 |
| `{{account_reference}}` | Account number | ACC-1002456 |
| `{{payment_link}}` | Customer's secure payment page | https://…/pay/… |
| `{{brand_name}}` | Who is sending | Maple Energy |
| `{{promise_amount}}` / `{{promise_date}}` | Promised amount / date (Promise-to-Pay messages only) | $300.00 / 2026-11-15 |
