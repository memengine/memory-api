# MODERATE event-loop blocking diagnosis

Date: 2026-08-22  
Evidence run: `moderate-auth-deadline-repair-rerun-20260822`  
Status: diagnosis only; no production behavior changed; holdout not used

## Finding

The highest-confidence event-loop blocking boundary is synchronous API-key bcrypt verification inside the async authentication middleware.

`AuthMiddleware._authenticate_api_key` calls `verify_api_key`, which directly calls `bcrypt.checkpw`, on the FastAPI event-loop thread. The disposable stack runs one Uvicorn process. When the Redis circuit falls back to a miss, concurrent requests each perform bcrypt inline before the event loop can service Redis, database, and response work promptly. This helps sustain the cache-miss/circuit-open feedback loop.

## Measured evidence

| Boundary | Count | p50 | p95 | p99 | Maximum |
|---|---:|---:|---:|---:|---:|
| Complete API-key auth | 5,428 | 87 ms | 12.741 s | 31.175 s | 91.700 s |
| bcrypt verification | 2,119 | 361 ms | 536 ms | 659 ms | 885 ms |
| Quota response envelope | 5,567 | 159 ms | 4.087 s | 7.512 s | 36.183 s |
| Retrieval feedback persistence | 1,306 | 270 ms | 6.025 s | 8.960 s | 34.373 s |
| Retrieval route total | 1,306 | 3.075 s | 19.328 s | 34.426 s | 63.026 s |
| Webhook sync-factory construction | 10,606 | 0.72 ms | 2.86 ms | 12.01 ms | 583 ms |

The 2,119 bcrypt checks account for approximately 762 seconds of synchronous request-thread wall time during a 20-minute run. No async/threaded bcrypt path exists in the repository.

Other correlated evidence:

- cache hit rate reached only 61.00%; 2,112 requests still used the database fallback;
- 24,066 Redis circuit-open fallbacks and 2,084 sampled Redis errors occurred;
- Redis itself was not previously observed as resource-saturated;
- webhook construction created at least 10,600 request-owned synchronous factories, although direct factory-construction telemetry totalled only about 14 seconds;
- quota envelope and retrieval-feedback tails are largely database/Redis waiting and are amplifiers, but are not the clearest direct event-loop CPU blocker;
- the authentication fallback also commits `last_used_at` on every verified miss, increasing database work, but the SQLAlchemy path is asynchronous.

## Boundary classification

1. **Direct event-loop blocker:** synchronous bcrypt in async API-key authentication.
2. **Request-path amplification:** repeated `last_used_at` commits after cache misses.
3. **Redundant lifecycle/resource churn:** request-created webhook synchronous session factories.
4. **Tail-latency amplification:** quota-envelope Redis/DB fallback after route completion.
5. **Retrieval critical-path durability work:** retrieval-feedback commit and refresh before response.

These should not be changed together. Prior isolated timeout and webhook-factory experiments did not meet MODERATE acceptance, so another combined repair would make attribution impossible.

## One isolated proposed repair

Offload only `verify_api_key(raw_api_key, key_hash)` from the async authentication path using `await asyncio.to_thread(...)` (or the equivalent bounded application executor already used by the service, if one is identified during implementation).

Keep unchanged:

- API-key comparison and rejection semantics;
- cache keys, TTL, GET/SET deadlines and circuit behavior;
- database query and `last_used_at` commit behavior;
- Redis retries/fallbacks;
- quota, feedback, webhook, extraction, retrieval, claim and lifecycle behavior.

## Acceptance criteria

Before MODERATE, add a focused concurrency regression proving that a deliberately slow verification does not block an independent event-loop heartbeat.

For the unchanged frozen MODERATE workload:

- API error rate at most 0.5%;
- zero Redis-related HTTP 500s;
- cache hit rate after 30-second warm-up at least 95%;
- database fallback and bcrypt verification after warm-up each at most 1%;
- no material bcrypt verification correctness regression;
- add p95 below 500 ms and p99 below 1 s;
- retrieval p95 below 750 ms and p99 below 1.5 s;
- job p95 below 10 s;
- zero unfinished jobs after drain;
- outbox convergence 100%;
- all winner, idempotency, provenance, version-chain and isolation invariants pass;
- focused auth/circuit tests, FAST and required integration gates remain green.

If the repair reduces event-loop stalls but the frozen MODERATE gates still fail, revert it and diagnose the next measured boundary separately; do not combine quota, feedback, or webhook changes into the same experiment.
