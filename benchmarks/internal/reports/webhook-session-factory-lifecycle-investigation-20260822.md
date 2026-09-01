# Webhook session-factory lifecycle investigation — 2026-08-22

Status: **confirmed application lifecycle defect; no production repair applied**.

## Scope

This investigation used the accepted failed MODERATE run `moderate-20260821`, static production-path tracing, and an offline constructor-control experiment. It did not change extraction, retrieval, quota, webhook, Redis, database, or claim behavior. Holdout and paid providers were not used.

## Construction path

For ordinary memory API traffic:

1. FastAPI creates `QuotaManager` through `get_quota_manager`.
2. `QuotaManager.__init__` creates `WebhookEventService`.
3. `WebhookEventService.__init__` calls `build_sync_session_factory()` when no factory is injected.
4. `build_sync_session_factory()` creates a new SQLAlchemy engine and connection pool.
5. `QuotaEnvelopeMiddleware` usually creates another `QuotaManager` after the route because memory routes do not place their computed envelope on `request.state`.

The engine is created regardless of whether a webhook exists or is dispatched. Closing a session in `WebhookEventService.send()` does not dispose the engine created by the service constructor.

Celery quality-gate callers already inject a process-owned factory. The API dependency and response-middleware paths do not. The quota Celery task also creates engines directly, but it was not the measured MODERATE API amplification boundary and must remain a separate cleanup.

## Evidence

The frozen MODERATE run issued 3,141 HTTP requests. PostgreSQL telemetry attributed synchronous engine construction to `api.services.webhook_event_service.__init__:61`; sampled logs contained 249 emitted construction records and the owner's monotonic construction counter reached **6,100**, approximately 1.94 factories per HTTP request.

This coincided with:

- API process CPU maximum: 206.8%;
- API RSS maximum: 884,944,896 bytes;
- Redis command timeouts: 695;
- Redis connection timeouts: 383;
- Redis pool-acquisition timeouts: 383;
- API error rate: 17.62%;
- achieved workload: 1.64 iterations/s against 8 scheduled iterations/s.

The Redis server itself rejected zero connections and evicted zero keys. The observed Celery broker queue was empty, while all 749 accepted jobs and 785 outbox records completed after drain. This supports request-side engine/pool and fallback amplification rather than durable worker corruption.

## Controlled constructor check

An offline patched-constructor check created 3,141 service instances without opening a real database connection:

| Mode | Factory builds |
|---|---:|
| Current default construction | 3,141 |
| Same instances with one injected factory | 0 additional builds |

This proves that the existing `session_factory` injection boundary is sufficient to eliminate service-level engine growth; a second idempotency or database abstraction is unnecessary.

## Classification

The defect belongs to **application resource ownership/lifecycle**:

- the engine/pool is process-scoped infrastructure;
- the service object is request-scoped;
- a process-scoped dependency is being created by the request-scoped object;
- there is no matching engine disposal for those request-created pools.

It is not a PostgreSQL capacity-limit failure, Redis semantic failure, Celery backlog, Qdrant bottleneck, or correctness failure.

## One isolated repair proposed

Create exactly one process-owned synchronous webhook session factory during FastAPI lifespan, store it on application state, inject it into every API-created `WebhookEventService` through `QuotaManager`, and dispose its bound engine during lifespan shutdown.

Only dependency ownership should change. Do not alter webhook delivery, quota computation, response middleware behavior, Redis timeouts/fallbacks, pool sizing, extraction, retrieval, or claims. Keep Celery task factory behavior outside this repair.

## Acceptance criteria

1. One webhook sync engine/factory per API process; no request-proportional growth.
2. Repeated construction of `QuotaManager`/`WebhookEventService` reuses the same injected factory.
3. The factory engine is disposed during application shutdown.
4. Existing webhook and quota unit tests remain green, with focused lifecycle regression tests.
5. FAST and required integration gates remain green.
6. Rerun the unchanged frozen MODERATE workload in the disposable stack.
7. API error rate <=0.5%, HTTP 500 count zero, and frozen latency thresholds remain unchanged.
8. PostgreSQL connection/engine growth and Redis connection/pool timeouts caused by request churn are zero.
9. All accepted jobs finish after drain; all durable correctness invariants and outbox convergence remain 100%.

If these criteria fail, revert the repair and retain this diagnosis without starting HIGHER or SUSTAINED.
