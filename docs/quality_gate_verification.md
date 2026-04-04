# Quality Gate Verification

Date: 2026-03-30

## Migration

- Ran `alembic revision --autogenerate -m "add_tenant_budgets_and_call_quality_log"`
- The generated revision file was initially empty, so it was reviewed and corrected manually to include:
  - `tenant_budgets`
  - `call_quality_log`
- Applied migration with:
  - `alembic upgrade head`
- Verified live database tables and columns exist:

### `tenant_budgets`
- `id`
- `tenant_id`
- `plan_tier`
- `monthly_call_limit`
- `monthly_token_limit`
- `current_month_calls`
- `current_month_tokens`
- `rate_limit_per_user_per_minute`
- `overage_policy`
- `alert_threshold_pct`
- `reset_at`
- `created_at`

### `call_quality_log`
- `id`
- `tenant_id`
- `external_user_id`
- `layer_blocked_at`
- `quality_score`
- `semantic_similarity`
- `created_at`

## Seeded Test Tenant Budget

- Inserted tenant:
  - `487990e8-797a-431a-91c8-61f6a8f4db0a`
- Inserted budget row:
  - `plan_tier = starter`
  - `monthly_call_limit = 1000`

## Verification Results

- `L1` rate limit:
  - Called `check()` 11 times in the same minute for the same tenant + user
  - 11th call returned:
    - `passed = False`
    - `blocked_layer = "L1"`
    - `reason = "rate_limit_exceeded"`

- `L2` low quality:
  - Input: `messages=[{"role":"user","content":"hi"}]`
  - Returned:
    - `passed = False`
    - `blocked_layer = "L2"`
    - `reason = "low_quality"`

- `L3` semantic duplicate:
  - Called `check()` twice with semantically identical conversation content
  - Second call returned:
    - `passed = False`
    - `blocked_layer = "L3"`
    - `reason = "duplicate_query"`

- `L4` budget exhausted:
  - Set `current_month_calls = monthly_call_limit`
  - Returned:
    - `passed = False`
    - `blocked_layer = "L4"`
    - `reason = "budget_exhausted"`

- Full pass:
  - 5-message technical conversation
  - Returned:
    - `passed = True`

- Performance:
  - 1000 sequential `check()` calls
  - `L1`-only path, no `L3` duplicate evaluation
  - Total time: `0.136968s`

- `call_quality_log` coverage:
  - Every `check()` call recorded one log entry in verification runs

## Commands Run

```powershell
osenv\Scripts\alembic upgrade head
osenv\Scripts\python -m pytest tests\unit\test_quality_gate.py
```
