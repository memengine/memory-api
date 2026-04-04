# Extraction Job Lifecycle Live Verification

This document records the live verification pass for the extraction job lifecycle work.

It now reflects the stronger hardening pass that added:

- `dead_letter_jobs`
- `created_at`
- `processing_started_at`
- `stale_after`
- `max_attempts`
- explicit watchdog requeue semantics

- `extraction_jobs` table + status tracking
- watchdog stale-job requeue
- dead-letter listing
- manual dead-letter retry
- richer `GET /v1/memories/jobs/{id}` lifecycle response

## Environment

- Date: `2026-04-02`
- Environment: local Docker stack
- API base URL: `http://127.0.0.1:8000`
- Verification tenant:
  - tenant id: `456eb115-e1db-4420-80d5-862522764efa`
  - tenant name: `Extraction Jobs Live Verify`

## Prerequisites Applied During Verification

### 1. Migration applied

Alembic was still on:

- `f2a3b4c5d6e7`

The original extraction jobs migration was then applied successfully:

- head: `a1b2c3d4e5f6`

The stronger hardening migration was then applied successfully too:

- head: `b3c4d5e6f7a8`

### 2. Migration fix required

The live database already had the enum type `extraction_job_status_enum`, so the migration needed a small idempotency fix.

Applied fix:

- [a1b2c3d4e5f6_add_extraction_jobs.py](d:/memoryos/memory-api/api/db/migrations/versions/a1b2c3d4e5f6_add_extraction_jobs.py)

Change:

- set `create_type=False` on the SQLAlchemy PostgreSQL enum declaration
- keep explicit `create(..., checkfirst=True)` in the migration

### 3. Containers rebuilt

The running API/worker containers were still on older images and did not include the newest lifecycle code. The stack was rebuilt before the final live verification:

```powershell
docker compose up -d --build api celery-enterprise celery-growth celery-starter celery-background celery-beat
```

During the stronger pass, the API container was rebuilt once more after fixing a live-only dead-letter endpoint bug in `internal.py`.

## Verified Live

### 1. Job tracking

Verified:

- `POST /v1/memories/add` created an `extraction_jobs` row immediately with `status='queued'`
- after rebuild, workers also populated:
  - `processing_started_at`
  - `stale_after`
  - `created_at`
  - `max_attempts`

Live job:

- job id: `22f46a20-907b-4e09-8cd9-664acaa9948c`

Observed DB row immediately after submit in the stronger pass:

- `status='processing'`
- `queue_name='starter-extraction'`
- `attempts=0`
- `max_attempts=3`
- `processing_started_at` populated
- `stale_after = processing_started_at + 10 minutes`

Also verified through API:

- `GET /v1/memories/jobs/22f46a20-907b-4e09-8cd9-664acaa9948c`

Observed response:

- `status='queued'`
- `attempts=0`
- `queue_name='starter-extraction'`
- timestamps present

### 2. Max retries -> DEAD

Verified:

- a controlled failing extraction job reached 3 failed attempts and moved to `dead`

Live job:

- job id: `7d042ecd-3b1c-4e21-bac3-569b672935f4`

Observed DB state:

- `status='dead'`
- `attempts=3`
- `error='Proxy user 00000000-0000-0000-0000-000000000001 not found.'`
- `dead_lettered_at` populated

### 3. Dead letter endpoint

Verified:

- `GET /v1/internal/dead-letter-jobs`

Observed:

- returned the dead job above
- included `attempts`, `queue_name`, `error`, and timestamps

Additional stronger-pass note:

- this endpoint initially returned a live `500` because SQLAlchemy async rows came back as `Row` objects rather than plain tuples
- fixed in:
  - [internal.py](d:/memoryos/memory-api/api/routers/internal.py)
- endpoint was rebuilt and re-verified live afterward

### 4. Manual retry

Verified:

- `POST /v1/internal/dead-letter-jobs/{id}/retry`

Live retry job:

- job id: `7650b952-acd2-4d7a-9d95-49f9330ecf23`

Flow:

- seeded a valid dead-letter job row
- called retry endpoint
- job returned to `queued`
- worker processed it successfully

Observed final DB state:

- `status='completed'`
- `attempts=1`
- `memories_created=3`
- `error=NULL`
- `completed_at` populated

Also verified through API:

- `GET /v1/memories/jobs/7650b952-acd2-4d7a-9d95-49f9330ecf23`

Observed response:

- full lifecycle fields present
- `status='completed'`
- `attempts=1`

Stronger-pass retry verification:

- seeded a second live dead-letter row with a valid payload:
  - `f5e2d13c-5a69-4d59-8a67-4c6f7da8b111`
- called:
  - `POST /v1/internal/dead-letter-jobs/f5e2d13c-5a69-4d59-8a67-4c6f7da8b111/retry`
- observed:
  - immediate response `status='queued'`
  - `/v1/memories/jobs/{id}` moved to `processing`
  - DB row then moved to `completed`

### 5. Watchdog idempotency

Verified:

- running watchdog twice on the same stale processing row requeued exactly once

Live stale job:

- job id: `bc048e5b-c13a-4572-b58f-2c717a6808e1`

Command run inside the live API container:

```powershell
docker compose exec api python -c "from api.tasks.job_watchdog_tasks import run_watchdog_cycle; print(run_watchdog_cycle()); print(run_watchdog_cycle())"
```

Observed result:

- first run: `{'checked': 1, 'requeued': 1}`
- second run: `{'checked': 0, 'requeued': 0}`

Stronger-pass deterministic verification:

- temporarily stopped `celery-beat` to avoid scheduler race
- seeded stale job:
  - `2c9cbfb2-9987-4a37-b4af-3be725fd9123`
- ran inside the live API container:

```powershell
docker compose exec api python -c "from api.tasks.watchdog_tasks import run_watchdog_cycle; print(run_watchdog_cycle()); print(run_watchdog_cycle())"
```

Observed result:

- first run: `{'checked': 1, 'requeued': 1, 'dead': 0}`
- second run: `{'checked': 0, 'requeued': 0, 'dead': 0}`

This is the clearest local-live proof that the watchdog is idempotent under the hardened schema.

## Not Fully Verified Yet

### 1. True worker-crash recovery while actively PROCESSING

Target checklist item:

- kill worker while job is truly `processing`
- watchdog requeues within 2 minutes

Status:

- partially verified only

What happened locally:

- I attempted a real crash drill by queueing a larger job and stopping `celery-starter`
- the job still completed before the stop landed

Stronger-pass note:

- after rebuilding, I repeated the attempt with a fresh live job:
  - `b806bc0c-aec1-4eba-9741-6acad374ebf0`
- the worker again completed before the stop could interrupt it

So the live watchdog path is proven through:

- a real stale `processing` row
- a real watchdog requeue
- real idempotency

But not yet through:

- catching a naturally in-flight worker process mid-task

### 2. External Sentry delivery

Target checklist item:

- Sentry alert fires on dead-letter after max retries

Status:

- code path exercised
- external Sentry delivery not directly verified in this pass

The dead-letter transition that triggers the Sentry call did occur, but I did not verify arrival in Sentry UI from this environment.

## Commands Used

### Apply migration

```powershell
osenv\Scripts\python -m alembic upgrade head
osenv\Scripts\python -m alembic current
```

### Create isolated test tenant

```powershell
osenv\Scripts\python scripts\create_tenant.py "Extraction Jobs Live Verify"
```

### Rebuild local stack

```powershell
docker compose up -d --build api celery-enterprise celery-growth celery-starter celery-background celery-beat
```

### Watchdog idempotency

```powershell
docker compose exec api python -c "from api.tasks.job_watchdog_tasks import run_watchdog_cycle; print(run_watchdog_cycle()); print(run_watchdog_cycle())"
```

## Current Conclusion

This feature is now in a good local-live state:

- queued job row creation: verified
- stronger lifecycle timestamps/state fields: verified
- dead-letter transition after 3 failures: verified
- dead-letter listing: verified
- manual retry: verified
- lifecycle API response: verified
- watchdog idempotency: verified

Remaining before calling it fully production-proven:

- real in-flight worker crash + watchdog recovery in staging or AWS
- external Sentry alert visibility check
