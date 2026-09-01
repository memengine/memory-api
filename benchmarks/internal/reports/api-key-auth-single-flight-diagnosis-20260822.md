# API-key authentication single-flight diagnosis

Date: 2026-08-22  
Evidence run: `moderate-bcrypt-thread-20260822`  
Status: diagnosis only; no production behavior changed

## Architecture finding

`AuthMiddleware._authenticate_api_key` has no admission control or per-key in-flight request
coordination. Every Redis miss, timeout, or circuit-open fallback independently performs:

1. API-key database lookup;
2. bcrypt verification;
3. `last_used_at` commit;
4. Redis cache fill attempt.

The cache key is already safely scoped by the API-key fingerprint, so it is also the natural
identity for coordinating identical concurrent authentication work. The current middleware is a
single in-process object, but no lock, task registry, semaphore, or single-flight mechanism exists
at this boundary.

## Measured stampede evidence

The failed unrestricted thread-offload run used one benchmark API key and recorded:

| Observation | Result |
|---|---:|
| Cache lookups | 9,565 |
| Hits / non-hits | 1,456 / 8,109 |
| Database fallbacks | 8,109 |
| bcrypt checks | 8,109 |
| Peak cache lookups per second | 37 |
| Peak non-hits per second | 34 |
| Peak overlapping bcrypt checks | 33 |
| Peak overlapping database fallbacks | 39 |
| Consecutive non-hit runs | 84 |
| Longest consecutive non-hit run | 941 requests / 154.58s |
| Runs with at least 10 non-hits | 27 |
| Runs lasting at least 30s | 16 |

An offline replay using the observed bcrypt start/end schedule treated the first verification as
the leader and every same-key verification starting before that leader completed as a follower.
It required 999 leaders and collapsed 7,110 redundant checks: an estimated 87.68% reduction on
the observed schedule, with up to 30 followers sharing one result. This is diagnostic potential,
not a predicted production result; reduced contention would change durations and arrival timing.

The roughly 30/60/90-second non-hit windows also align with circuit-open periods. Single-flight
cannot repair Redis availability, but it can prevent all concurrent requests during those periods
from independently multiplying database and bcrypt work.

## Bounded admission versus single-flight

A global bcrypt semaphore alone is not the best next repair. It would cap CPU, but identical
requests would remain queued as redundant database/bcrypt operations and could increase request
latency. Single-flight removes duplicate work rather than merely delaying it.

Single-flight cannot be wrapped around the current synchronous bcrypt call without offloading the
leader: while bcrypt blocks the event loop, followers cannot reach and await the in-flight task.
The smallest coherent repair is therefore one **process-local, per-cache-key authentication
fallback flight** whose leader performs the existing fallback path and offloads its single bcrypt
call; followers await the same task and receive the same `ApiKeyAuthResult` or failure. The task
entry must be removed in `finally` and cancellation of one follower must not cancel the shared
leader.

This should initially remain process-local. A Redis-backed distributed lock would depend on the
same degraded service and introduces lock-expiry and recovery semantics. In a multi-Uvicorn setup,
the maximum duplicate work would initially be one leader per process; that limitation should be
measured separately.

## Proposed isolated repair and acceptance

Implement only per-key in-process single-flight around the existing database/bcrypt/cache-fill
fallback, with bcrypt offload occurring only inside the elected leader. Do not change cache TTL,
Redis deadlines/circuit behavior, authentication decisions, database queries, `last_used_at`,
quota, feedback, webhook, extraction, retrieval, or worker configuration.

Focused correctness/concurrency acceptance:

- concurrent valid requests for one uncached key execute exactly one database fallback and one
  bcrypt verification;
- every waiter receives the same valid authentication result;
- concurrent invalid requests are also coalesced but failures are not cached beyond the flight;
- leader exception/cancellation cleans the registry and a later request can retry;
- cancelling a follower does not cancel the leader or other followers;
- different API keys do not coalesce;
- cache-hit behavior remains unchanged;
- no secret/raw-key value is retained in registry keys or telemetry.

Frozen MODERATE acceptance remains unchanged: API errors at most 0.5%, warm cache hits at least
95%, post-warm database/bcrypt fallback at most 1%, existing latency limits, zero unfinished jobs,
100% outbox convergence, and all correctness/security invariants green. If it fails, revert it and
do not add quota, feedback, webhook, Redis, or worker tuning to the experiment.
