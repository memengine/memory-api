**Purpose**
This runbook explains how to use the new zero-downtime migration pieces together:
- expand-first schema changes
- resumable backfill jobs
- contract-phase migration guards

**What Was Added**
- backfill framework: [backfill_tasks.py](d:/memoryos/memory-api/api/tasks/backfill_tasks.py)
- backfill status/trigger endpoints: [internal.py](d:/memoryos/memory-api/api/routers/internal.py)
- contract guard helpers: [contract_guard.py](d:/memoryos/memory-api/api/db/migrations/contract_guard.py)
- backfill jobs table migration: [e1f2a3b4c5d6_add_backfill_jobs.py](d:/memoryos/memory-api/api/db/migrations/versions/e1f2a3b4c5d6_add_backfill_jobs.py)

**The 4 Phases**
1. Expand
- add new nullable column, table, or index
- old code must still work
- new code may dual-write old + new fields

2. Backfill
- run a resumable backfill in batches
- monitor with `GET /v1/internal/backfill-status`
- keep the system live during migration

3. Gate
- before removing old compatibility paths or enforcing the final contract, run a precondition check
- if legacy rows still exist, fail clearly

4. Contract
- only after backfill is complete and the gate passes
- remove old code paths or tighten schema constraints

**Backfill Trigger**
API:
```text
POST /v1/internal/backfill/run/proxy-user-ids
```

Status:
```text
GET /v1/internal/backfill-status
```

Shell queue command:
```powershell
osenv\Scripts\python -c "from api.tasks.backfill_tasks import run_backfill_proxy_user_ids; result = run_backfill_proxy_user_ids.delay(batch_size=500, sleep_between_batches_ms=100); print(result.id)"
```

Direct local run:
```powershell
osenv\Scripts\python -c "from api.tasks.backfill_tasks import BackfillProxyUserIds; print(BackfillProxyUserIds().run(batch_size=500, sleep_between_batches_ms=100))"
```

**How Resume Works**
- the cursor is stored in Redis:
  - `backfill:backfill_proxy_user_ids:cursor`
- if the worker crashes, a later run resumes from the cursor instead of restarting from row 1

**How Load Throttling Works**
- the task pauses automatically when:
  - system CPU is above the configured threshold
  - or active PostgreSQL queries are above the configured threshold
- then it waits and retries later

**Contract-Phase Guard**
Use the helper functions in [contract_guard.py](d:/memoryos/memory-api/api/db/migrations/contract_guard.py).

Primary checks:
- `assert_no_remaining_nulls(...)`
- `assert_backfill_completed(...)`

**Example Migration Guard**
Use this pattern inside a future contract migration:

```python
from alembic import op
from api.db.migrations.contract_guard import assert_backfill_completed
from api.db.migrations.contract_guard import assert_no_remaining_nulls

def upgrade():
    bind = op.get_bind()
    assert_backfill_completed(bind, task_name="backfill_proxy_user_ids")
    assert_no_remaining_nulls(
        bind,
        table_name="memories",
        column_name="proxy_user_id",
    )
    # safe contract-phase change here
```

What happens if it fails:
- the migration stops immediately
- the error clearly says how many legacy rows remain
- sample row ids are included for debugging

**When To Use The Gate**
Use the gate before:
- `SET NOT NULL`
- dropping legacy columns
- removing dual-write logic
- removing old read compatibility

Do not skip it just because backfill was started earlier.

**What This Solves**
- avoids long downtime
- avoids unsafe “stop the world” migrations
- makes large data migrations resumable
- prevents contract-phase deploys while legacy data still exists

**Checklist Mapping**
What is covered now:
- resumable cursor-based backfill
- load-aware pause/resume
- internal status monitoring
- manual operator trigger
- contract-phase helper that can fail clearly

What still depends on operator execution:
- the actual live 10K-row test
- kill-worker-and-resume verification in a real running stack
- using the guard inside each future contract migration

**Short Recommendation**
- expand first
- backfill in batches
- verify status is complete
- run contract guard
- only then apply destructive schema tightening
