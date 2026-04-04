**Purpose**
This doc explains the manual backfill trigger endpoint, the equivalent shell commands, and when to use each one.

**What Exists**
MemoryOS now has:
- a resumable backfill framework in [backfill_tasks.py](d:/memoryos/memory-api/api/tasks/backfill_tasks.py)
- an internal monitoring endpoint:
  - [internal.py](d:/memoryos/memory-api/api/routers/internal.py) -> `GET /v1/internal/backfill-status`
- a manual trigger endpoint:
  - [internal.py](d:/memoryos/memory-api/api/routers/internal.py) -> `POST /v1/internal/backfill/run/proxy-user-ids`

**Current Concrete Backfill**
The current concrete task is:
- `backfill_proxy_user_ids`

It is used to backfill legacy `memories.proxy_user_id` values in small cursor-ordered batches while the system stays live.

**Important**
- this backfill does not auto-start by itself
- it is operator-triggered
- that is intentional, so large migrations only run when you choose to run them

**When To Trigger It**
Trigger this backfill when:
- a new schema field has already been added safely
- old rows still need to be gradually migrated
- you want to run migration work while live traffic continues
- you want progress tracking and resumability

Do not trigger it when:
- there is no pending legacy data to backfill
- the database or worker fleet is already under heavy load
- you have not yet applied the migration that introduced the required schema

**API Endpoint**
Trigger route:

```text
POST /v1/internal/backfill/run/proxy-user-ids
```

Query parameters:
- `batch_size`
  - default: `1000`
- `sleep_between_batches_ms`
  - default: `100`

Example in Swagger:
- authorize with `ApiKeyAuth` or `BearerAuth`
- open `POST /v1/internal/backfill/run/proxy-user-ids`
- set:
  - `batch_size = 500`
  - `sleep_between_batches_ms = 100`
- click `Execute`

Example response:

```json
{
  "data": {
    "task_name": "backfill_proxy_user_ids",
    "task_id": "abc123",
    "status": "queued",
    "batch_size": 500,
    "sleep_between_batches_ms": 100
  },
  "request_id": "req_123",
  "timestamp": "2026-04-01T12:00:00+00:00"
}
```

Meaning:
- the task has been queued in Celery
- the worker will run it asynchronously
- use the status endpoint to monitor progress

**Status Endpoint**

```text
GET /v1/internal/backfill-status
```

This returns rows from `backfill_jobs`, including:
- `task_name`
- `status`
- `total_rows`
- `processed_rows`
- `pct_complete`
- `eta_seconds`
- `started_at`
- `completed_at`
- `error`

**Recommended API Workflow**
1. Trigger the backfill:

```text
POST /v1/internal/backfill/run/proxy-user-ids?batch_size=500&sleep_between_batches_ms=100
```

2. Monitor it:

```text
GET /v1/internal/backfill-status
```

3. Wait until the job shows:
- `status = complete`

**Shell Command Option**
If you want to trigger it from the terminal instead of the API:

Queue it in Celery:
```powershell
osenv\Scripts\python -c "from api.tasks.backfill_tasks import run_backfill_proxy_user_ids; result = run_backfill_proxy_user_ids.delay(batch_size=500, sleep_between_batches_ms=100); print(result.id)"
```

Run it directly in-process for a local/manual one-off:
```powershell
osenv\Scripts\python -c "from api.tasks.backfill_tasks import BackfillProxyUserIds; print(BackfillProxyUserIds().run(batch_size=500, sleep_between_batches_ms=100))"
```

Use the direct in-process command only when:
- you are doing local maintenance
- you intentionally want to bypass Celery queueing

Prefer the API endpoint or Celery `.delay()` in normal operations.

**How It Resumes**
- progress cursor is stored in Redis:
  - `backfill:backfill_proxy_user_ids:cursor`
- if the worker crashes, the next run resumes from the saved cursor

**How It Avoids Downtime**
- scans rows by ordered cursor, not full-table rewrite
- works in batches
- sleeps between batches
- pauses when CPU or active DB queries are too high
- does not require stopping the API

**Best Starting Values**
Safer operator defaults:
- `batch_size = 500`
- `sleep_between_batches_ms = 100`

If rows are large or DB pressure is high:
- lower `batch_size` to `100` or `200`

If the system is healthy and the backfill is small:
- you can try `batch_size = 1000`

**Short Recommendation**
- use the endpoint when you want a clean operator workflow
- use the shell command when you are working directly from the server/terminal
- always monitor with:
  - `GET /v1/internal/backfill-status`
