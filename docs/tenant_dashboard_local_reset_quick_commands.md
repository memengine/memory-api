# Tenant Dashboard Local Reset Quick Commands

Use this when you want a clean local reset for tenant dashboard testing without reading the full verification runbook.

## 1. Stop and wipe local state

```powershell
docker compose down -v
docker compose up -d --build
```

Expected:
- PostgreSQL, Redis, Qdrant, and LocalStack local state is wiped
- containers start fresh

## 2. Apply migrations

```powershell
osenv\Scripts\python -m alembic upgrade head
```

Expected:
- database schema is at `head`

## 3. Create a fresh tenant and first API key

```powershell
osenv\Scripts\python scripts\create_tenant.py "My Local Tenant"
```

Expected:
- terminal prints:
  - tenant id
  - raw API key
- save the raw API key immediately

## 4. Map Clerk org to the tenant

SQL:

```sql
UPDATE tenants
SET clerk_org_id = 'org_your_clerk_org_id'
WHERE id = '<your_new_tenant_uuid>';
```

Expected:
- tenant dashboard auth can resolve the signed-in Clerk org to the tenant

## 5. Restart and sign in

Tenant app:

```powershell
cd d:\memoryos\memory-dashboard\tenant
npm run dev
```

Expected:
- app opens at `http://localhost:3000`

Then:
1. sign out if already signed in
2. sign back in under the correct Clerk organization
3. reload the dashboard

## 6. Generate useful data

Use the printed tenant API key and make real `add()` calls.

Expected:
- proxy users are created automatically
- memories are created
- quality logs appear
- dashboard charts and tables become useful

## 7. Optional larger seed

```powershell
osenv\Scripts\python scripts\seed_memories.py
```

Expected:
- extra local memory data is added for deeper testing

## Minimum things you need after a reset

1. One tenant
2. One tenant API key
3. One `clerk_org_id` mapped to that tenant
4. Some real add/memory activity if you want non-empty charts

