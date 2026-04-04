# Staging / Pre-Prod Verification Tracker

This file is a running checklist for things that are still worth verifying outside the local Docker environment before calling the platform production-ready.

Use it as the handoff tracker for future staging and pre-production passes.

## How To Use This File

- `Local` means already verified on the current machine / Docker stack
- `Staging` means verify in a production-like AWS or staging deployment
- `Production` means verify only after real deployment, with caution and low blast radius

---

## 1. Extraction Job Lifecycle

Status:

- local-live strongly verified

Reference:

- [extraction_job_lifecycle_live_verification.md](d:/memoryos/memory-api/docs/extraction_job_lifecycle_live_verification.md)

Still verify in staging:

- kill a worker while a real extraction job is actively `processing`
- confirm watchdog requeues it within the expected window
- confirm the replacement task completes normally
- verify Sentry receives the dead-letter alert after 3 failed attempts

Why staging:

- local extraction jobs still complete too quickly to reliably catch mid-flight worker termination
- Sentry/UI visibility is more meaningful in a real deployed environment

---

## 2. Queue Fairness / Noisy Neighbor Isolation

Status:

- local routing and queue-depth behavior verified
- full fairness under real concurrent load still pending

Reference:

- [noisy_neighbor_queue_fairness_verification.md](d:/memoryos/memory-api/docs/noisy_neighbor_queue_fairness_verification.md)

Still verify in staging:

- heavy starter/free traffic does not delay enterprise queue completion materially
- same-tier fairness under burst load
- queue-depth endpoint reflects real buildup under concurrent workers
- autoscaling reacts correctly once CloudWatch metric emission exists

Why staging:

- single-machine Docker is not representative for sustained multi-tenant queue contention

---

## 3. Circuit Breakers / Infrastructure Degradation

Status:

- steady-state degraded behavior improved locally
- first-hit outage latency still environment-sensitive

Reference:

- [redis_first_failure_hardening.md](d:/memoryos/memory-api/docs/redis_first_failure_hardening.md)

Still verify in staging:

- first request after Redis outage degrades quickly
- first request after Qdrant outage fails fast once circuit is open
- shared circuit state across multiple API replicas
- internal circuit health endpoint reflects multi-replica state correctly

Why staging:

- multi-replica behavior and real networking differ from laptop Docker

---

## 4. Vector Outbox / Reconciliation

Status:

- code path and drift repair tool verified
- daily reconciliation and large-drift operations should still be watched in staging

References:

- [vector_drift_repair_runbook.md](d:/memoryos/memory-api/docs/vector_drift_repair_runbook.md)

Still verify in staging:

- outbox worker keeps up under sustained write load
- reconciliation runtime and memory usage on realistic data volumes
- alert behavior when `missing_in_qdrant > 100`

Why staging:

- production-like data size matters here

---

## 5. Embedding Model Versioning / Re-Embedding

Status:

- local-live model switch and retry hardening verified

References:

- [embedding_model_versioning.md](d:/memoryos/memory-api/docs/embedding_model_versioning.md)
- [gemini_to_openai_embedding_migration.md](d:/memoryos/memory-api/docs/gemini_to_openai_embedding_migration.md)

Still verify in staging:

- long-running tenant re-embedding under realistic data size
- active model switch via internal endpoint across multiple API replicas
- multi-model search latency impact during migration window
- rollback from new model to old model with no retrieval outage

Why staging:

- migration-window latency and replica cache behavior matter most there

---

## 6. Zero-Downtime Backfill Framework

Status:

- framework and guards verified
- the original proxy-user backfill is already complete, so a real 10K live test needs a shadow dataset

References:

- [zero_downtime_migration_runbook.md](d:/memoryos/memory-api/docs/zero_downtime_migration_runbook.md)
- [backfill_operator_runbook.md](d:/memoryos/memory-api/docs/backfill_operator_runbook.md)

Still verify in staging:

- 10K-row shadow backfill run
- worker kill and resume from Redis cursor
- live API latency impact stays within target

Why staging:

- the real legacy dataset is already contracted locally, so a meaningful scale drill needs synthetic staging data

---

## 7. LLM Provider Abstraction / Failover

Status:

- provider abstraction and failover logic verified locally
- Gemini live path verified with real API key
- Cohere live embedding path verified with real API key
- Anthropic live extraction still pending

Verified locally:

- `LLMRouter` provider selection and fallback behavior
- extraction fallback logic:
  - `gemini -> anthropic`
- embedding provider selection:
  - `gemini / cohere / local`
- add/retrieve compatibility after provider abstraction refactor
- correctness guard preserved:
  - no silent Cohere fallback for Gemini-backed vector collections

Verified live:

- Gemini provider availability check
- real Gemini embedding returned a `1536`-dimension vector
- real Gemini extraction returned JSON and flowed through `ExtractionService`
- `EmbeddingService` still returns the active model metadata correctly for Gemini
- Cohere provider availability check
- real Cohere embedding returned a `1024`-dimension vector
- `LLMRouter.get_embed_provider()` selected `cohere` when the Gemini embed circuit was forced open
- local/live compatibility checks confirm:
  - `retrieve()` returns empty or cached-only behavior when Gemini embedding is unavailable
  - `add()` compatibility tests still pass after the provider abstraction refactor

Still verify in staging:

- real Anthropic extraction with valid production-like key
- failover behavior when Gemini is intentionally unavailable
- retrieve behavior during Gemini embedding outage:
  - cache-only response, no Cohere query embedding fallback for Gemini collections
- add behavior during Gemini embedding outage:
  - job retry / queue behavior instead of mixed-vector writes

Why staging:

- Anthropic is still missing a real key here
- real outage and recovery behavior still needs controlled multi-service staging drills

---

## 8. Production-Only Checks

These are better after deployment, with careful scope:

- real tenant-facing latency under load
- real Sentry alerts and incident routing
- ECS/Fargate autoscaling alarms
- CloudWatch dashboards and queue-depth metrics
- multi-AZ database/network failure behavior

---

## 9. Region-Aware Routing / Data Residency Phase 1

Status:

- Phase 1 code wiring verified locally
- `regions` table and `tenants.region_id` migration applied
- all tenants still effectively routed to `IN1`

References:

- [region_assignment_runbook.md](d:/memoryos/memory-api/docs/region_assignment_runbook.md)

Verified locally:

- region-aware request-scoped DB/cache/Qdrant dependency selection
- separate connection objects for `IN1` and `EU1`
- `RegionMiddleware` cached-path overhead is comfortably below target
- middleware order is correct for request handling:
  - auth runs before region routing
- hardcoded local DSN fallbacks were removed from the main runtime path

Still verify in staging:

- one live tenant request logs and routes through the expected `IN1` bundle
- Secrets Manager-backed region secret loading works in deployed AWS runtime
- region cache invalidation works after manual tenant region reassignment
- no replica keeps stale tenant region after cache invalidation

Still verify in later region rollout phases:

- `EU1` infrastructure exists and is healthy before assigning any tenant
- `US1` infrastructure exists and is healthy before assigning any tenant
- tenant data never crosses region boundaries for DB, Redis, Qdrant, workers, and backups
- model provider calls follow regional compliance rules when EU routing is introduced

Why staging:

- local Phase 1 proves the routing framework, but not real AWS Secrets Manager access or multi-region infrastructure behavior

Operator rule right now:

- keep all tenants on `IN1`
- do not assign tenants to `EU1` or `US1` until those regional stacks are deployed and verified

---

## 10. API Versioning / Deprecation Tracking

Status:

- local-live deprecation drill verified
- version routing and unsupported-version rejection verified

Verified locally:

- `/v1/...` requests do not emit deprecation headers unless a deprecated field is used
- deprecated field responses now emit:
  - `Deprecation: true`
  - `Sunset: ...`
  - `Link: ...`
  - `X-MemoryOS-Deprecated-Fields: ...`
- tenant-scoped deprecated field usage is persisted and visible in:
  - `GET /v1/tenant/deprecation-usage`
- repeated deprecated-field requests update usage tracking correctly
- unsupported versions like `/v3/...` return a clear `400`
- deprecation alert task fires for tenants still using a field in the 30-day warning window

Still verify in staging:

- real tenant webhook delivery for 30/7/1-day deprecation warnings
- log shipping / structured log visibility in the deployed logging stack
- multi-replica consistency for deprecation usage persistence

Why staging:

- webhook delivery, logging pipelines, and replica behavior are more meaningful in deployed infrastructure than local Docker

---

## 11. Tenant Webhook Event Delivery

Status:

- structured webhook event system is implemented locally
- signed delivery, retries, invalid-URL skip behavior, and quota/queue event dispatch are verified in local tests
- real network delivery is still pending

Reference:

- [webhook_event_staging_verification.md](d:/memoryos/memory-api/docs/webhook_event_staging_verification.md)

Verified locally:

- HMAC-SHA256 signature generation for webhook payloads
- retries on failed webhook responses
- silent skip when no webhook URL is configured
- silent skip when webhook URL fails validation
- quota manager dispatches:
  - `quota.warning`
  - `quota.critical`
  - `quota.exhausted`
  - `mode.changed`
- queue ETA path dispatches:
  - `processing.delayed`
  - `processing.recovered`
- monthly reset task dispatches:
  - `quota.reset`

Still verify in staging:

- real webhook delivery to a reachable endpoint
- real signature verification using a stored tenant `webhook_secret`
- timeout and retry behavior against a slow/failing endpoint
- queue delay/recovery event delivery from deployed workers
- monthly reset event delivery from deployed beat/worker infrastructure

Why staging:

- real outbound HTTP, DNS, TLS, and worker networking are not meaningfully proven by local-only tests

---

## 12. Public Status Page

Status:

- runbook prepared
- setup intentionally deferred until closer to external launch

Reference:

- [status_page_rollout_runbook.md](d:/memoryos/memory-api/docs/status_page_rollout_runbook.md)

Still do before production launch:

- create the public status page at `status.memoryos.io`
- add core components:
  - API
  - Memory Storage
  - Memory Retrieval
  - Vector Search
- add monitors for:
  - `/health`
  - `/v1/internal/circuit-health` or equivalent private/internal monitor wiring
- connect CloudWatch / infra alerts to incident updates
- verify one practice incident flow before launch
- link the status page from tenant-facing docs and later from the dashboard

Why before launch:

- once real tenants rely on the platform, missing public status visibility turns every outage or degradation into support traffic
- this is not required for private development, but it is required before public customer rollout

---

## Suggested Order Before Production

1. staging worker-crash + watchdog recovery
2. staging noisy-neighbor fairness drill
3. staging circuit-breaker replica drill
4. staging LLM failover drill with real Anthropic/Cohere keys
5. staging re-embedding migration drill
6. staging region-routing / Secrets Manager verification
7. staging deprecation webhook / logging verification
8. staging tenant webhook event delivery verification
9. set up and smoke-test `status.memoryos.io`
10. production canary checks with one low-risk tenant

## Notes

This file is intentionally not tied to one prompt. It is the ongoing reminder list for future verification work that local development cannot fully prove.
## Deferred Warnings Cleanup

Do not prioritize these now. Address them in a single cleanup pass before the first enterprise customer or before hiring the first engineer, whichever comes first.

- Upgrade `sentry-sdk` to clear the current Sentry deprecation warning.
- Upgrade the Qdrant server so it matches the client version and removes the compatibility warning.
- Add `.pytest_cache` to `.gitignore` and set correct pytest cache permissions in the CI/Docker environment.
- Audit async test teardown in `conftest.py` and related test helpers to remove the coroutine cleanup warning.
