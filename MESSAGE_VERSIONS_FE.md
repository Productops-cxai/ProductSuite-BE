# Message versions on the workflow screen

## What a message has now

When the AI writes a workflow's messages, every Communication step gets a **main text**. On top of that, the AI writes a **version for each age group** that has 12 or more customers in the workflow. The code decides which versions exist. The user does not set up segments.

```
 Communication step "Reminder 1"
 ┌───────────────────────────────────────────────────────────────┐
 │ Main text          ← everyone without a version of their own  │
 │ Age 18 – 24        ┐                                          │
 │ Age 25 – 34        │                                          │
 │ Age 35 – 44        │  a version only when 12+ customers       │
 │ Age 45 – 54        │  in the workflow are this age            │
 │ Age 55 – 64        │  (a step has 0 to 6 versions)            │
 │ Age 65 and over    ┘                                          │
 └───────────────────────────────────────────────────────────────┘
 Every text has:  email subject · email body · SMS text · AI explanation · translations (e.g. French)
```

**Who gets what when the workflow runs:** a customer gets their age group's version if the step has one, in their own language. Everyone else gets the main text in their language. The worker picks this automatically.

## Where to find it in a step

Everything is inside the step's `config`, in the workflow you already load:

| What | Where |
|---|---|
| Main text | `subject`, `body`, `sms_body` |
| Main text's AI explanation | `brief` (the reason is in `brief.why`) |
| Main text in other languages | `translations` → `{"FR": {subject, body, sms_body}}` |
| The versions | `versions` → `{"age_65_plus": {...}, "age_18_34": {...}}` |
| One version | `label`, `subject`, `body`, `sms_body`, `brief`, `translations`: the same fields as the main text |

**The "Selected version" dropdown:** "Main / Default", then one entry per key in `versions`, shown with its `label` (for example "Age 65 and over").
- Only list the versions the step actually has.
- Don't hard-code age groups. The groups are **18 – 24, 25 – 34, 35 – 44, 45 – 54, 55 – 64 and 65 and over**.
- Workflows written before these six groups can have **"Age 18 – 34"** and **"Age 35 – 64"** versions. Show them the same way, with their `label`. They keep working as they are.

**Which text boxes to show** depends on the step's channel:

| Channel | Show |
|---|---|
| Email + SMS (AI steps) | Email subject, Email body, SMS text (`sms_body`) |
| Email only | Email subject, Email body |
| SMS only | SMS text only. **On an SMS-only step the SMS text is stored in `body`**, not `sms_body`, for the main text and for every version |

## What each button does

| Button | What to call | What happens |
|---|---|---|
| **Save version** | Save the workflow as you do today (`POST /payflow/workflows/{id}/update`), with the edited text inside that version | Saved. If the workflow has customers in other languages, the edited text is translated again automatically. Any problems in the text come back as warnings (see below) |
| **Regenerate** (one text) | `POST /payflow/workflows/{id}/content` with `{"step_id": "s3", "audience": "age_65_plus", "remarks": "..."}`. Use `"audience": "main"` for the main text | The AI rewrites only that text (and its translations), applying the remarks. The main text, the other versions and the other steps don't change. A note is added to the workflow history |
| **Regenerate** (whole step) | The same call with only `step_id` | The AI rewrites the main text and every version of that step |
| **Remove version** | Save the step without that version in `versions` | The version is gone. That age group gets the main text. **Hide this button on the main text:** it can't be removed |
| Email / SMS preview | Nothing | Show the text as it is. Values in `{{ }}` are filled in per customer when it's sent |

**Regenerate** only works on **Draft, AI Proposed and Under Review** workflows, the same as Write messages today. Add a **remarks box** to it: the AI follows what the reviewer writes there.

If Regenerate can't be done, the call returns an error with a readable message:

| Situation | Response |
|---|---|
| No step chosen | 400 "Choose the message (step_id) whose text to rewrite" |
| The step doesn't have that version | 400 "Message s3 has no Age 18 – 24 version - rewrite its main text or one of its versions" |
| The AI couldn't do it | 502 with a message starting "AI could not ...". Nothing was changed, so the user can try again |

## Warnings after saving

When a person edits a text, the save now checks it the same way the AI's own text is checked. **The save always goes through.** Problems come back in the save response as `text_warnings`:

```json
"text_warnings": [
  {"step_id": "s3", "text": "age_65_plus", "channel": "SMS", "problem": "SMS must end with 'Reply STOP to opt out.'"}
]
```

- `text` says which text it is: `"main"` or the version key.
- `channel` says which part: `"Email"` or `"SMS"`.
- An empty list means everything is fine.
- Only the texts changed in that save are checked.

Examples of problems: "must include {{payment_link}}", "'legal action' sounds like a threat - remove it", "email body has 20 words - write 30 to 200".

**Suggested UI:** after Save, show each warning under the text box it belongs to, in amber, with a note that the text was saved.

## Personalization Context (real data, not simulated)

`GET /payflow/workflows/{id}/audiences` returns one entry per text: the main text plus each version. Match an entry to the dropdown by its `key`. It is read-only and cheap: no AI and nothing saved.

| Field in the design | Where it comes from |
|---|---|
| Age group | `profile.age`, e.g. `{"65 and over": 100}` |
| Country / Region | `profile.countries`, `profile.regions` |
| Language preference | `profile.languages`, e.g. `{"EN": 87, "FR": 13}` |
| Outstanding balance | `profile.avg_balance`, `profile.balance_bands` |
| Reminder stage | The step's Message Purpose (already on the screen) |
| Communication history & preferences | `profile.contact_history`, `profile.with_email_pct`, `profile.with_mobile_pct` |
| Payment arrangement status | `situation` (e.g. "Payment Plan", empty for an ordinary segment) and `profile.partly_paid_pct` |
| Number of customers | `accounts` |

- All numbers are **percentages of that text's customers** or averages. Never one person's data.
- The data is from today's accounts, so it can differ a little from the day the text was written.
- An email open rate per version is not in this call yet.

**Remove the "simulated" labels** on Personalization Context and on AI Explanation. For the AI Explanation, show `brief.why` first. `approach`, `tone`, `length` and `call_to_action` can go under it.

## Changing a step's channel or purpose

The versions were written for the old channel or purpose. When the step is saved after such a change, **its versions are removed**, along with the main text's AI explanation. This stops an SMS going out with an email's text.

**Suggested UI:** before the change, warn: "This clears the step's texts and its age versions. Use Regenerate on the step to write them again."

## Translations (missing from the design)

Every text can have versions in other languages (`translations`, keyed by language code such as `FR`). Reviewers can't see them at the moment.

**Suggested UI:** a small language switch (EN / FR / ...) above the text boxes, read-only for the other languages. Edits are made in the main language, and the translations are rewritten automatically on save.

## Approval

There is no approval per version. Approving the workflow approves all of its texts together, as the Version Summary in the design says. Versions can't be switched off: a version is either there or removed.

## Checklist for the frontend

1. Dropdown: Main text plus the step's `versions`, using each version's `label`.
2. Text boxes per channel. On an SMS-only step the SMS text is in `body`.
3. Save version: the normal update call, then show `text_warnings`.
4. Regenerate: the `/content` call with `step_id` + `audience` + remarks. Only for Draft, AI Proposed and Under Review.
5. Remove version: save the step without it. No Remove on the main text.
6. Personalization Context: the `/audiences` call, matched by `key`. Remove "simulated".
7. AI Explanation: `brief.why` first. Remove "simulated".
8. Language switch to view translations.
9. Warn before a channel or purpose change.
