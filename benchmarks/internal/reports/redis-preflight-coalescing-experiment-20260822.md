# Redis TCP preflight coalescing experiment

Date: 2026-08-22  
Run: `moderate-redis-preflight-coalesced-20260822`  
Decision: **failed frozen acceptance and reverted**

## Isolated change tested

One in-flight TCP connectivity probe was shared per Redis `CircuitBreaker` instance when the
existing 250 ms result cache was stale. The socket probe, 50 ms connect timeout, 100 ms caller
deadline, circuit thresholds/recovery, Redis commands/retries/fallbacks, authentication behavior,
and production timeout defaults were unchanged. The disposable `memoryos-scale` stack used the
deterministic provider; holdout was inaccessible and paid-provider cost was zero.

Pre-load gates passed: focused Redis/auth tests 29/29, FAST 8/8, and INTEGRATION 5/5 in 678.31s.

## Frozen MODERATE result

| Metric | Result | Acceptance |
|---|---:|---:|
| Completed / dropped iterations | 2,066 / 7,534 | capacity target not met |
| Interrupted iterations | 0 | 0 |
| API error rate | 8.86% | <=0.50% |
| HTTP failure rate | 19.63% | <=0.50% |
| Add p50 / p95 / p99 | 16.371s / 30.001s / 30.003s | p95 <0.5s; p99 <1s |
| Retrieval p50 / p95 / p99 | 17.787s / 30.002s / 30.007s | p95 <0.75s; p99 <1.5s |
| Job p50 / p95 / p99 | 24.767s / 46.217s / 51.504s | p95 <10s |
| Queue wait p50 / p95 / p99 | 2.579s / 9.017s / 17.736s | observe |
| Cache hits / lookups | 134 / 3,255 = 4.12% | >=95% after warm-up |
| Cache lookup timeouts | 68 | 0 attributable to aggressive deadlines |
| Database fallbacks / bcrypt checks | 3,121 / 3,121 | <=1% after warm-up |
| HTTP 500 responses | 21 retrieval responses; Qdrant/httpx read failures observed | 0 Redis-related |
| Jobs after drain | 814 completed; 0 unfinished | 0 unfinished |
| Outbox after drain | 857 done; 0 pending | 100% converged |

The durable audit passed single-winner, winner-alignment, event-idempotency, provenance,
version-chain, and outbox checks with zero violations. No retry, dead-letter, PostgreSQL pool
timeout, or holdout event was observed. Host-loop monitoring recorded 24,357 samples, maximum
lag 83.141 ms, and zero over-100 ms anomalies.

## Redis boundary comparison

| Boundary | Diagnosed reference | Coalesced run |
|---|---:|---:|
| Physical TCP probes | 531 | 38 |
| TCP preflight errors | 427 | 32 |
| TCP errors / auth lookup | 3.17% | 0.98% |
| Shared circuit fallback logs | 56,945 | 28,189 |
| Redis command timeouts | 156 | 753 |
| Pool-acquisition timeouts | 4 | 446 |
| Connection timeouts | 4 | 446 |
| Circuit execution timeout/deadline | 16 | 1 |

Coalescing achieved its local mechanism: physical probes fell 92.8%, and preflight errors per
authentication lookup fell about 69%. It did not restore the Redis authentication cache or the
frozen MODERATE service level. Under the saturated run, command/connection/pool timeouts became
the dominant Redis boundary, cache hits fell to 4.12%, and fallbacks remained pervasive.

## Decision

The coalescing implementation and its four temporary tests were reverted. Post-revert focused
Redis/auth tests passed 25/25 using a clean repository-local pytest temp root, and FAST passed
8/8 with zero product failures, harness errors, or provider cost.

Do not tune or combine repairs from this result. The next work should be diagnosis only: correlate
Redis connection and pool-acquisition timeout telemetry with API concurrency, client-pool state,
event-loop scheduling, and Qdrant/request saturation. That evidence should determine whether the
next isolated boundary is Redis client connection creation/pool contention or broader request-path
resource starvation.
