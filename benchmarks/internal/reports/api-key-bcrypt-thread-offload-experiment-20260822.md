# API-key bcrypt thread-offload experiment

Date: 2026-08-22  
Run: `moderate-bcrypt-thread-20260822`  
Decision: **failed acceptance and reverted**

## Change tested

Only the synchronous API-key `verify_api_key(...)` call in the async authentication path was
offloaded with `await asyncio.to_thread(...)`. Authentication semantics, Redis deadlines and
circuit behavior, cache format, database fallback, extraction, retrieval, claims, and the frozen
MODERATE workload were unchanged.

The disposable `memoryos-scale` stack used the deterministic benchmark provider. No holdout or
paid provider was used. Pre-load FAST passed 8/8 and INTEGRATION passed 5/5.

## Frozen MODERATE result

| Metric | Result | Acceptance |
|---|---:|---:|
| Completed / dropped iterations | 2,556 / 7,039 | capacity target not met |
| Interrupted iterations | 6 | 0 |
| API error rate | 1.80% | <=0.50% |
| HTTP failure rate | 1.53% | <=0.50% |
| Add p50 / p95 / p99 | 7.056s / 11.582s / 30.001s | p95 <0.5s; p99 <1s |
| Retrieval p50 / p95 / p99 | 8.835s / 14.142s / 30.002s | p95 <0.75s; p99 <1.5s |
| Job p50 / p95 / p99 | 13.134s / 28.502s / 39.661s | p95 <10s |
| Warm-cache approximation, full-run log | 1,456 / 9,565 = 15.22% hits | >=95% after warm-up |
| Database fallbacks / bcrypt checks | 8,109 / 8,109 | <=1% after warm-up |
| HTTP 500 log lines | 3 | 0 Redis-related 500s |
| Jobs after extended drain | 161 queued, 4 processing | 0 unfinished |
| Outbox after extended drain | 1,064 done, 4 pending | 100% converged |

The first durable audit snapshot passed single-winner, winner-alignment, event-idempotency,
provenance, version-chain, and then-current outbox checks with zero violations. Continued worker
completion subsequently created four new pending outbox rows, so the final convergence and
unfinished-job gates did not pass.

## Diagnosis

The change removed direct event-loop blocking but delegated every cache-miss bcrypt operation to
the default, effectively unbounded-for-this-workload thread pool. During observation the API
container reached about 881% CPU. Concurrent bcrypt work amplified CPU contention, Redis
cache-lookup deadline misses, database fallback, and further bcrypt work. This is a cache-miss
stampede/CPU-admission problem rather than evidence that unrestricted `asyncio.to_thread` is a
safe production repair.

Compared with the preceding auth-deadline run, API errors improved from 26.12% to 1.80%, but the
result remains outside acceptance and latency stayed orders of magnitude above the frozen gates.
The cache hit rate also fell from 61.00% to 15.22%.

## Decision and next isolated investigation

The thread offload and its temporary heartbeat test were reverted. Post-revert focused
auth/Redis/circuit tests passed 24/24 and FAST passed 8/8. Run-scoped cleanup removed all proxy,
memory, claim, revision, job, source-event, outbox, Qdrant, and Redis fixtures; the disposable
containers and volumes were destroyed.

Before another repair, diagnose bounded authentication admission and per-key single-flight cache
fill. The key question is whether one verification/database fallback per API-key cache miss can
serve concurrent waiters without changing authentication, Redis, or cache semantics. Do not
combine that investigation with quota, feedback, webhook, or worker tuning.
