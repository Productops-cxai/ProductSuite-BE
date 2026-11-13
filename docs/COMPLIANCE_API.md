# Compliance — API for the Frontend

All paths start with `/payflow`. Same login as the rest of PayFlow (session cookie).

General points:

- **Rules move through statuses:** `Draft` → `Pending Approval` → `Active` → `Inactive` / `Archived`. There is no status field to edit; each move has its own endpoint.
- **Who can do what:** `view_rules` to read; `modify_governance` to create, edit, submit and set client rules; **Operations Admin** to approve, reject, activate, deactivate, archive and read the audit log.
- **Mandatory rules** cannot be switched off for a client.
- Most changes take an optional `reason`; reject, deactivate and client on/off **require** one.
- Errors: `403` = no permission, `400` = bad input (the message says what), `404` = not found, `409` = conflict.

| # | Endpoint | What it is for |
|---|---|---|
| 1 | `GET /compliance/catalog` | All dropdown lists for the editor and filters |
| 2 | `GET /compliance/jurisdictions` | Countries, states / provinces, regions, time zone |
| 3 | `GET /compliance/rules` | Rules list with filters |
| 4 | `POST /compliance/rules` | Create a rule (Draft) |
| 5 | `GET /compliance/rules/{id}` | One rule |
| 5b | `POST /compliance/rules/{id}/explain` | AI plain-language explanation (What it is / When it applies / What happens) |
| 6 | `POST /compliance/rules/{id}/update` | Edit a rule |
| 7 | `POST /compliance/rules/{id}/duplicate` | Copy a rule as a new Draft |
| 8 | `POST /compliance/rules/{id}/submit` | Draft → Pending Approval |
| 9 | `POST /compliance/rules/{id}/approve` | Pending Approval → Active (or approved, not yet active) |
| 10 | `POST /compliance/rules/{id}/reject` | Pending Approval → Draft |
| 11 | `POST /compliance/rules/{id}/activate` | Put an approved / inactive rule in force |
| 11b | `POST /compliance/rules/{id}/schedule-activate` | FR-06: Active with a **future** `effective_from` (Pending Approval or approved Inactive) |
| 12 | `POST /compliance/rules/{id}/deactivate` | Take a rule out of force |
| 13 | `POST /compliance/rules/{id}/archive` | Archive a rule |
| 14 | `GET /compliance/rules/{id}/history` | Version history of a rule |
| 14b | `GET /compliance/rules/{id}/clients` | FR-05 Associated Clients (reverse mapping) |
| 15 | `GET /compliance/holidays` | Holiday / restricted-date calendar |
| 16 | `POST /compliance/holidays` | Add a holiday |
| 17 | `POST /compliance/holidays/{id}/update` | Edit a holiday |
| 18 | `POST /compliance/holidays/{id}/delete` | Delete a holiday |
| 19 | `POST /compliance/evaluate` | Rule testing: would this contact be allowed? |
| 20 | `GET /compliance/audit` | Audit log (Operations Admin) |
| 21 | `GET /clients/{id}/compliance-rules` | Rules that apply to a client (Effective Rules) |
| 22 | `POST /clients/{id}/compliance-rules/update` | Switch one rule on / off for a client |
| 23 | `POST /clients/{id}/compliance-rules/sync` | Re-link the client to the rules of its jurisdiction |
| 24 | `GET /accounts/{id}/compliance-profile` | An account's compliance facts (consent, flags, ...) |
| 25 | `POST /accounts/{id}/compliance-profile/update` | Edit those facts |
| 26 | `GET /accounts/{id}/compliance-overrides` | Overrides requested for this account |
| 27 | `POST /compliance/overrides` | Request an authorized override (reason + evidence) |
| 28 | `POST /compliance/overrides/{id}/approve` | Approve a Pending override |
| 29 | `POST /compliance/overrides/{id}/reject` | Reject a Pending override |

---

## The rule object

Returned by endpoints 3–13.

```json
{
  "id": 12, "code": "CMP-003",
  "name": "Ontario · recipient-local contact window",
  "description": "...",
  "category": "Permitted Hours",
  "rule_type": "JUR-01", "rule_type_name": "Contact hours",
  "enforcement": "Mandatory",
  "regulatory_authority": "...", "legal_reference": "...", "source": "...",
  "jurisdiction_type": "State / Province",
  "country": "Canada", "state": "Ontario", "region": "", "city": "", "postal": "",
  "timezone": "America/Toronto",
  "collector_type": "", "debt_type": "",
  "channel": "All Channels",
  "logic": "AND",
  "conditions": [{"id": "c1", "field": "Outside Permitted Hours", "operator": "Equals", "value": "Yes", "period": 7}],
  "settings": {"start": "08:00", "end": "21:00", "days": [1, 2, 3, 4, 5, 6]},
  "action": "Reschedule",
  "override_policy": "Not Permitted",
  "priority": 100,
  "effective_from": "2026-01-01", "effective_until": null,
  "status": "Active", "version": 2,
  "client_id": null,
  "is_library": false,
  "created_by": "...", "updated_by": "...", "submitted_by": "...", "approved_by": "...", "approved_at": "...",
  "rejection_reason": null,
  "created_at": "...", "updated_at": "..."
}
```

- `client_id: null` = applies to every client in the jurisdiction; a number = one client's own rule.
- `is_library: true` = a jurisdiction-library rule (seeded as Draft, never enforced until activated).
- `timezone` is worked out by the backend from the jurisdiction (read-only).
- Endpoint 5 also returns `activation_problems`: a list of reasons the rule cannot be activated yet (empty = ready).

---

## 1. `GET /compliance/catalog`

Lists for every dropdown: `categories`, `rule_types` (JUR-01…JUR-25, each with its default category, action and condition), `statuses`, `enforcements`, `actions`, `channels`, `logics`, `operators`, `condition_fields`, `jurisdiction_types`, `collector_types`, `debt_types`, `override_policies`, `count_events`, `count_scopes`, `account_flags`, `default_settings`, `countries`.

## 2. `GET /compliance/jurisdictions`

Query (optional): `country`, `state`. Returns the countries with their states / provinces, the regions of a state, and the time zone. Use it for the jurisdiction pickers.

## 3. `GET /compliance/rules`

Query (all optional): `status`, `category`, `rule_type`, `country`, `state`, `channel` (exact match), `enforcement`, `client_id`, `search`, `sort` (default `updated_at`), `order` (`desc` default / `asc`), `limit` (default 50, max 200), `offset`.

```json
{"rules": [{"...": "rule object"}], "total": 53}
```

## 4. `POST /compliance/rules` · 6. `POST /compliance/rules/{id}/update`

Body: any rule fields (missing fields keep their value on update), plus an optional `reason`.

```json
{
  "name": "Ontario contact window", "rule_type": "JUR-01", "category": "Permitted Hours",
  "enforcement": "Mandatory", "jurisdiction_type": "State / Province",
  "country": "Canada", "state": "Ontario", "channel": "All Channels",
  "logic": "AND",
  "conditions": [{"field": "Outside Permitted Hours", "operator": "Equals", "value": "Yes"}],
  "settings": {"start": "08:00", "end": "21:00", "days": [1, 2, 3, 4, 5, 6]},
  "action": "Reschedule", "priority": 100, "effective_from": "2026-01-01",
  "client_id": null, "reason": "optional"
}
```

Returns the rule object. A new rule is always a `Draft`. `rule_type` is required.

## 7. `POST /compliance/rules/{id}/duplicate`

No body. Returns the copy (a new Draft).

## 8–13. Status changes

| Endpoint | Body | Note |
|---|---|---|
| `submit` | `{"reason": "optional"}` | Draft → Pending Approval |
| `approve` | `{"reason": "optional", "activate": true}` | `activate: false` = approved but not in force yet |
| `reject` | `{"reason": "required"}` | back to Draft, `rejection_reason` set |
| `activate` | `{"reason": "optional"}` | `400` with the problems when the rule is not complete |
| `schedule-activate` | `{"effective_from": "YYYY-MM-DD", "reason": "optional"}` | Future date required. Pending Approval or approved Inactive → **Active**; engine waits until `effective_from` |
| `deactivate` | `{"reason": "required"}` | |
| `archive` | `{"reason": "optional"}` | |

All return the rule object.

FE may **display** lifecycle aliases without changing API statuses: `Pending Approval` → Pending Review; `Active` + future `effective_from` → Scheduled; `Active` + past `effective_until` → Expired.

## 14. `GET /compliance/rules/{id}/history`

```json
{
  "rule_id": 12, "code": "CMP-003", "current_version": 2,
  "versions": [
    {"id": 40, "version": 2, "status": "Active", "event": "Rule activated", "snapshot": {"...": "rule fields"}, "change_reason": "...", "changed_by": "...", "created_at": "..."}
  ]
}
```

## 14b. `GET /compliance/rules/{id}/clients`

Associated Clients (FR-05). Returns clients in the rule’s jurisdiction (or the single client when the rule is client-scoped), with mapping flags.

```json
{
  "rule_id": 12,
  "rule_code": "CMP-003",
  "rule_name": "Ontario · contact window",
  "client_scoped": false,
  "total": 3,
  "associated_count": 2,
  "clients": [
    {
      "client_id": 5,
      "code": "ACME",
      "name": "Acme Collections",
      "country": "Canada",
      "province_state": "Ontario",
      "city": "Toronto",
      "status": "active",
      "associated": true,
      "enabled": true,
      "locked": true,
      "in_force": true,
      "source": "auto"
    }
  ]
}
```

---

## 15–18. Holidays

`GET /compliance/holidays?country=&state=` →

```json
{"holidays": [{"id": 1, "name": "Christmas Day — sample blackout", "date": "2026-12-25", "recurring": true, "country": "Canada", "state": "", "source": "..."}]}
```

Create / update body: `{"name", "date": "YYYY-MM-DD", "recurring", "country", "state", "source"}` → returns the holiday. Delete has no body.

---

## 19. `POST /compliance/evaluate`

Rule testing. Two ways to ask:

- **For a real account:** `{"account_id": 15, "channel": "SMS", "at": "2026-11-12T19:30:00Z", "message_text": "optional"}`
- **With made-up facts:** `{"client_id": 3, "channel": "Email", "at": "...", "jurisdiction": {"country": "Canada", "state": "Ontario", "timezone": "America/Toronto"}, "consent": true, "opted_out": false, "flags": ["Disputed Debt"], "contact_history": [{"at": "...", "channel": "Email"}], "balance": 500, "days_past_due": 30, "language": "English", "message_text": "..."}`

Optional for both: `"record": true` to keep the result in the audit log.

```json
{
  "decision": "RESCHEDULE",
  "matches": [
    {
      "rule_id": 12, "rule_code": "CMP-003", "version": 2, "name": "...",
      "action": "Reschedule", "enforcement": "Mandatory",
      "reason_code": "...", "explanation": "...", "layer": "Compliance"
    }
  ],
  "warnings": [],
  "evaluated": ["CMP-001", "CMP-003"],
  "next_permitted_at": "2026-11-13T13:00:00+00:00",
  "requires_review": false,
  "explanation": "...",
  "rule_ids": [12],
  "reason_codes": ["..."]
}
```

- `decision`: `ALLOW`, `BLOCK`, `RESCHEDULE` or `REVIEW_REQUIRED`.
- `evaluated`: the codes of every rule that was checked.
- `layer`: `Compliance`, or `Client Compliance` for a client's own rule.
- A match with `rule_code: "CONTEXT"` means facts are missing (e.g. no jurisdiction or time zone).
- `next_permitted_at` is set when the contact can happen later.

## 20. `GET /compliance/audit`

Operations Admin only. Query (optional): `entity_type` (`rule`, `holiday`, `client`, `account`, ...), `rule_id`, `client_id`, `account_id`, `limit` (default 100, max 500), `offset`.

```json
{"audit": [{"id": 1, "entity_type": "rule", "entity_id": 12, "rule_id": 12, "rule_name": "...", "client_id": null, "account_id": null, "event": "Rule activated", "reason": "...", "before": {}, "after": {}, "approval": "...", "by": "...", "at": "..."}], "total": 120}
```

---

## 21. `GET /clients/{id}/compliance-rules`

Needs `view_client` for the client. The rules in force for the client (from its address: country / state), with whether each is on for this client.

```json
{
  "client_id": 3,
  "jurisdiction": {"country": "Canada", "state": "Ontario", "city": "Toronto", "postal": "M5V 1A1"},
  "jurisdiction_resolved": true,
  "message": null,
  "rules": [{"...": "rule fields", "associated": true, "enabled": true, "in_force": true, "inherited": true, "locked": true}]
}
```

Per rule: `enabled` (on for this client), `in_force` (actually enforced for it), `inherited` (a jurisdiction rule, not the client's own), `locked` (Mandatory: cannot be switched off).

`jurisdiction_resolved: false` + `message` when the client has no usable country yet.

## 22. `POST /clients/{id}/compliance-rules/update`

Body: `{"rule_id": 12, "enabled": false, "reason": "required"}`. A Mandatory rule cannot be disabled (`403`). Needs `modify_governance`.

## 23. `POST /clients/{id}/compliance-rules/sync`

No body. Links the client to the rules of its (possibly changed) jurisdiction. Rules are also linked automatically when a client is saved.

---

## 24. `GET /accounts/{id}/compliance-profile`

```json
{
  "account_id": 15,
  "country": "Canada", "state": "Ontario", "city": "...", "postal": "...",
  "timezone": "America/Toronto",
  "resolved_jurisdiction": {"country": "Canada", "state": "Ontario", "timezone": "America/Toronto"},
  "email_consent": true, "sms_consent": true, "sms_opt_out": false,
  "consent_source": "...", "consent_evidence": "...", "consent_recorded_at": "...",
  "flags": {"Disputed Debt": false, "Cease Communication": false},
  "written_notice_sent_at": null, "last_conversation_at": null,
  "attorney_contact": null,
  "inconvenient_times": [],
  "notes": null
}
```

The `flags` keys are the catalog's `account_flags`.

## 25. `POST /accounts/{id}/compliance-profile/update`

Body: any of `timezone`, `flags` (`{"Disputed Debt": true}`), `sms_consent`, `consent_source`, `consent_evidence`, `consent_recorded_at`, `written_notice_sent_at`, `last_conversation_at`, `attorney_contact`, `inconvenient_times`, `notes`, plus `reason`. Returns the profile (endpoint 24 shape). The change is written to the audit log.

---

## 26–29. Compliance overrides (FR-13)

Authorized, time-bounded exceptions for one Active rule on one account. The evaluation engine still runs first; an **Approved** override may clear a matched restriction only when the rule’s `override_policy` permits it. `Not Permitted` can never be overridden. Default validity after approval: 24 hours (max 7 days).

| # | Endpoint | What it is for |
|---|---|---|
| 26 | `GET /accounts/{id}/compliance-overrides` | List overrides for an account (`status` optional) |
| 27 | `POST /compliance/overrides` | Request: `{account_id, rule_id, reason, evidence, channel?, valid_until?}` |
| 28 | `POST /compliance/overrides/{id}/approve` | Approve Pending (`review_note?`, `valid_until?`). Ops Admin for `Operations Admin With Reason`; Ops Admin or `modify_governance` for `Client Configurable` |
| 29 | `POST /compliance/overrides/{id}/reject` | Reject Pending (`review_note` required) |

`POST /compliance/evaluate` may also return `overrides_applied` and each match may include `override_policy` / `overrideable`.

---

## Sample data

On start-up the backend adds, once: the 25 rule types, the 43 jurisdiction-library rules (Drafts), the 10 sample rules CMP-001…CMP-010 (8 Active, 1 Pending Approval, 1 Draft) and 3 sample holidays. All are illustrative and need legal validation before real use.
