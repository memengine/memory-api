# Redis connection/pool saturation diagnosis

Date: 2026-08-23  
Scope: frozen MODERATE run `moderate-command-health-20260823`  
Status: diagnosis only; no production behavior changed; holdout not used

## Conclusion

The run does **not** show Redis server capacity or a configured connection-pool ceiling being exhausted. It shows connection establishment timing out after request-side pressure begins, with each connection failure reported again by the enclosing pool-acquisition timer.

The strongest confirmed initiating boundary remains API-key authentication's local 200 ms `asyncio.wait_for` around Redis GET/SET. That deadline is shorter than the accepted 500 ms Redis connect/command timeouts and 750 ms circuit deadline. When it expires, authentication force-opens the process-wide Redis circuit. Repeated PostgreSQL+bcrypt fallback then saturates the single API process and drives connection creation/cancellation churn.

Increasing Redis pool capacity is therefore unsupported by the evidence and could increase connection churn.

## Client and pool inventory

| Consumer | Process | Client lifetime / pool | Circuit behavior |
|---|---|---|---|
| `AuthMiddleware` | API | Dedicated async Redis client and `ConnectionPool` | Shared process-wide `redis_cb` |
| fallback `CacheService` | API | Separate dedicated async client and pool | Same shared `redis_cb` |
| regional `CacheService` | API | Region-owned async client | Same shared `redis_cb`; regional initialization failed in this run and fallback cache was used |
| circuit state store | API/worker | Separate synchronous client | Stores circuit state; 50 ms socket settings |
| queue router helpers | API/worker | New synchronous client object per helper call | Direct Redis operations |
| Celery broker/result backend | workers | Celery/Kombu-managed pools | Independent from request cache pool |

The instrumented async pool inherits redis-py's default `max_connections=100`. The API uses one Uvicorn process. The disposable Redis server was configured with 512 MiB and no eviction.

## Frozen-run evidence

| Observation | Result | Interpretation |
|---|---:|---|
| Connection errors | 245 | All sampled failures were `TimeoutError` during creation |
| Pool-acquisition errors | 245 | Exact one-for-one wrapping of the connection errors |
| Explicit pool-limit errors | 0 | No `Too many connections`/pool-limit signature |
| Redis `maxclients` errors | 0 | No server client-limit signature |
| Redis-related HTTP 500 | 0 | Fallback remained functional but overloaded |
| Auth cache hit rate | 32.68% | Far below expected stable-key cache behavior |
| Auth DB+bcrypt fallbacks | 2,861 | Major downstream amplification |
| Circuit-open fallbacks | 23,324 | Shared-circuit blast radius |
| PostgreSQL exhaustion | 0 | DB pool was not the initiating boundary |

The first recorded failure pair has a 702.796 ms connection timeout followed by a 704.363 ms pool-acquisition timeout. Later pairs are likewise adjacent and nearly identical (for example 657.274/657.373 ms and 1,054.212/1,054.387 ms). This proves the pool metric is timing the failed connection creation; it is not measuring a separate wait for an available pooled connection.

Successful Redis commands exist between open-circuit periods, and the Redis CPU snapshot was low in the prior instrumented run. The evidence therefore supports an API event-loop/deadline feedback loop rather than Redis server saturation.

## Instrumentation limitation

The benchmark pool events do not currently carry a client-role label or live pool counters. Consequently, the frozen artifact cannot allocate the 245 failures between the auth and fallback-cache client pools. This limits attribution, but it does not affect the conclusion that no pool ceiling was reached: every reported pool error is paired with a connection-creation timeout.

## One isolated capacity experiment

Remove only the two auth-local 200 ms `asyncio.wait_for` wrappers around API-key cache GET and SET, and do not force-open Redis from those caller-local wrapper timeouts. Let the already accepted 500 ms connect/command timeouts and 750 ms circuit execution deadline be the only timeout authority.

Keep unchanged:

- Redis pool type and `max_connections`
- shared-circuit thresholds/recovery semantics
- TCP preflight and fallback behavior
- auth cache key/TTL and bcrypt/database semantics
- worker count, workload, PostgreSQL settings, Redis server settings, extraction, retrieval, and correctness behavior

This directly tests whether cancellation-driven connection churn and the circuit/fallback cascade disappear without obscuring genuine Redis failures.

## Acceptance criteria

Run focused real-Redis concurrent auth tests, then the unchanged frozen MODERATE workload:

- auth cache hit rate after 30-second warm-up: at least 95%
- DB+bcrypt fallbacks after warm-up: at most 1% of authenticated requests
- caller-local auth timeout force-opens: zero
- Redis connection and pool-acquisition timeout failures: zero, or isolated genuine failures without a sustained cascade
- Redis-related HTTP 500: zero
- API error rate: at most 0.5%
- completed iterations and add/retrieval/job tails materially improve
- no unfinished jobs after drain
- correctness invariants, FAST, and required INTEGRATION gates remain green

If it fails, revert it. The next investigation should add benchmark-only client-role/pool-state labels before testing pool topology or sizing; pool size must not be increased speculatively.
