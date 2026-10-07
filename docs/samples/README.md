# ProductSuite-BE — samples, recent changes & how to start

This folder holds **daily account import** sample data. Client import samples are uploaded from the FE (CSV/XLSX); account template Excel is regenerated on API startup.

---

## How to start the application

Run **backend first**, then the frontend (`ProductSuite-FE`).

### Prerequisites

- Python **3.11+** (venv recommended)
- PostgreSQL with a database (default name `PayFlowDB`)
- Copy `.env.example` → `.env` and set `DATABASE_URL`, JWT, and seed admin if needed

### Backend (this repo)

From `ProductSuite-BE/`:

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

| Item | Value |
|------|--------|
| API | http://127.0.0.1:8000 |
| Swagger | http://127.0.0.1:8000/docs |
| Health | http://127.0.0.1:8000/health |

On startup the API runs **additive schema sync + seeder** (no Alembic). It also writes `docs/samples/payflow_daily_accounts_sample.xlsx`.

Default seeded suite admin (override via `.env`): see `SEED_SUPER_ADMIN_*` in `.env.example`.

### Frontend (sibling repo)

From `ProductSuite-FE/` (see that repo’s `docs/samples/README.md` and root `README.md`):

```bat
npm install
npm run dev
```

| Item | Value |
|------|--------|
| UI | http://localhost:5173 |
| API proxy | `/api` → `http://127.0.0.1:8000` (`VITE_API_BASE_URL=/api`) |

---

## Daily CRM account sample

`payflow_daily_accounts_sample.xlsx` is written on API startup (schema seed) and is the same file as **Download sample template** on Accounts → Upload Daily CRM File.

Use seed client codes `paypal`, `canadian-tire`, `northstar-utilities` and portfolio codes `paypal-loans`, `paypal-finance`, `ct-triangle`, `ns-residential`.

The workbook includes valid refresh rows, new accounts, and intentional errors (unknown client, missing email, negative balance, duplicate account_id).

Optional segmentation columns: `employment_status`, `income_band`, `education_level`, `customer_segment` (ingest also accepts aliases `employment_type` and `persona`).

Also included: `crm_case_id`, `debtor_id`, `client_reference_number`, required `product_code`, `date_listed`, `last_email_sent_date`, `last_sms_sent_date`, `last_contact_date`, `provincial_hold` (alias `communication_hold`), `hold_days`, `email_consent` (alias `contact_permission`), `source_updated_at`.

---

## Recent product changes (clients / mapping / geo / imports)

Documented here so FE + BE stay aligned. Deeper design notes: root `ARCHITECTURE.md`.

### Clients & hierarchy

- CRM-style hierarchy: **master client** (`is_master_client`) and **sub-clients** (`master_client__client_number`).
- Shared **upsert** path for UI create and CSV/XLSX **client import**.
- Primary profile fields (contact, address, CRM numbers) aligned between create form and import.
- **Data source:** onboarding is **file-first** (`data_source_type=file`). Per-client CRM “Data Mapping” tab removed from create/detail UX.
- Activation no longer blocked on per-client field mappings for file-mode clients.

### System Mapping (global CRM catalog)

- Menu label **System Mapping** (route still under integrations / `system-mapping`).
- One global **CRM → PayFlow** inbound catalog from `app/modules/payflow/crm_catalog.py` (`CRM_INBOUND_FIELDS`).
- `loan_identifier` is an **inbound payload id** (`LOAN_IDENTIFIER` → `loan_identifier`, required, available Yes) — not a “missing from placement” blocker. Same id is used on outbound REMIT.

### Imports

- Client import + account (daily) import with persisted **import runs** (`/payflow/imports`, validate/upload endpoints).
- Account template download: `GET /payflow/imports/accounts/template`.

### Address / geo cascade

- Client address UI: searchable **Country → Province/State → City**, plus language/currency defaults.
- Browser never calls public geo APIs directly (CORS). FE calls:
  - `GET /payflow/geo/countries` — **bundled** country list (offline-safe)
  - `GET /payflow/geo/states?country=` — proxies [countriesnow.space](https://countriesnow.space)
  - `GET /payflow/geo/cities?country=&state=` — same provider
- Implementation: `app/modules/payflow/services/geo_service.py`.

### Soft delete / deletion logs

- Soft deletes and `POST …/delete` patterns; deletion audit via `/payflow/deletion-logs` (and platform equivalent where wired).

### API convention reminder

- PayFlow **create/update/delete-style actions** use **POST** (e.g. `POST /payflow/clients/{id}/update`), not PATCH/PUT.

---

## Related docs

| Doc | Location |
|-----|----------|
| Architecture | `../../ARCHITECTURE.md` |
| Root quick start | `../../README.md` |
| FE counterpart | `ProductSuite-FE/docs/samples/README.md` |
