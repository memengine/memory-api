# Redis failure-ownership experiment — 2026-08-24

## Decision

Retain the isolated Redis failure-ownership repair. It removed caller-forced circuit opens and materially reduced HTTP 500 responses while preserving durable correctness. Do **not** accept this run as the MODERATE scale baseline: the frozen workload still exceeded its API error, HTTP failure, latency, and dropped-arrival thresholds.

## Frozen run

- Run ID: `redis-failure-ownership-moderate-20260824`
- Workload: MODERATE, 8 arrivals/s for 20 minutes, 20 preallocated VUs, 40 maximum VUs
- Environment: disposable `memoryos-scale` Compose stack
- Provider: deterministic benchmark extraction and embedding; paid-provider cost `$0`
- Holdout: not loaded
- Candidate scope: Redis failure ownership only; no registry split, late-success guard, retry, timeout, fallback, cache, auth, or business-logic changes

## Load results

| Metric | Frozen reference | Ownership candidate |
|---|---:|---:|
| Completed iterations | 2,153 | 1,939 |
| Dropped arrivals | 7,448 | 7,658 |
| Interrupted iterations | — | 3 |
| API error rate | 24.71% | 16.01% |
| HTTP failure rate | 36.75% | 27.31% |
| HTTP 500 log responses | 488 | 90 |
| Add p95 | 29.892 s | 30.001 s |
| Retrieval p95 | 28.924 s | 30.001 s |
| Job completion p95 | 53.648 s | 52.411 s |

The error-rate and HTTP-500 reductions are material. However, completion fell 9.9%, dropped arrivals increased, and add/retrieval latency reached the 30-second client timeout ceiling. Therefore the product remains capacity-limited at the frozen MODERATE arrival rate.

## Redis ownership evidence

- Caller-forced-open markers: `219 -> 0`.
- First circuit OPEN occurred at the configured fifth failure.
- Open sources were limited to owned failures: `auth_outer_deadline` and `circuit_execution`.
- Circuit transitions: 827; OPEN transitions: 230; circuit-open gates: 1,947.
- Execution errors: 680; command errors: 1,701.
- No ordinary caller reset the OPEN recovery clock through `force_open()`.

This validates the architectural change: breaker-wrapped Redis failures are counted by the breaker once; only the separate auth outer deadline reports an external failure.

## Durable correctness

- Accepted jobs: 727; completed: 727; unfinished: 0.
- Outbox rows: 753; done: 753; pending/failed: 0.
- Single winner: pass.
- Winner alignment: pass.
- Event idempotency: pass.
- Provenance preservation: pass.
- Version-chain integrity: pass.
- PostgreSQL connection errors in service-log analysis: 0.
- Enum telemetry errors: 0.

## Regression gates

- Focused Redis/auth/cache/service tests: 69 passed.
- Post-load FAST tier: 8/8 suites passed; no product failures or harness errors.
- Provider cost: `$0`.

## Harness diagnostics

The independent PostgreSQL observer failed before sampling because its host process did not receive `APP_ENV=benchmark`. This is harness configuration drift, not a product failure. PostgreSQL connection-error evidence remains available from service telemetry, but under-load connection-count percentiles are unavailable for this run. The initial in-container preflight also required copying the benchmark package into the disposable API container because the scale image excludes benchmark sources by design; the safety preflight itself passed afterward.

## Cleanup

Run-scoped cleanup removed 80 proxy users, 674 Qdrant points, and 19 Redis keys; all scoped database counts reached zero. The disposable containers, network, and volumes were then destroyed.

## Next isolated investigation

Do not tune Redis again. The next confirmed boundary is API request occupancy/latency under the fixed 40-VU cap: add and retrieval calls sit at the 30-second client timeout while durable worker/outbox processing eventually converges. Diagnose API-side wait composition (authentication database fallback, request transaction time, retrieval hydration/vector time, and job-poll occupancy) before proposing one optimization.
