# Queue Fairness Verification

This document records what was verified for the multi-queue Celery fairness rollout, what still remains, and when the remaining checks should happen.

## Goal

Prevent one tenant from monopolizing the shared extraction worker pool.

The new design introduces:

- plan-tier extraction queues
- dedicated worker pools per queue
- per-tenant queue depth limits
- queue monitoring via `GET /v1/internal/queue-depth`
- `--prefetch-multiplier 1` on all queue workers

## Verified Now

These checks were completed successfully in the current local Docker environment.

### 1. Focused code-level verification

Command:

```powershell
osenv\Scripts\python -m pytest tests\unit\test_queue_router.py tests\unit\test_memory_service.py tests\integration\test_all_endpoints.py tests\unit\test_celery_app.py
```

Result:

- `13 passed`

What this covered:

- plan-tier queue selection
- per-tenant queue limit handling
- extraction dispatch uses the new queue-aware Celery call shape
- internal queue-depth endpoint is exposed in OpenAPI
- Celery default/background routing config is present

### 2. Docker Compose topology reflects the new worker split

Command:

```powershell
docker compose config --services
```

Observed services:

- `api`
- `celery-enterprise`
- `celery-growth`
- `celery-starter`
- `celery-background`
- `celery-beat`
- `postgres`
- `redis`
- `qdrant`
- `localstack`

### 3. Local runtime is actually using the new worker topology

Command used:

```powershell
docker compose up -d --build api celery-enterprise celery-growth celery-starter celery-background celery-beat
docker compose up -d --remove-orphans
docker compose ps
```

Verified:

- old shared `celery-worker` container was removed
- new workers were running and healthy:
  - `celery-enterprise`
  - `celery-growth`
  - `celery-starter`
  - `celery-background`
  - `celery-beat`

### 4. Queue subscriptions are correct at worker startup

Worker logs verified:

- `celery-enterprise` subscribed to:
  - `enterprise-extraction`
- `celery-growth` subscribed to:
  - `growth-extraction`
- `celery-starter` processed starter extraction jobs
- `celery-background` processed background tasks such as:
  - `vector_sync_outbox`

Important runtime property confirmed:

- workers are running with `--prefetch-multiplier 1`

### 5. Internal queue-depth endpoint works live

Command:

```powershell
GET http://127.0.0.1:8000/v1/internal/queue-depth
```

Verified live:

- endpoint returned `200`
- response included all expected queues:
  - `enterprise-extraction`
  - `growth-extraction`
  - `starter-extraction`
  - `free-extraction`
  - `celery`
  - `reembedding`
  - `dead-letter`

### 6. Live starter-plan enqueue succeeds on the new flow

Command:

- live `POST /v1/memories/add` using a valid API key and a starter-plan tenant

Verified:

- response returned `status="queued"`
- starter worker log showed the extraction task completed
- task payload included:
  - `queue_name: "starter-extraction"`
  - `plan_tier: "starter"`

This confirms the add path is using the queue router and the task reaches the expected queue worker.

### 7. Live `queue_full` protection works

Procedure used:

1. manually set Redis key:

```powershell
docker compose exec redis redis-cli SET tenant_queue_depth:<tenant_id>:starter-extraction 50 EX 600
```

2. called:

```powershell
POST /v1/memories/add
```

3. removed the temporary key:

```powershell
docker compose exec redis redis-cli DEL tenant_queue_depth:<tenant_id>:starter-extraction
```

Verified response:

```json
{
  "job_id": null,
  "status": "queue_full",
  "blocked_reason": "tenant_queue_limit_reached"
}
```

This confirms that the per-tenant queue depth guard works in the live API path.

## Not Yet Verified

These checks are still pending.

## 1. True noisy-neighbor isolation under simultaneous load

Not yet verified:

- Tenant A floods starter/growth queue with a large backlog
- Tenant B submits jobs on another tier
- Tenant B jobs are still processed promptly

Why still pending:

- this requires a controlled load test with many concurrent add requests and timing measurements
- it is more meaningful in staging or production-like infrastructure than in a single laptop Docker run

When to verify:

- best in staging before production launch
- can also be run locally with a dedicated load script, but AWS/staging is more representative

## 2. Same-tier fairness under heavy multi-tenant contention

Not yet verified:

- two starter tenants both push jobs heavily
- one tenant hits `queue_full`
- the other tenant continues to make progress

Why still pending:

- requires a dedicated multi-tenant load drill

When to verify:

- staging
- or local load lab if you want an earlier rehearsal

## 3. ECS autoscaling based on queue depth

Terraform changes were added, but not fully verified live.

Not yet verified:

- CloudWatch alarm fires when `enterprise-extraction` depth stays above `100`
- ECS scales enterprise workers from baseline to `8`
- scale-in occurs when all extraction queues stay at `0`

Why still pending:

- requires deployed AWS infrastructure
- requires real `MemoryOS/Celery` CloudWatch metrics to exist

Important note:

- the Terraform alarms assume queue depth metrics are available in CloudWatch
- that metric-emission path still needs to exist operationally in AWS

When to verify:

- after AWS deployment
- ideally in staging first

## 4. Terraform validation

Not yet verified here:

- `terraform fmt`
- `terraform validate`
- `terraform plan`

Why still pending:

- Terraform CLI was not installed in this environment

When to verify:

- any engineer machine with Terraform installed
- definitely before applying AWS changes

Recommended commands:

```powershell
cd infrastructure\terraform
terraform fmt
terraform validate
terraform plan
```

## 5. Worker counts vs. AWS service behavior

Code/config intent is aligned:

- enterprise worker concurrency `4`
- growth worker concurrency `3`
- starter worker concurrency `3`
- background worker concurrency `2`

Not yet verified live on AWS:

- ECS services come up with the expected desired counts
- worker queues match the intended queue assignment

When to verify:

- after AWS deployment

## Recommended Next Verification Stages

### Before AWS deploy

Do these first:

- run the focused pytest suite
- run Terraform validation/plan
- verify local Docker topology is clean
- optionally do a small local load drill

### In staging on AWS

This is the best place to verify:

- true noisy-neighbor protection across tiers
- same-tier queue fairness
- queue depth growth and oldest age under load
- CloudWatch queue alarms
- ECS worker scale-out/scale-in

### In production

Verify carefully but passively first:

- `/v1/internal/queue-depth`
- queue age growth by tier
- tenant breakdown behavior during bursts
- no unexpected backlog in lower-volume tenants

## Current Readiness Summary

Current status:

- queue routing: verified
- worker split: verified locally
- queue-depth endpoint: verified live
- queue-full guard: verified live
- old shared worker removed locally: verified

Still required before calling this fully production-proven:

- AWS Terraform validation
- CloudWatch/ECS scaling verification
- real load/fairness drill with concurrent tenants
