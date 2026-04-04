# Quota Alert Live Verification

Date: 2026-03-30

Tenant used:
- `487990e8-797a-431a-91c8-61f6a8f4db0a`

Webhook endpoint used:
- `https://webhook.site/3973a04d-cf3b-4535-868d-a7d75a54a2c4`

## What Was Verified

### 1. FULL -> PASSTHROUGH transition
- Forced the tenant budget to:
  - `current_month_calls = monthly_call_limit`
  - `overage_policy = warn`
  - `last_notified_mode = FULL`
- Cleared the cached quota envelope.
- Called `QuotaManager.get_quota_envelope(tenant_id)`.
- Result:
  - `mode = PASSTHROUGH`
  - `budget_remaining_pct = 0.0`

### 2. Real webhook delivery
- Webhook.site received exactly one POST request for the transition.
- Delivery timestamp:
  - `2026-03-30 14:10:28`
- Latest request UUID:
  - `645fd30e-488b-4f58-8cf1-7680c889a92d`

Payload received:

```json
{
  "event": "quota_mode_changed",
  "tenant_id": "487990e8-797a-431a-91c8-61f6a8f4db0a",
  "from_mode": "FULL",
  "to_mode": "PASSTHROUGH",
  "reset_at": null,
  "upgrade_url": "https://memoryos.io/pricing"
}
```

### 3. Alert idempotency
- Ran 100 more quota evaluations after the tenant was already in `PASSTHROUGH`.
- Queried Webhook.site again.
- Result:
  - request count remained `1`
  - no duplicate alert was sent

## Fixes Made During Verification

### A. QuotaManager dispatch wiring
- Problem:
  - live app dependency construction created `QuotaManager` without `dispatch_task`
  - mode transitions would never enqueue/send alerts
- Fix:
  - wired `QuotaManager` to `celery_app.send_task`
- File:
  - [`api/dependencies.py`](d:\memoryos\memory-api\api\dependencies.py)

### B. Enum mapping mismatch
- Problem:
  - PostgreSQL enum stores `FULL/PASSTHROUGH/...`
  - ORM was attempting to write enum member names like `full`
  - this broke `last_notified_mode` updates
- Fix:
  - configured SQLAlchemy enum to persist enum values instead of names
- File:
  - [`api/db/models.py`](d:\memoryos\memory-api\api\db\models.py)

### C. Docker build stability
- Problem:
  - Docker build started failing on loose `python-jose` dependency resolution
- Fix:
  - pinned `python-jose[cryptography]==3.5.0`
- File:
  - [`pyproject.toml`](d:\memoryos\memory-api\pyproject.toml)
- Result:
  - `docker compose build api celery-worker` passed

## Final Status

Passed:
- real `FULL -> PASSTHROUGH` mode transition
- real webhook delivery
- alert idempotency
- Docker rebuild after fixes

Current state of test tenant:
- mode effectively `PASSTHROUGH`
- `last_notified_mode = PASSTHROUGH`
