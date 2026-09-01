# Redis circuit-transition diagnosis — 2026-08-24

Status: diagnosis complete; no production Redis behavior changed.

## Run contract

- Run: `redis-circuit-transition-moderate-20260824`
- Workload: frozen MODERATE, 20 minutes, 8 arrivals/second, 20 configured VUs
- Environment: disposable `memoryos-scale` Docker stack
- Provider: deterministic benchmark provider; paid-provider cost **$0**
- Redis comparison settings: TCP preflight disabled, generation invalidation v1, cache namespace v2
- Holdout: not loaded

## Result

The first Redis circuit open was caused by an unconditional caller-requested `force_open`, not by
the circuit breaker's five-failure policy.

At `2026-08-24T10:26:12.886Z`, an API-key authentication cache lookup exceeded its independent
200 ms `asyncio.wait_for` boundary (observed lookup duration: 219.7 ms). The timeout handler called
`AuthMiddleware._mark_redis_unavailable`, which immediately forced the shared Redis circuit from
`CLOSED` to `OPEN` with a synthetic failure count of five. The first normal threshold-driven open
did not occur until about 51 seconds later and was caused by five circuit-execution deadlines.

The initiating lookup had obtained an auth Redis connection from the pool; its underlying command
did not report a Redis socket timeout before the outer 200 ms wrapper cancelled it. Therefore, the
first open cannot be attributed to TCP preflight, pool exhaustion, the 500 ms Redis socket timeout,
or the 750 ms circuit deadline.

## Circuit evidence

| Measurement | Result |
|---|---:|
| Circuit transition events | 1,139 |
| Opens | 285 |
| Opens caused by `force_open` | 219 |
| Opens caused by normal failure threshold | 66 |
| `force_open` calls from authentication | 135 |
| `force_open` calls from cache service | 84 |
| Circuit execution deadline failures | 919 |
| Circuit-open fallback gates | 1,050 |
| Pool acquisition timeouts | 27 |
| Connection timeouts | 27 |
| TCP-preflight failures | 0 (preflight intentionally disabled) |

There were 1,082 `CLIENT` `ResponseError` telemetry records during Redis connection negotiation.
They were not passed into circuit failure accounting and frequently coexisted with successful
connections/commands, so they are instrumentation/protocol noise rather than the initiating
circuit failure.

Authentication telemetry recorded 3,013 cache misses, 383 hits, 77 outer cache timeouts, and
3,090 successful PostgreSQL fallback authentications. This confirms that forced circuit openings
amplified cache unavailability and PostgreSQL fallback load.

## Workload and correctness outcome

| Measurement | Result |
|---|---:|
| Completed iterations | 2,153 |
| Dropped iterations | 7,448 |
| API error rate | 24.71% |
| HTTP request failure rate | 36.75% |
| Add p50 / p95 / p99 | 19.243 s / 29.892 s / 30.005 s |
| Retrieval p50 / p95 / p99 | 16.901 s / 28.924 s / 30.006 s |
| Job completion p50 / p95 / p99 | 30.968 s / 53.648 s / 60.056 s |
| PostgreSQL connections first / max / last | 1 / 92 / 57 |
| PostgreSQL observer failures | 0 |

All 601 accepted extraction jobs completed, all 620 outbox records converged, and no job remained
unfinished at the traffic-end snapshot. The frozen durable audit passed: single winner, winner
alignment, event idempotency, provenance, version-chain integrity, and outbox convergence all had
zero violations. The run is a performance/reliability failure, not a durable correctness failure.

## Boundary classification

- **Primary initiating boundary:** authentication's 200 ms outer cache deadline plus unconditional
  shared-circuit `force_open`.
- **Secondary amplification:** cache-service Redis exceptions also unconditionally force the shared
  circuit open.
- **Later independent saturation:** genuine 750 ms circuit-execution deadlines eventually reached
  the normal five-failure threshold; PostgreSQL fallback then grew to 92 observed connections.
- **Not causal for this run:** TCP preflight and holdout/provider behavior.

## One isolated next experiment

Change only the API-key authentication cache-timeout transition policy: replace its direct
`force_open` on the 200 ms outer lookup/fill timeout with one normal circuit failure observation,
preserving the existing five-failures-in-ten-seconds threshold, fallback behavior, Redis timeouts,
TCP-preflight behavior, cache semantics, and authentication result.

Acceptance for the experiment:

- a single auth wrapper timeout cannot open the shared Redis circuit;
- an actual five-failure sequence still opens it;
- authentication fallback correctness remains 100%;
- auth-triggered `force_open` count becomes zero;
- circuit-open gates, PostgreSQL fallback growth, API errors, and dropped arrivals improve materially;
- no regression in durable correctness or cleanup.

Wait for approval before implementing this experiment.

## Artifacts

- `artifacts/internal-benchmarks/scale/redis-circuit-transition-moderate-20260824/redis-circuit-transition-analysis.json`
- `artifacts/internal-benchmarks/scale/redis-circuit-transition-moderate-20260824/service-logs.raw.log`
- `artifacts/internal-benchmarks/scale/redis-circuit-transition-moderate-20260824/k6-moderate.json`
- `artifacts/internal-benchmarks/scale/redis-circuit-transition-moderate-20260824/postgres-observer.json`
- `artifacts/internal-benchmarks/scale/redis-circuit-transition-moderate-20260824/traffic-end-snapshot.json`
- `artifacts/internal-benchmarks/scale/redis-circuit-transition-moderate-20260824/final-audit.json`

Cleanup removed 79 proxy users, 553 Qdrant points, and 19 run-scoped Redis keys. No run-scoped
database rows remained, and the disposable containers/network were destroyed.
