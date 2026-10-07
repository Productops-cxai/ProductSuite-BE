# Platform Suite API (ProductSuite-BE)

FastAPI backend for **Platform Suite** (identity + product entitlement) and **PayFlow** (collections operations).

## How to start

### Prerequisites

- Python 3.11+
- PostgreSQL (create DB, e.g. `PayFlowDB`)
- Copy `.env.example` → `.env` and set `DATABASE_URL` / seed admin as needed

### Run API

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

| | |
|--|--|
| API | http://127.0.0.1:8000 |
| Swagger | http://127.0.0.1:8000/docs |

Startup runs **additive schema sync + seeder** (no Alembic migrations).

Then start the FE from `ProductSuite-FE/` (`npm install` → `npm run dev` → http://localhost:5173). Full notes: [`docs/samples/README.md`](docs/samples/README.md).

Default admin: `SEED_SUPER_ADMIN_*` in `.env` / `.env.example`.

## IDs

- **Integer (1, 2, 3):** products, organizations, roles, menus, link tables, PayFlow entities
- **UUID only:** users + auth tokens (login identity)

## Database (main + link)

Main: `users`, `roles`, `organizations`, `products`, `menu_sections`, `menu_items`  
Link: `organization_products`, `user_products`  
Auth: `auth_tokens`, `refresh_tokens`, `token_denylist`

After a breaking ID/schema change: drop/recreate `PayFlowDB`, then restart API so seeder rebuilds tables.

## Docs

| Doc | Purpose |
|-----|---------|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Modular monolith layout, modules, conventions |
| [`docs/samples/README.md`](docs/samples/README.md) | Samples, **recent PayFlow changes**, start guide |
| `.cursor/rules/api-post-updates.mdc` | Prefer **POST** for PayFlow create/update |

## API conventions (PayFlow)

- **GET** — reads  
- **POST** — create, update (`…/update`), soft delete (`…/delete`), and actions (`…/activate`, etc.)  
- Do **not** use PATCH/PUT for PayFlow updates unless explicitly required  
