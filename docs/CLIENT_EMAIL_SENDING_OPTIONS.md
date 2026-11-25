# Sending Email From the Client’s Address — Simple Options

**Chosen path for build:** Option **2 (DNS + SendGrid)** — see [SENDGRID_DNS_MULTI_EMAIL_WORK_BREAKDOWN.md](./SENDGRID_DNS_MULTI_EMAIL_WORK_BREAKDOWN.md).

**Goal:** Send collection emails so they appear to come from the client’s email (e.g. `collections@clientx.com`), and get delivery tracking where possible.

**Important:** PayFlow does **not** create the client’s mailbox. The client must already have Microsoft/Google/email, or authorize PayFlow via DNS.

---

## Option 1 — Mailbox Connect (OAuth)

The client signs in once with Microsoft/Google and allows PayFlow to send from that mailbox. Mail goes out through their Outlook/Gmail.

| Pros | Concerns |
|---|---|
| No DNS change needed | Weak delivery tracking (no SendGrid-style Delivered/Bounce) |
| Relatively easy setup (one-time sign-in) | Daily send limits (mailbox limits; ~1,000/day is usually fine) |
| Real client From address | Connection can break (password change / revoke) → reconnect needed |
| No password stored by PayFlow | Client/IT must complete login/consent once |

**Best when:** You want to avoid DNS, volume is medium, and tracking is secondary.

---

## Option 2 — Domain Authentication (DNS + SendGrid)

The client adds a few DNS records. PayFlow then sends via SendGrid using the client’s email address.

| Pros | Concerns |
|---|---|
| Real client From address | Requires DNS changes from client IT |
| Full tracking (Delivered, Bounce, etc.) | Setup can be more technical / slower |
| Strong for high volume | Each client domain must be verified separately |
| No mailbox login required | |

**Best when:** Tracking matters, volume is high, and IT can update DNS.

---

## Option 3 — SMTP (Client mail server credentials)

The client provides SMTP host/username/password (or app password); PayFlow sends through it.

| Pros | Concerns |
|---|---|
| Real client From address | Handling passwords/secrets is a security risk |
| Different path from DNS/OAuth | Tracking is usually weak |
| | Higher security and support burden |
| | Most modern orgs prefer not to share SMTP credentials |

**Best when:** Rare/legacy cases only. Not recommended if OAuth or DNS is available.

---

## Option 4 — Soft Branding Only

Mail still sends from the PayFlow/platform address; display name and/or Reply-To use the client’s details.

| Pros | Concerns |
|---|---|
| No DNS, no mailbox connect | Not a true client From address |
| Full tracking (SendGrid) | Customers see the platform/operator email |
| Can start immediately | Weaker brand presence |

**Best when:** You need to go live quickly, or the client cannot do connect/DNS yet.

---

## Side-by-side (simple)

| | Client From | Tracking | Client effort | Volume |
|---|---|---|---|---|
| **1. OAuth mailbox** | Yes | Weak | Low (1× login) | Medium |
| **2. DNS + SendGrid** | Yes | Strong | Medium (IT/DNS) | High |
| **3. SMTP** | Yes | Weak | Medium + security risk | Medium |
| **4. Soft branding** | No | Strong | None | High |

---

## Simple recommendation

- **Need tracking + client From** → Option **2 (DNS)**
- **No DNS, but need client From** → Option **1 (OAuth)**
- **Just need to run now** → Option **4 (Soft branding)**
- **SMTP** → last resort

> Note: Outside OAuth, SMTP, and DNS, there is no separate clean option that gives **true client From + full tracking** together.

---

## Open product questions

1. Is tracking more important, or avoiding DNS?
2. If the client does not use the app, who completes OAuth consent (IT one-time)?
3. First ship: OAuth, DNS, or both in phases?
