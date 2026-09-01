# API-key auth deadline MODERATE experiment

Date: 2026-08-23  
Valid run: `moderate-auth-deadline-frozen-20260823`  
Decision: **failed and reverted**

## Isolated change

The two API-key authentication-local `asyncio.wait_for(..., timeout=0.2)` wrappers around Redis cache GET and SET were removed. Redis connect/command timeouts remained 500 ms and the shared circuit execution deadline remained 750 ms. No pool, cache, retry, fallback, extraction, retrieval, workload, or correctness semantics were changed.

Focused auth/circuit/Redis tests passed 28/28 before load and FAST passed 8/8.

## Harness correction

An initial run used Compose fallback settings (`TCP preflight=enabled`, legacy cache invalidation) rather than the frozen comparison settings. It also reached PostgreSQL client exhaustion. That run is classified as a harness/configuration failure and is excluded from the product decision.

The stack was destroyed and recreated. The valid run explicitly verified:

- `BENCHMARK_REDIS_TCP_PREFLIGHT=disabled`
- `BENCHMARK_CACHE_INVALIDATION_MODE=generation-v1`
- `BENCHMARK_CACHE_NAMESPACE=v2`
- dedicated `memoryos-scale` stack
- deterministic provider, zero provider cost
- holdout unused

## Valid frozen result

| Metric | Result | Reference | Acceptance |
|---|---:|---:|---:|
| Completed iterations | 2,798 | 2,322 | materially improve; insufficient |
| Dropped iterations | 6,800 | 7,278 | materially improve; insufficient |
| API error rate | 27.13% | 16.84% | <=0.5%; fail |
| HTTP failure rate | 18.38% | 21.01% | <=0.5%; fail |
| Add p50 / p95 / p99 | 6.112 / 30.002 / 30.004 s | 15.532 / 30.001 / 30.004 s | fail |
| Retrieval p50 / p95 / p99 | 6.951 / 30.002 / 30.004 s | 14.296 / 30.001 / 30.004 s | fail |
| Job p50 / p95 / p99 | 15.104 / 38.644 / 53.984 s | 23.021 / 43.651 / 51.450 s | fail |
| Auth cache hits / lookups | 5,591 / 7,730 | 1,389 / 4,250 | 72.33%; target >=95%; fail |
| DB/bcrypt fallbacks | 2,101 | 2,861 | target <=1% after warm-up; fail |
| Caller-local auth timeouts | 0 | 69 | expected improvement |
| Redis connection/pool errors | 76 / 76 | 245 / 245 | improved, but not zero |
| Circuit deadlines / fallbacks | 641 / 18,066 | not directly comparable / 23,324 | sustained cascade remained |
| HTTP 500 | 351 | 0 | zero required; fail |
| PostgreSQL too-many-client signatures | 81 | 0 | zero required; fail |

The removal reduced caller-local cancellation and improved medians, cache-hit rate, connection errors, and completed work. It also allowed Redis/circuit waits to occupy request tasks longer during contention. The API still hit the 40-VU ceiling, circuit deadlines accumulated, database fallbacks remained high, and PostgreSQL exhaustion/HTTP 500s appeared. The isolated change therefore shifted the failure mode rather than restoring MODERATE capacity.

## Correctness and drain

After bounded drain:

- jobs: 981 completed, 2 still processing
- queue wait p50/p95/p99: 4.555 / 61.475 / 126.622 s
- DB completion p50/p95/p99: 9.386 / 68.229 / 129.142 s
- outbox: 942 done, 89 failed at final snapshot; audit later observed 93 unconverged
- single winner, winner alignment, idempotency, provenance, and version-chain integrity passed
- outbox convergence and no-unfinished-job requirements failed

## Decision and next step

The two auth-local wrappers were restored. The experiment-only delayed-cache test was removed. Post-revert focused tests passed 27/27 and FAST passed 8/8 with zero product failures, harness errors, provider calls, or holdout access.

Do not increase the Redis pool or repeat deadline tuning next. The next isolated step should be benchmark-only attribution instrumentation: label every Redis client role and record live pool counters plus circuit caller role. That will identify which consumer opens the shared circuit and whether connections are being cancelled, churned, or held before another behavioral change is proposed.
