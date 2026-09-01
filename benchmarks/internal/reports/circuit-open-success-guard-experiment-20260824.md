# Circuit OPEN-state success guard experiment — 2026-08-24

Status: **failed full acceptance and reverted**.

## Isolated change

`CircuitBreaker._record_success` was changed so a late success could not transition an `OPEN`
circuit to `CLOSED`. Success while `CLOSED` still cleared accumulated failures, and a successful
`HALF_OPEN` probe still closed the circuit. No force-open caller, threshold, deadline, retry,
fallback, Redis command, cache, authentication, or business behavior was changed.

Run: `circuit-open-success-guard-moderate-20260824`  
Reference: `redis-circuit-transition-moderate-20260824`

## Local mechanism

Focused concurrency and regression tests passed 41/41 before the run. A delayed operation admitted
before opening completed successfully without closing the open circuit; half-open recovery still
worked.

Under frozen MODERATE traffic, genuine `CLOSED -> OPEN` transitions fell from 112 to 31 (72.3%).
However, redundant `OPEN -> OPEN` events increased from 146 to 280, and seven reopen gaps shorter
than 30 seconds remained without a logged half-open failure between them.

## Newly exposed integration boundary

The API process can hold multiple Redis breaker instances:

1. Starlette constructs middleware and `AuthMiddleware.__init__` captures
   `CircuitBreakerRegistry.get_instance().redis_cb`.
2. During lifespan startup, `api.main.lifespan` calls `CircuitBreakerRegistry.reset()` and replaces
   the registry instance.
3. Region/cache services created after the reset capture the new breaker, while middleware retains
   the old breaker.

The two breakers are independently local because Redis breaker state is not stored in Redis.
Therefore, fixing `_record_success` on each instance cannot enforce one process-wide recovery
window. This explains short cross-instance reopen gaps and invalidates the experiment's global
acceptance result.

## Performance and correctness

| Measurement | Reference | Candidate | Outcome |
|---|---:|---:|---|
| Completed iterations | 2,153 | 1,594 | **26.0% worse** |
| Dropped iterations | 7,448 | 7,999 | **7.4% worse** |
| API error rate | 24.71% | 26.34% | worse |
| HTTP request failure rate | 36.75% | 46.09% | worse |
| Circuit-open gates | 1,050 | 2,432 | **131.6% worse** |
| Circuit execution errors | 920 | 171 | improved |
| Add p50 / p95 / p99 | 19.243 / 29.892 / 30.005 s | 25.184 / 30.011 / 30.059 s | worse |
| Retrieval p50 / p95 / p99 | 16.901 / 28.924 / 30.006 s | 23.175 / 30.009 / 30.042 s | worse |
| Job completion p50 / p95 / p99 | 30.968 / 53.648 / 60.056 s | 36.688 / 55.826 / 58.383 s | mixed/worse |
| PostgreSQL peak connections | 92 | 92 | unchanged |

All 557 accepted jobs completed and claim/idempotency/provenance/version checks passed. However,
8/580 outbox records remained failed after the full drain window, so outbox convergence and the
aggregate durable correctness audit failed. The available warning-level service logs did not expose
the individual Qdrant/outbox failure reasons.

## Decision

The candidate failed acceptance and was reverted. Post-revert focused tests passed 40/40.
Production behavior remains at the pre-experiment reference state.

## One next repair proposed

Repair only process-local registry identity: do not replace `CircuitBreakerRegistry._instance`
inside lifespan after middleware may already have captured it. Ensure authentication middleware,
rate limiter, app state, regional cache, quota manager, quality gate, proxy-user service, and UUI
resolve the exact same Redis breaker object within an API process.

Do not yet reapply the OPEN-state success guard or change force-open policy.

Acceptance:

- one Redis breaker object identity across all API middleware/services in a running app;
- lifespan startup does not invalidate previously captured breaker references;
- test reset/isolation facilities remain available outside normal startup;
- no authentication/cache behavior changes;
- existing circuit/auth/cache suites remain green;
- frozen MODERATE baseline is rerun and durable correctness remains 100%.

Wait for approval before implementing this repair.

## Cleanup and artifacts

Cleanup removed 80 proxy users, 514 Qdrant points, and 14 run-scoped Redis keys; no scoped rows
remained. The disposable containers, network, and volumes were destroyed.

- `artifacts/internal-benchmarks/scale/circuit-open-success-guard-moderate-20260824/`
