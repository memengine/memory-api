# Proxy User Security Verification

Date: 2026-03-30

## Scope

This verification covered the proxy-user migration and tenant isolation work:

- proxy user upsert concurrency
- cross-tenant retrieval isolation
- GDPR delete across PostgreSQL and Qdrant
- blocked proxy-user behavior
- plaintext `external_user_id` leakage check in runtime logs
- security test suite

## Migrations Applied

Applied with:

```powershell
osenv\Scripts\alembic upgrade head
```

Latest heads applied during this pass:

- [`add_proxy_users_table.py`](d:\memoryos\memory-api\api\db\migrations\versions\add_proxy_users_table.py)
- [`e4f5a6b7c8d9_repair_legacy_schema_drift.py`](d:\memoryos\memory-api\api\db\migrations\versions\e4f5a6b7c8d9_repair_legacy_schema_drift.py)
- [`f6a7b8c9d0e1_repair_audit_action_enum.py`](d:\memoryos\memory-api\api\db\migrations\versions\f6a7b8c9d0e1_repair_audit_action_enum.py)

## Repair Work Found During Verification

Two real schema-drift issues existed in the live database and had to be repaired before GDPR-delete verification could pass:

1. `users.memory_count` was missing in the running database.
2. `audit_logs.metadata` and the enum value `proxy_user_deleted` were missing in the running database.

These were fixed with the two repair migrations above.

## Qdrant Payload Migration

Helper script added:

- [`migrate_qdrant_payloads.py`](d:\memoryos\memory-api\scripts\migrate_qdrant_payloads.py)

Run:

```powershell
osenv\Scripts\python scripts\migrate_qdrant_payloads.py
```

Result during this pass:

- `No proxy-user-scoped memories found. Nothing to migrate.`

That means the helper is runnable, but there were no existing `proxy_user_id`-backed memory vectors to backfill at the time of execution.

## Verification Results

### 1. Concurrent Upsert

Verification script ran `resolve(tenant_a, "shared-user-123")` 100 times concurrently.

Result:

- `concurrent_resolve_unique_ids = 1`
- `concurrent_resolve_row_count = 1`

Interpretation:

- exactly one `proxy_users` row was created
- no duplicate rows were created
- no deadlock occurred during the 100 concurrent upserts

This also covers the “50 parallel requests” requirement, since the stronger 100-request case passed.

### 2. Cross-Tenant Isolation

Setup:

- created memory for `tenant_a / shared-user-123`
- queried retrieve path as `tenant_b / shared-user-123`

Result:

- `cross_tenant_results = 0`

Interpretation:

- same external user id across tenants does not leak memories across tenants
- retrieval returns empty results, not a permission error

### 3. GDPR Delete

Setup:

- created one Postgres memory row and one matching Qdrant vector for `tenant_a / shared-user-123`
- called proxy-user deletion path

Result:

- `gdpr_deleted_count = 1`
- `gdpr_db_remaining = 0`
- `gdpr_qdrant_remaining = 0`

Interpretation:

- proxy-user GDPR delete removed the memory from PostgreSQL
- matching vector was removed from Qdrant

### 4. Blocked Proxy User

Setup:

- resolved a proxy user
- blocked that proxy user
- attempted to resolve again

Result:

- `blocked_flag_set = True`
- `blocked_status_code = 403`

Interpretation:

- blocked proxy users correctly raise `ProxyUserBlockedError`
- current behavior maps to HTTP `403`

### 5. Plaintext `external_user_id` Log Leakage

Checked runtime container logs for the exact verification ids:

- `shared-user-123`
- `blocked-user-456`

Command used:

```powershell
docker compose logs api celery-worker | Select-String -Pattern 'shared-user-123|blocked-user-456'
```

Result:

- no matches

Repository logging scan also found no logger call that directly logs `external_user_id` plaintext.

### 6. Security Test Suite

Run:

```powershell
osenv\Scripts\python -m pytest tests\security\test_cross_tenant_isolation.py tests\unit\test_proxy_user_service.py
```

Result:

- `6 passed`

## Manual Supabase RLS Step Still Required

This part is still manual and was not applied from the repo:

```sql
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON memories
USING (
  proxy_user_id IN (
    SELECT id
    FROM proxy_users
    WHERE tenant_id = current_setting('app.tenant_id')::uuid
  )
);
```

Note:

- this is defence-in-depth only
- application-level filtering by `tenant_id + proxy_user_id` remains the primary isolation mechanism

## Overall Status

Passed:

- concurrent upsert uniqueness
- cross-tenant empty retrieval
- GDPR delete in Postgres and Qdrant
- blocked proxy-user behavior
- runtime log leakage check
- security tests
- Qdrant payload migration helper exists and runs

Still manual:

- enable Supabase row-level security in the SQL editor
