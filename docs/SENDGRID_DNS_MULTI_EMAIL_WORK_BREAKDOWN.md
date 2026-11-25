# SendGrid + DNS — Multi Email Per Client (Work Breakdown)

**Decision:** Use **SendGrid + DNS domain authentication** (not mailbox OAuth).

**Implementation status (repo):**
- **Backend:** implemented (providers, identities, DNS auth helpers, resolver, client-key send/webhook, From failover, payment clears override, tests).
- **Frontend:** not implemented yet (still single `emailFrom` branding UI).
- Client secrets encrypt with **`ENV_MASTER_KEY`** (no separate `SENDGRID_CLIENT_TOKEN_KEY`).

**Tenancy (locked):**
- **Each client has their own SendGrid account** (own API key + webhook verification key).
- That one client SendGrid account holds **multiple From emails** for that client.
- Another client → **different** SendGrid account → their own emails.
- Platform `.env` SendGrid keys remain only for **PayFlow system / fallback** mail (not for client collection From list).

**Product intent (3 points):**
1. One client can have **multiple From emails** on **their** SendGrid; each identity has verification/DNS state (DNS is per domain on that client’s SendGrid).
2. Each From email has an **on/off toggle**. Only toggled-on identities may be used to send.
3. If an account **spam-marks or unsubscribes** from mail sent via From-A **and has not paid**, the **next** collection email to that account goes from another From-B; B’s toggle turns **on** automatically. Accounts that did not spam/unsub keep receiving from the original From-A.

**Key BE modules:**
- `app/modules/payflow/services/email_provider_service.py`
- `app/modules/payflow/services/sender_identity_service.py`
- `app/modules/payflow/sender_routes.py`
- `app/infrastructure/email/sendgrid_domain_auth.py`
- `tests/test_client_sendgrid.py`

**Related:** [CLIENT_EMAIL_SENDING_OPTIONS.md](./CLIENT_EMAIL_SENDING_OPTIONS.md) (options comparison that led to this path).

---

## Product rules to lock before build

Clarify these so BE/FE don’t guess wrong:

1. **Same domain vs different domain**  
   - `a@client.com` + `b@client.com` → usually **one** DNS domain auth covers both.  
   - `a@clientA.com` + `b@clientB.com` → separate DNS per domain.  
   UI should still show status **per From email**, but DNS work is **per domain**.

2. **How many toggles can be ON?**  
   - Recommended: one **Primary** + any number of **Enabled (failover pool)** identities.  
   - Or: multiple ON, Primary marked separately.

3. **Spam/unsub behaviour change**  
   - Today: stop all email (`email_consent = false`).  
   - New intent: for no-payment cases, **do not permanently kill channel** — rotate From for that account.  
   - Need legal/compliance sign-off: rotating From after spam/unsub can be sensitive.

4. **“Without any payment”**  
   - Means: no successful payment on the account (ever / in current strategy / since last send)? Define exactly.

5. **Explicit Unsubscribe (preference page) vs Spam**  
   - Same failover rule for both, or unsub still means “stop all email”?

6. **If no other verified+enabled From exists** when failover needed → hold / suppress / keep old behaviour?

---

# BACKEND — detailed work list

## BE-1. Data model — client sender identities

**New table (suggested):** `payflow_client_sender_identities`

| Column | Purpose |
|---|---|
| `id` | PK |
| `client_id` | FK → client |
| `email` | From address (lower-cased, unique per client) |
| `display_name` | Optional From name override |
| `domain` | Derived domain (`client.com`) |
| `is_enabled` | Toggle — may be used for send / failover |
| `is_primary` | Default From for accounts with no override |
| `verification_status` | `pending` / `dns_pending` / `verified` / `failed` / `disabled` |
| `sendgrid_domain_id` / external refs | Optional IDs from SendGrid whitelabel APIs |
| `dns_records` | JSON snapshot of required CNAME/TXT (for UI) |
| `last_verified_at` | |
| `priority` / `failover_order` | Which From to try next |
| `connected_by` / timestamps | Audit |

**Also:**
- Keep `payflow_clients.email_from` as **deprecated mirror** of primary (or migrate reads fully to new table).
- Unique: `(client_id, email)`.
- Constraint: at most one `is_primary=true` per client among enabled+verified (enforce in service).

**Files:** `models/__init__.py`, schema_sync (existing pattern).

---

## BE-2. Per-account From override (failover memory)

**New table (suggested):** `payflow_account_sender_overrides`

| Column | Purpose |
|---|---|
| `account_id` | FK |
| `client_id` | FK |
| `blocked_identity_id` | From they spam/unsub’d against |
| `active_identity_id` | From to use for next sends |
| `reason` | `spamreport` / `unsubscribe` / `manual` |
| `payment_gate` | only created when “no payment” rule matched |
| `created_at` / `cleared_at` | |

**Why:** Point 3 is **per account**, not global client switch. Other accounts must keep Primary.

---

## BE-3. Persist From on every send

**Change `payflow_communications`:**
- Add `from_email`, `from_name`, `sender_identity_id` (nullable FK)

**Change logging:**
- Keep `[from=…]` in `email_logs` but stop relying on it as source of truth.
- Comm detail / resend / analytics must use stored From.

**Files:** models, `communication_service.py`, any export paths.

---

## BE-4. Per-client SendGrid account (credentials table) — required

**New table:** `payflow_client_email_providers`  
**(one row per client SendGrid account — not per email)**

| Column | Purpose |
|---|---|
| `id` | PK |
| `client_id` | FK, unique (v1: one SendGrid account per client) |
| `provider` | `sendgrid` |
| `api_key_enc` | Fernet-encrypted API key — never returned by API |
| `webhook_verification_key_enc` | Encrypted webhook verify key for this client’s SendGrid |
| `is_active` | |
| `label` | optional (“Client X production”) |
| `last_checked_at` / `last_error` | connectivity / auth errors |
| timestamps | |

**APIs:**
| Method | Path | Purpose |
|---|---|---|
| GET | `/payflow/clients/{id}/email-provider` | status only (masked: configured yes/no, last error) — **no raw keys** |
| POST | `/payflow/clients/{id}/email-provider/upsert` | save/rotate API key + webhook key |
| POST | `/payflow/clients/{id}/email-provider/test` | lightweight SendGrid auth check |
| POST | `/payflow/clients/{id}/email-provider/delete` | disconnect provider |

**Send path change:** `send_collection_email` / SendGrid HTTP call must use **that client’s** decrypted API key, not always `settings.SENDGRID_API_KEY`.

**Webhook:** events must verify with the **correct client key**. Options:
- Prefer: custom_arg / unique args already send `communication_id` → load comm → client → provider → verify with that key; or
- Signed webhook per client is harder on one URL — verify after resolving communication/client from event payload (`client_id` / `communication_id` in unique_args).

Platform env keys stay for non-client / fallback mail only.

---

## BE-5. SendGrid DNS / domain verification (on the **client’s** SendGrid)

**New module (suggested):** `app/infrastructure/email/sendgrid_domain_auth.py`

Work items:
- All domain-auth API calls use **client provider** `api_key_enc`, not platform key
- Create/authenticate domain when first From for that domain is added on that client
- Return DNS records to store + show in UI
- Validate/check domain status
- Map SendGrid errors to identity/domain status

**Note:** One DNS setup per **domain** on that client’s SendGrid; multiple From emails on same domain share it.

---

## BE-6. Sender identity APIs (POST-style, PayFlow convention)

Requires email-provider configured before adding From emails (or allow draft emails pending provider).

| Method | Path | Purpose |
|---|---|---|
| GET | `/payflow/clients/{id}/sender-identities` | List emails + toggle + DNS/verify status |
| POST | `/payflow/clients/{id}/sender-identities` | Add email (creates/links domain auth on **client** SendGrid) |
| POST | `/payflow/clients/{id}/sender-identities/{sid}/update` | display name, failover order |
| POST | `/payflow/clients/{id}/sender-identities/{sid}/toggle` | enable/disable |
| POST | `/payflow/clients/{id}/sender-identities/{sid}/set-primary` | make default From |
| POST | `/payflow/clients/{id}/sender-identities/{sid}/verify` | re-check SendGrid/DNS status |
| POST | `/payflow/clients/{id}/sender-identities/{sid}/delete` | soft/hard delete rules |
| GET | `/payflow/clients/{id}/sender-domains` | domain-level DNS records + status |

Permissions: `edit_client` for that client. Auditable.

**Files:** new routes module or under clients; `schemas.py`; wire `main.py`.

Identities: optional `email_provider_id` FK → client’s provider row (v1 always that one provider).

---

## BE-7. From resolver (replace `_resolve_collection_from`)

**File:** `communication_service.py`

New selection order per send:
1. If account has **active override** → use that identity (must be enabled + verified; else fallback rules).
2. Else use client **primary** enabled+verified identity.
3. Else first enabled+verified by failover order.
4. Else today’s fallback (global operator From / error — product choice).
5. Third Party: decide if multi-client-From allowed or still operator-only (legal).

Must run for:
- workflow send
- manual send
- resend (policy: same From as original vs re-resolve)

---

## BE-8. Spam / unsub → From failover (core of point 3)

**Today:** `apply_provider_event` → `email_consent=False` + Suppress.

**New path (proposed):**

On SendGrid `spamreport` / `unsubscribe` / `group_unsubscribe` (and optionally preference-page unsub — if product agrees):

1. Know **which From** was used → from `communications.sender_identity_id` / `from_email`.
2. Check **payment rule** (“without any payment”).
3. If payment exists → keep current strict unsub (or product rule).
4. If **no payment**:
   - Do **not** only flip global `email_consent=false` in a way that blocks forever **or** redefine: consent becomes “blocked for this From”, not all email. **This is the biggest behaviour change.**
   - Pick next identity: enabled? if none, **auto-enable** next verified identity by failover order (point 3: toggle ON automatically).
   - Write `payflow_account_sender_overrides` (blocked A, active B).
   - Log engagement event e.g. `email_from_failover` (new) **and/or** keep `email_unsubscribed` only for true global opt-out.
5. Next `send_workflow_step` uses resolver → sends from B for that account only.
6. Other accounts: still Primary A.

**Also update:**
- `email_preference_service` / one-click unsub — same policy or stricter
- `engagement_service` mapping
- Adaptive workflows: today `email_unsubscribed` may switch channel / exit — revisit so failover doesn’t look like “dead email” incorrectly
- Eligibility gates: must allow send when override exists even if old consent model would block

**Files:** `communication_service.py`, `email_preference_service.py`, `engagement_service.py`, `adaptive_workflow_service.py`, compliance/eligibility if they key off `email_consent`.

---

## BE-9. Payment interaction

- Define helper: `account_has_disqualifying_payment(...)` used by failover rule.
- On payment success: optional clear of overrides? or keep B? (product decision)
- Ensure Stripe/payment hooks don’t fight the new rule.

---

## BE-10. Verification gates before send

- Client must have active email-provider (API key) before collection sends via client From.
- Refuse send from identity unless `verification_status=verified` on **that client’s** SendGrid domain.
- If primary unverified → clear error / operational exception, don’t silently use platform From unless product allows fallback.
- Multiple domains on **that one client** SendGrid account are fine; track per-domain status.

---

## BE-11. Webhooks / events

- Ensure spam/unsub webhook ties to communication → client → provider key → From identity.
- Verify signature with **that client’s** `webhook_verification_key_enc` (not only env key).
- Store which From was complained about.
- Idempotency unchanged (`provider_event_id`).

**File:** `sendgrid_webhook.py` + `apply_provider_event`.

---

## BE-12. Client create/update migration

- Creating client: allow 0..N identities instead of single `email_from`.
- Activation branding checks (`_branding_issues`): require ≥1 verified+enabled (+ primary) if email channel on.
- Backfill: existing `email_from` → one identity row, primary+enabled, status unknown/pending until verified.
- Third Party branding rules unchanged unless legal opens client From.

**Files:** `client_service.py`, schemas, seeder if needed.

---

## BE-13. Tests

- Provider upsert/test/delete; keys never returned; send uses client API key
- Webhook verifies with correct **client** webhook key
- Multi identity CRUD + toggle + primary constraints
- Domain auth mock on client SendGrid (DNS records + verify)
- Resolver: primary / override / auto-enable failover
- Spam without payment → override to B; other accounts still A
- Spam/unsub with payment → agreed behaviour
- True global opt-out still blocks when configured
- Communication stores `from_email` / identity id
- Adaptive/eligibility don’t wrongly block failover sends

---

# FRONTEND — detailed work list

## FE-1. Types + API client

**Files:** `src/types/index.ts`, `src/api/payflow.ts`

- Types for email-provider (masked status), sender identity, domain DNS records, verification, toggle/primary
- API helpers: provider upsert/test/delete + identity list/add/update/toggle/set-primary/verify/delete + domain DNS fetch

---

## FE-1b. Connect client SendGrid account (first step on client)

- UI to paste **API key** + **webhook verification key** for **this client’s** SendGrid
- Show configured / not configured / last error (never show full key again after save — only “rotate”)
- Test connection button
- Block “add From email” until provider is configured (or allow draft with warning)
- Help: “Each client uses their own SendGrid account; multiple From emails live under that account”

---

## FE-2. Replace single `emailFrom` with Sender Identities UI

**Files:** `client-config-sections.tsx`, `ClientNewPage.tsx`, `ClientDetailPage.tsx`

- Remove/replace single From input with a **list**:
  - email, display name, domain, verified badge, enabled toggle, primary badge
  - Add email
  - Set primary
  - Enable/disable toggle (point 2)
  - Delete (with rules)
- Must be **editable after activation** (failover/toggles can’t be draft-locked like old branding)
- First Party vs Third Party copy
- Validation: can’t disable last enabled identity if email channel on (or warn)

---

## FE-3. DNS / SendGrid status panel (per domain, shown per email)

- For each identity/domain: show required DNS records (host/type/value) + copy buttons
- Status: pending / verified / failed
- “I’ve added DNS — Check status” → calls verify API
- Help text: same domain emails share one DNS setup; different domains need separate DNS
- Do **not** pretend each email always needs unique DNS if domain matches

---

## FE-4. Primary + failover order UX

- Mark Primary clearly (“default From for most accounts”)
- Optional drag/order or numeric failover order for auto-toggle target
- Explain: if someone spam/unsubs from Primary with no payment, next mail may use next identity and that toggle turns on

---

## FE-5. Communication / case visibility

**Files:** `CommDetailPage.tsx`, case communication lists, export dialog

- Show **actual From used** on the communication (from BE field), not live client primary
- If failover happened, show badge: “Sent via failover From” + reason if available

---

## FE-6. Unsubscribe / spam staff views

**Files:** `EmailUnsubscribesPage.tsx`, `CaseDetailPage.tsx`

- Show whether event caused **global unsub** vs **From failover**
- Show blocked From + active From override on the case
- Staff action (optional later): clear override / force primary / force global unsub

---

## FE-7. Client wizard + locked branding flow

- Wizard step: add one or more From emails (not one field)
- After activate: identities section remains editable; other branding may stay locked as today
- Strip-on-save logic in `ClientDetailPage` must **not** strip sender-identity edits

---

## FE-8. Empty / error states

- No verified From + email channel on → blocking banner
- Toggle off all identities → prevent save / warn
- Failover needed but no other verified identity → staff/ops message

---

## FE-9. Copy / i18n / operator messaging

- Short layman text: what DNS is, why needed, what toggle means, what auto-failover means
- Third Party: if still operator From, hide multi-From or show read-only policy

---

# Suggested build order

1. **Per-client SendGrid provider table + encrypt keys + send/webhook use client key**  
2. **FE: connect SendGrid account for client**  
3. **Identities model + APIs + backfill** (emails + toggles + primary)  
4. **FE list/toggle/primary + DNS status UI**  
5. **Domain auth on client SendGrid + verify gate on send**  
6. **Store From on communications + resolver uses primary**  
7. **Account override + spam/unsub failover (point 3)**  
8. **Adaptive/eligibility/consent alignment + tests**  
9. **Staff visibility (comm detail, unsub list)**

---

# SendGrid keys — env vs database (locked wayout)

## Your model (now documented as the plan)

```text
Client A → SendGrid account A → emails a1@, a2@, a3@ …
Client B → SendGrid account B → emails b1@, b2@ …
```

- **1 SendGrid account per client** (credentials in DB, encrypted)
- **Many From emails per that account** (identities table)
- **Not** 1 API key per email
- **Not** all clients sharing one PayFlow SendGrid for collection From

| Setting | Per what? | Where |
|---|---|---|
| API key | Per **client SendGrid account** | DB `payflow_client_email_providers.api_key_enc` |
| Webhook verification key | Per **client SendGrid account** | DB `…webhook_verification_key_enc` |
| From email / name | Per **email identity** | DB `payflow_client_sender_identities` |
| Platform `SENDGRID_*` in `.env` | PayFlow system / fallback only | `.env` **once** |

## What NOT to do

- ❌ `SENDGRID_API_KEY_CLIENT_1` … in `.env` for every client
- ❌ Duplicate API key on each of the 5 email rows
- ❌ Return API keys from GET APIs
- ❌ Assume one shared SendGrid for all client collection mail (that was the old draft recommendation — **rejected**)

## Security

- Encrypt at rest with `ENV_MASTER_KEY` via `secrets_crypto.encrypt_value` / `decrypt_with_key`
- UI: enter once / rotate; list endpoint returns `configured: true`, never the secret
- Ops: each client’s SendGrid Event Webhook should point at PayFlow `/webhooks/sendgrid` with **their** verification key stored on the provider row

---

# Out of scope (unless added later)

- Mailbox OAuth / Graph / Gmail send
- Per-portfolio From (can add `portfolio_id` later)
- Automatic DNS at registrar (client IT still adds records)
- Paying customers’ From rotation policy (needs explicit decision)
- Multiple SendGrid accounts **per one client** (v1 = one provider row per client)

---

# Effort sketch (rough)

| Track | Rough |
|---|---|
| BE (model, SendGrid DNS, resolver, failover, tests) | larger slice — multi-week capable |
| FE (identities UI, DNS panel, toggles, comm/case displays) | medium |

Exact days depend on locked product rules in the section above (especially spam/unsub vs consent).
