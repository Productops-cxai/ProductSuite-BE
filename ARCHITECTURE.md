# ProductSuite-BE — Architecture

Backend for **Platform Suite** (identity + product entitlement) and **PayFlow** (collections operations product).

**Root:** `ProductSuite-BE/`

---

## 1. Overview

This is a **modular monolith**:

| Concern | Module | API surface |
|---------|--------|-------------|
| Login, JWT, activate, profile | `identity` | `/auth/*` |
| Super-admin catalog & entitlements | `platform` | `/products`, `/people`, `/platform/*`, … |
| Collections product APIs | `payflow` | `/payflow/*` |

Shared pieces:

- One PostgreSQL database
- One JWT identity
- Shared enums + ORM models
- Services talk to SQLAlchemy **directly** (no separate repository layer)
- No Alembic — schema is synced additively on startup

---

## 2. Tech stack

| Layer | Choice |
|--------|--------|
| Framework | **FastAPI** |
| Server | **Uvicorn** |
| ORM | **SQLAlchemy 2.0** (`Mapped` / `mapped_column`) |
| Database | **PostgreSQL** (`psycopg2`) |
| Settings | **pydantic-settings** + `.env` |
| DTOs | **Pydantic v2** |
| Auth | **python-jose** (JWT HS256) + **passlib/bcrypt** |
| Uploads | **python-multipart** + static `/uploads` |
| Excel / CSV | **openpyxl** (+ CSV for client import) |
| HTTP client | **httpx** (geo provider proxy) |
| Email | Optional SMTP; always logged in `email_logs` |
| Tests | **pytest** |

---

## 3. Directory structure

```text
ProductSuite-BE/
├── .env / .env.example
├── requirements.txt
├── pytest.ini
├── README.md
├── ARCHITECTURE.md          ← this file
├── API_FLOW.md
├── .cursor/rules/api-post-updates.mdc
├── docs/samples/            # daily account sample + start/changelog notes
├── uploads/                 # avatars, client logos, client-imports (runtime)
├── app/
│   ├── main.py              # FastAPI app entry
│   ├── core/                # config, deps, security, exceptions
│   ├── shared/              # shared enums + soft-delete helpers
│   ├── infrastructure/
│   │   ├── database/        # session, models, schema sync, seeder
│   │   └── email/           # send_email + email_logs
│   └── modules/
│       ├── identity/        # /auth
│       ├── platform/        # suite admin + product enter
│       └── payflow/         # /payflow product APIs
└── tests/
```

---

## 4. How the app boots

**Entry:** `app/main.py` → typically `uvicorn app.main:app`

### Boot sequence

1. **Lifespan** runs `run_seeder()`:
   - Additive schema sync (`schema_sync.py`)
   - Seed roles, products, menus, super admin, demo PayFlow data
2. Create **FastAPI** app (title from `APP_NAME`, Swagger auth)
3. Attach **CORS** (`allow_origins=["*"]`)
4. Ensure `uploads/`, `uploads/avatars/`, `uploads/client-logos/` exist
5. Mount static files at `/uploads`
6. Register routers:
   - `identity` → `/auth`
   - `platform` → platform paths (no common prefix)
   - `payflow` → `/payflow`
7. Health: `GET /health`

There is **no** custom auth middleware. JWT is enforced via FastAPI `Depends`.

---

## 5. Layered architecture

```text
HTTP Request
    ↓
Router (routes.py)          ← thin: validate input, call service, map errors
    ↓
Service (service.py / services/*)
    ↓
SQLAlchemy models + Session
    ↓
PostgreSQL
```

| Layer | Responsibility |
|-------|----------------|
| **Router** | HTTP paths, status codes, `Depends` for auth |
| **Schema** | Pydantic request/response DTOs |
| **Service** | Business logic + DB queries |
| **Model** | ORM tables |
| **Core** | Config, JWT, password hashing, shared deps |
| **Infrastructure** | DB session, schema sync, seeder, email |

Error flow: services raise `AppError` subclasses → routers map `code` → HTTP 401/403/404/409/400.

---

## 6. File-by-file: core & infrastructure

### `app/core/`

| File | What it does |
|------|----------------|
| `config.py` | `Settings` from env: DB URL, JWT, TTLs, seed admin, SMTP, `FRONTEND_URL` |
| `deps.py` | `get_current_user`, `require_platform_super_admin`, `require_product_access`, typed aliases (`CurrentUser`, `SuperAdmin`, `DbSession`) |
| `security.py` | bcrypt hash/verify, access/refresh JWT create/decode, password policy, `as_uuid` |
| `exceptions.py` | `AppError`, `NotFoundError`, `ConflictError`, `UnauthorizedError`, `ForbiddenError`, `ValidationAppError` |

### `app/shared/`

| File | What it does |
|------|----------------|
| `enums.py` | Shared string enums: product/user status, platform & PayFlow roles, client/CRM/integration/review/rule/strategy/comm/notification statuses, auth token types, `LoginNextStep`, `MenuContext` |

### `app/infrastructure/database/`

| File | What it does |
|------|----------------|
| `session.py` | Engine, `SessionLocal`, `Base`, `get_db()` generator |
| `models/__init__.py` | **All** ORM models (platform + PayFlow + auth) in one place |
| `schema_sync.py` | Renames legacy tables/columns, `create_all`, adds missing columns additively |
| `seeder.py` | Seeds roles, orgs, products, menus, super admin, PayFlow permissions/roles/demo data |
| `seed_cli.py` | CLI: `python -m app.infrastructure.database.seed_cli` |

### `app/infrastructure/email/`

| File | What it does |
|------|----------------|
| `service.py` | `send_email`: always writes `email_logs`; sends via SMTP only if `SMTP_HOST` is set |

---

## 7. Module: Identity (`/auth`)

**Folder:** `app/modules/identity/`

| File | What it does |
|------|----------------|
| `routes.py` | HTTP endpoints under `/auth` |
| `schemas.py` | Login, me, tokens, activate, reset, profile DTOs |
| `service.py` | `IdentityService`: login, JWT lifecycle, activate, password reset, invite emails, avatar I/O |

### Main flows

| Flow | Endpoints / behavior |
|------|----------------------|
| Login | `POST /auth/login` → access + refresh JWT + `products` + `next_step` |
| Me | `GET /auth/me` → current user + products |
| Refresh | `POST /auth/refresh` |
| Logout | Revokes access JTI (denylist) + optional refresh via `X-Refresh-Token` |
| Activate | Invite email → `GET /auth/activation/{token}` → `POST /auth/activate` |
| Reset password | `POST /auth/forgot-password` → preview → `POST /auth/reset-password` |
| Profile | `PATCH /auth/me` (name), avatar upload, change password |

### Login `next_step`

| Condition | `next_step` |
|-----------|-------------|
| Platform super admin | `platform_admin` |
| 0 products | `no_access` |
| Exactly 1 product | `direct_entry` |
| Multiple products | `product_selection` |

---

## 8. Module: Platform (suite admin)

**Folder:** `app/modules/platform/`

| File | What it does |
|------|----------------|
| `routes.py` | Admin + launcher APIs (overview, products, orgs, access, people, menus, enter product) |
| `schemas.py` | Product/org/people/access DTOs |
| `service.py` | `PlatformService`: CRUD, grant/revoke, invites, enter-product |
| `services/entitlement_service.py` | Effective products = assigned ∩ org-granted ∩ product active |

### Effective product access

```text
user_product_assignments
  ∩ organization_product_entitlements (status = granted)
  ∩ products.status = active
```

### Typical admin surfaces

- Products catalog
- Organizations
- Product access (grant/revoke org entitlements)
- People (invite, assign products, resend invite)
- Menus
- Email logs
- `POST /products/{code}/enter` — gate before using a product API

---

## 9. Module: PayFlow (`/payflow`)

**Folder:** `app/modules/payflow/`

### Top-level files

| File | What it does |
|------|----------------|
| `routes.py` | All `/payflow/*` HTTP routes (thin controllers) |
| `schemas.py` | Large Pydantic surface for every PayFlow domain |
| `deps.py` | `RequirePayflowProduct`, `PayflowOpsAdmin`, `PayflowAccessContext` |
| `membership.py` | Ensure ops-admin membership when PAYFLOW assigned; role helpers |
| `crm_catalog.py` | Global CRM inbound/outbound catalog (**System Mapping**); includes payload ids such as `loan_identifier` |
| `service.py` | `PayflowUserService`: PayFlow users + custom roles/permissions + deletion logs |

### Services (`services/`)

| File | What it does |
|------|----------------|
| `access_context_service.py` | Membership, permission codes, client visibility, PayFlow menus |
| `client_service.py` | Clients (master/sub hierarchy), portfolios, logos, mapping catalog sync, client CSV/XLSX import, activation blockers, supervisors; file-first `data_source_type` |
| `account_service.py` | Collection accounts list/detail (scoped by access) |
| `import_service.py` | Daily account Excel validate/upload, import runs, sample template generation |
| `geo_service.py` | Country list (bundled) + states/cities via countriesnow.space proxy |
| `dashboard_service.py` | KPI / funnel / attention / activity dashboard |
| `integration_service.py` | Legacy client-derived integration cards; global catalog is preferred for System Mapping |
| `review_service.py` | Human review queue + approve/modify/reject/hold |
| `rule_service.py` | Governance rules catalog + CRUD activate/deactivate |
| `strategy_service.py` | Workflows/strategies create/update/draft/approve/reject |
| `communication_service.py` | Communications list/detail (scoped) |
| `notification_service.py` | In-app notifications create/list/mark-read |

### Authorization layers (PayFlow)

| Dependency | Checks |
|------------|--------|
| `RequirePayflowProduct` | Effective entitlement for product code `PAYFLOW` |
| `PayflowOpsAdmin` | Product access + operations_admin membership |
| `PayflowAccessContext` | Builds role / permissions / visible clients (403 if no membership) |

---

## 10. API routing conventions

### Router organization

| Module | Prefix / style |
|--------|----------------|
| Identity | `APIRouter(prefix="/auth")` |
| Platform | No single prefix — paths like `/products`, `/people`, `/platform/...` |
| PayFlow | `APIRouter(prefix="/payflow")` |

### HTTP verbs (important)

- **GET** — reads / lists
- **POST** — create **and** update / actions
  - Examples: `POST /payflow/roles/{id}/update`, `POST /payflow/clients/{id}/update`
  - Soft delete / deactivate: `POST .../delete`, `POST .../deactivate`
  - Actions: `.../activate`, `.../approve`, `.../reject`
- Platform upsert: `POST /products`, `POST /people` with optional `id`
- Exception: `PATCH /auth/me` for profile name only

Workspace rule: prefer **POST** for updates under PayFlow; do not use PATCH/PUT unless explicitly requested.

---

## 11. Database model overview

**ID policy:** integers for products/orgs/roles/menus/PayFlow entities; **UUID** for `users` and auth token tables.

### Platform relationships

```text
platform_roles 1──* users *──1 organizations
products
  ↑ org:  organization_product_entitlements (granted|revoked)
  ↑ user: user_product_assignments
navigation_sections 1──* navigation_items
users 1──0..1 payflow_user_memberships
```

### PayFlow relationships

```text
payflow_roles 1──* payflow_role_permissions *──1 payflow_permissions
payflow_roles 1──* payflow_user_memberships
  memberships 1──* payflow_user_client_assignments
    assignments *──1 payflow_clients
    assignments 1──* payflow_user_client_permissions

payflow_clients 1──* portfolios, accounts, rules, reviews, strategies, communications, field_mappings
```

### Auth / ops tables

| Table | Purpose |
|-------|---------|
| `auth_tokens` | Activation / password-reset (hashed) |
| `refresh_tokens` | Refresh JTIs per user |
| `token_denylist` | Revoked access JTIs |
| `email_logs` | Outbound mail archive |
| `payflow_notifications` | In-app inbox |

---

## 12. Auth & JWT (detail)

### Token model

| Token | Contents | TTL setting |
|-------|----------|-------------|
| Access | `sub` = user UUID, `type=access`, `jti`, `email`, `role`, `org_id` | `JWT_ACCESS_EXPIRE_MINUTES` |
| Refresh | `type=refresh`, `jti` stored in DB | `JWT_REFRESH_EXPIRE_DAYS` |

Algorithm: `JWT_ALGORITHM` (HS256) with `JWT_SECRET`.

### `get_current_user` steps

1. Require `Authorization: Bearer <token>`
2. Decode; require `type == access`
3. If `jti` in denylist → 401
4. Load user; require `status == active`

### Activate / reset tokens

- Stored hashed (SHA-256) in `auth_tokens`
- TTLs: `ACTIVATION_TOKEN_EXPIRE_MINUTES`, `PASSWORD_RESET_EXPIRE_MINUTES`
- Email links use `FRONTEND_URL` (e.g. `/activate?token=…`)

---

## 13. End-to-end flows

### Login → product entry

```text
POST /auth/login
  → JWT + products + next_step

Platform admin → platform menus / products / people / access

Invite: POST /people → email_logs + activation_link
  → GET /auth/activation/{token}
  → POST /auth/activate
  → login

User: GET /me/products
  → POST /products/PAYFLOW/enter
  → /payflow/* (RequirePayflowProduct + membership/permissions)
```

### System Mapping & imports note

- **System Mapping** (FE `/payflow/system-mapping`) reads `GET /payflow/clients/mapping-catalog` → `CRM_INBOUND_FIELDS` / `CRM_OUTBOUND_FIELDS` in `crm_catalog.py`. This is the **global** CRM→PayFlow catalog; per-client Data Mapping UI was removed for file-first onboarding.
- **Client hierarchy:** master vs sub-client via `is_master_client` + `master_client__client_number`; shared upsert for UI create and client import.
- **Account imports:** `import_service.py` + `/payflow/imports/*` (template, validate, upload, run history). Sample workbook under `docs/samples/`.
- **Geo:** `/payflow/geo/countries|states|cities` — countries bundled; provinces/cities proxied to countriesnow (avoids browser CORS). No Google geo dependency.
- `PayflowIntegrationService` still builds optional client-derived connector cards; there is no live third-party CRM connector package in-repo yet.

---

## 14. Environment variables (names only)

| Key | Purpose |
|-----|---------|
| `DATABASE_URL` | PostgreSQL SQLAlchemy URL |
| `APP_NAME` | API title |
| `APP_ENV` | Environment name |
| `DEBUG` | Debug flag |
| `JWT_SECRET` | Signing secret |
| `JWT_ALGORITHM` | e.g. HS256 |
| `JWT_ACCESS_EXPIRE_MINUTES` | Access token TTL |
| `JWT_REFRESH_EXPIRE_DAYS` | Refresh token TTL |
| `PASSWORD_RESET_EXPIRE_MINUTES` | Reset link TTL |
| `ACTIVATION_TOKEN_EXPIRE_MINUTES` | Invite link TTL |
| `FRONTEND_URL` | Base URL for email action links |
| `PASSWORD_MIN_LENGTH` | Password policy |
| `SEED_SUPER_ADMIN_EMAIL` | Seeded suite admin |
| `SEED_SUPER_ADMIN_PASSWORD` | Seeded admin password |
| `SEED_SUPER_ADMIN_NAME` | Seeded admin display name |
| `SMTP_HOST` | If empty → log-only email |
| `SMTP_PORT` | SMTP port |
| `SMTP_USER` / `SMTP_PASSWORD` | SMTP auth |
| `SMTP_FROM` | From address |
| `SMTP_USE_TLS` | STARTTLS flag |

---

## 15. Architectural decisions (summary)

1. **Modular monolith** — three routers, one DB, one identity.
2. **No repository layer** — services own queries.
3. **No Alembic** — additive `schema_sync` + seeder on startup.
4. **Two role systems** — `platform_roles` vs `payflow_roles` (+ client-scoped permissions).
5. **POST-for-updates** is intentional under `/payflow`.
6. **Demo-heavy seeder** — seeds clients, accounts, rules, reviews, strategies, communications, notifications for UI development.
7. **Entitlement gate** — org granted + user assigned + product active, then PayFlow membership for in-product RBAC.
8. **File-first clients** — daily file / import is the primary onboarding path; System Mapping is global, not per-client.
9. **Geo via BE proxy** — public geo APIs are never called from the browser.

Operational start + recent changelog: `docs/samples/README.md`.
