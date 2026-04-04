# Proxy User + RLS Verification

Date: 2026-03-30

## Applied

- Alembic head applied successfully:
  - `0b1c2d3e4f5a_enforce_memories_proxy_user_id_not_null`
- `memories.proxy_user_id` verified as `NOT NULL`
- Qdrant payload migration script executed:
  - migrated payloads: `1`
  - skipped missing Qdrant points: `10004`

## Passed

- Concurrent upsert `resolve(tenant, user)` 100 times:
  - unique proxy user ids returned: `1`
  - rows created in `proxy_users`: `1`
- Cross-tenant isolation:
  - Tenant A memory queried using Tenant B + same `external_user_id`
  - results returned: `0`
- GDPR delete:
  - deleted from PostgreSQL: `1`
  - remaining DB rows: `0`
  - remaining proxy user row: `0`
  - remaining Qdrant points for that proxy user: `0`
- Blocked proxy user:
  - block call succeeded: `true`
  - subsequent resolve raised `403`
- Plaintext hash logging check:
  - `docker compose logs api celery-worker | Select-String ...`
  - matches found: `0`
- Security test:
  - `pytest tests/security/test_cross_tenant_isolation.py`
  - result: `2 passed`

## Manual Remaining

Supabase RLS still must be applied manually in the SQL editor:

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

## Important Note

The Qdrant payload migration completed safely, but the large skipped count means many database `memories` rows do not currently have matching Qdrant points. That is not a failure of the migration script itself; it indicates historical DB/Qdrant drift in existing data.
