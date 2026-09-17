# Platform Suite API

## One-command run
```bat
.\run.bat
```
Docs: http://127.0.0.1:8000/docs

## IDs
- **Integer (1, 2, 3):** products, organizations, roles, menus, link tables
- **UUID only:** users + auth tokens (login identity)

## Database (main + link)
Main: `users`, `roles`, `organizations`, `products`, `menu_sections`, `menu_items`  
Link: `organization_products`, `user_products`  
Auth: `auth_tokens`, `refresh_tokens`, `token_denylist`

After this ID change: drop/recreate `PayFlowDB`, then restart API so seeder rebuilds tables.
