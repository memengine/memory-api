# Region Assignment Runbook

This runbook explains how region assignment works in Phase 1 and what an operator should do if a tenant asks for a different data region.

## Current Phase

Phase 1 makes the application region-aware in code, but the live infrastructure is still effectively single-region.

Current state:
- `IN1` is the default and only safe tenant region right now
- `IN1` maps to AWS `ap-south-1`
- `EU1` and `US1` rows exist in the database for future rollout preparation
- `EU1` and `US1` should **not** receive tenants yet because their regional PostgreSQL, Redis, Qdrant, and worker infrastructure are not deployed

## Region Assignment Rule

Phase 1 rule:
- All tenants default to `IN1`
- New tenants should stay on `IN1`
- Do not assign tenants to `EU1` or `US1` until the regional infrastructure exists and has been verified

## Default Behavior

When a tenant is created:
- `tenants.region_id` defaults to `IN1`
- request routing will use `IN1`
- tenant region is cached in Redis under:
  - `tenant:<tenant_uuid>:region`

That means a direct database update will not take effect for live traffic until the cache entry is removed or expires.

## If a Tenant Requests a Region Change

Important:
- changing `tenants.region_id` is only safe when the destination region has real infrastructure deployed
- region change is an enterprise migration event, not a casual setting change

### Current Phase 1 operator rule

If a tenant asks for `EU1` or `US1` now:
- do **not** assign them yet
- record the request
- wait for the later infrastructure phase where those regions are actually deployed

## Manual Region Assignment Procedure

Only use this when the target region infrastructure is ready.

### Step 1: Update the tenant row

Run:

```sql
UPDATE tenants
SET region_id = 'EU1'
WHERE id = '<tenant_uuid>';
```

Replace:
- `EU1` with the real target region
- `<tenant_uuid>` with the tenant id

### Step 2: Invalidate the tenant region cache

Run:

```bash
redis-cli DEL tenant:<tenant_uuid>:region
```

Why this is required:
- `RegionMiddleware` caches the tenant region for 3600 seconds
- if you skip cache invalidation, the API may keep routing that tenant to the old region until the cache expires

### Step 3: Verify the next request picks up the new region

Expected behavior on the next authenticated tenant request:
- cache miss happens
- the middleware reads `tenants.region_id`
- the new region value is cached
- downstream services use the new region connection bundle

## What Happens Internally

After cache invalidation, the system handles the rest:
- `AuthMiddleware` resolves the tenant
- `RegionMiddleware` loads the tenant region
- `request.state.region_id` is attached
- DB, Redis, and Qdrant dependencies resolve through `RegionConnectionPool`
- downstream services use the correct region-specific clients

No code change is needed for an assignment itself.

## What Not To Do

Do not:
- assign a tenant to `EU1` or `US1` in Phase 1
- change `region_id` without clearing the Redis cache
- assume a DB update alone is enough for live traffic
- treat region change as reversible runtime config without a data migration plan

## Phase 1 Safe Summary

Safe now:
- keep all tenants on `IN1`
- prepare region metadata
- keep the code region-aware

Not safe yet:
- routing live tenant traffic to `EU1`
- routing live tenant traffic to `US1`

Use this exact Phase 1 rule:
- All tenants default to `IN1` (`ap-south-1`)
- To assign a tenant to a different region later:
  - `UPDATE tenants SET region_id = 'EU1' WHERE id = '<tenant_uuid>';`
  - `redis-cli DEL tenant:<tenant_uuid>:region`
- `EU1` and `US1` have no real infrastructure yet, so do not assign tenants to them until the later deployment phase is complete



⚠	ENGINEER MANUAL — Compliance steps for EU1 launch (future)
Before assigning any EU tenant: verify Qdrant EU1 cluster is deployed in eu-west-1 (not ap-south-1)
Verify PostgreSQL RDS instance is in eu-west-1 with automated backups to eu-west-1 only
Verify Redis ElastiCache is in eu-west-1
Verify OpenAI API calls from EU1 workers are not routed through non-EU infrastructure (use OpenAI EU endpoint)
Have legal review the DPA (Data Processing Agreement) for all EU1 sub-processors (OpenAI, Qdrant Cloud, AWS)
Document sub-processors in privacy policy before signing first EU enterprise contract
