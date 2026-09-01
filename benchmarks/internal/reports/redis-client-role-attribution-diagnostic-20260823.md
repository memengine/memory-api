# Redis client-role attribution diagnostic — 2026-08-23

## Scope

- Disposable `memoryos-scale` stack only.
- `DIAGNOSTIC_2RPS`: 2 iterations/s for 3 minutes, 10 VU cap.
- Deterministic benchmark provider; no paid calls; holdout inaccessible.
- Frozen Redis/cache flags: TCP preflight disabled, generation invalidation `generation-v1`, namespace `v2`.
- No Redis, circuit, cache, retry, or business-logic behavior was changed.

## Valid run

Run ID: `redis-role-attribution-2rps-v3-20260823`.

| Measure | Result |
|---|---:|
| Completed iterations | 360 |
| Dropped arrivals | 1 |
| API error rate | 0.278% |
| HTTP 500 | 0 |
| HTTP failure | 1 x 409 |
| Add p50 / p95 / p99 | 60 / 99.4 / 2078.72 ms |
| Retrieval p50 / p95 / p99 | 45 / 71.25 / 485.15 ms |
| Jobs completed / unfinished | 153 / 0 |
| Job queue wait p50 / p95 / p99 | 9.65 / 17.98 / 151.53 ms |
| Job completion p50 / p95 / p99 | 366.03 / 1029.25 / 1231.49 ms |
| Outbox done / pending | 164 / 0 |

## Redis attribution

Successful telemetry is sampled; error telemetry is unsampled.

| Client | Layer | p50 / p95 / p99 | Failures | Observed pool use |
|---|---|---:|---:|---:|
| auth | pool acquisition | 0.086 / 0.147 / 0.163 ms | 0 | 1 / 100 |
| cache | pool acquisition | 0.048 / 0.099 / 0.145 ms | 0 | 5 / 100 |
| auth | command | 0.419 / 0.784 / 0.851 ms | 0 operational | — |
| cache | command | 0.275 / 0.620 / 1.227 ms | 0 operational | — |
| auth | circuit execution | 0.686 / 1.143 / 1.260 ms | 0 | — |
| cache | circuit execution | 0.400 / 0.951 / 1.503 ms | 0 | — |

Ten `CLIENT` negotiation `ResponseError` telemetry events occurred during connection setup (five per role); they did not create command failures, fallbacks, circuit transitions, or HTTP 500s.

Counts by failure type:

- connection establishment timeout: 0
- pool acquisition timeout/error: 0
- Redis command timeout: 0
- circuit execution deadline: 0
- circuit-open fallback: 0
- PostgreSQL connection exhaustion: 0

## Correctness

All audited invariants passed: single winner, winner alignment, event idempotency, provenance, version chains, and outbox convergence. Holdout was not loaded.

## Conclusion

Redis client/pool saturation is not the limiting boundary at this controlled rate. The pool had at least 95% unused capacity and sub-millisecond acquisition latency. The earlier MODERATE collapse therefore should not be addressed by increasing Redis pool size or relaxing Redis deadlines.

The next isolated investigation should correlate the MODERATE tail with PostgreSQL acquisition/transaction telemetry and request-phase timings, especially the known bursts of PostgreSQL connection errors and long add/retrieval waits. Do not change Redis behavior based on this diagnostic.

## Invalidated runs

- `redis-role-attribution-2rps-20260823`: role coverage incomplete; retained only as diagnostic history.
- `redis-role-attribution-2rps-v2-20260823`: invalid because regional startup fell back after the first wrapper signature did not preserve omitted timeout arguments. Its fixtures were cleaned and it is excluded from conclusions.
