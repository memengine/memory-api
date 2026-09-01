# Redis connection, pool, and request-saturation diagnosis

Date: 2026-08-22  
Evidence: `moderate-redis-preflight-coalesced-20260822`  
Status: **diagnosis only; production behavior unchanged**

## Conclusion

The dominant initiating boundary is routine write-side cache invalidation performing Redis
keyspace `SCAN` operations, not Redis max-client exhaustion or a wait queue inside the connection
pool.

`CacheService.invalidate_user_cache()` scans both `retrieve:{user}:*` and
`hot_memory:{user}:*` after memory mutations. Under the frozen mixed workload, `SCAN` produced
527 of 753 Redis command timeouts (70.0%). A caught invalidation timeout calls
`force_open()` on the shared Redis circuit. Authentication then loses its cache and executes the
database/bcrypt fallback. This feedback loop creates more concurrent I/O and new Redis connection
attempts while the API is already saturated.

The failed preflight-coalescing experiment removed most duplicate physical probes but could not
break this separate invalidation/circuit/authentication loop.

## Boundary evidence

### Redis commands

| Command/boundary | Success samples | Timeout errors | Notes |
|---|---:|---:|---|
| `SCAN` | 1,918 | 527 | 70.0% of command timeouts |
| `GET` | 872 | 83 | authentication/cache reads |
| `HELLO` | 82 | 39 | new-connection handshake |
| `EXPIRE` | 96 | 31 | cache/queue metadata |
| `CLIENT` | 141 | 29 | timeout errors only; unsupported-response telemetry excluded |
| `WATCH` | 94 | 13 | transactional Redis work |
| Other commands | - | 31 | `HINCRBY`, `ZADD`, `INCRBY`, and `SET` |

There were 2,445 sampled `SCAN` responses and 762 persisted memories. This is consistent with
repeated multi-pass invalidation during write traffic rather than an isolated background scan.

### Connection versus pool classification

| Observation | Result |
|---|---:|
| Connection timeout errors | 446 |
| Pool-acquisition timeout errors | 446 |
| Failed connections marked newly created | 446 / 446 |
| Existing-connection successful samples | 2,394 |
| Newly-created successful samples | 59 |
| Redis max-client / max-connection errors | 0 |
| Redis OOM / eviction errors | 0 |

The installed redis-py pool obtains a connection and then calls `ensure_connection()` inside
`get_connection()`. A connection timeout therefore appears once as a connection error and again
as the enclosing pool-acquisition error. Exact one-to-one counts and `created=true` on every
failure show connection-establishment churn; they do not prove exhaustion of a bounded pool wait
queue. No `MaxConnectionsError`, server rejection, OOM, or eviction evidence was present.

### Shared-circuit and authentication amplification

| Metric | Result |
|---|---:|
| Authentication cache lookups | 3,255 |
| Cache hits | 134 = 4.12% |
| Explicit cache timeouts | 68 |
| Database/bcrypt fallbacks | 3,121 |
| Bcrypt p50 / p95 / p99 | 301.82 / 479.79 / 568.05 ms |
| Aggregate observed bcrypt time | 997.41s |
| Database-fallback p50 / p95 / p99 | 1.547 / 6.808 / 14.661s |
| Shared Redis fallback log entries | 28,189 |

`verify_api_key()` is synchronous inside the async authentication path. This diagnosis does not
recommend changing it here—the previous isolated offload/single-flight experiments were rejected.
It is recorded as an amplifier: once the shared circuit prevents cache use, repeated bcrypt work
materially reduces API request-path capacity.

### Other services

- PostgreSQL showed no pool-timeout telemetry; all 814 accepted jobs completed after drain.
- The outbox converged 857/857 with zero pending rows.
- Qdrant/httpx read failures produced 21 retrieval HTTP 500 responses. They began after Redis
  timeout activity was already present and were distributed across later workload deciles.
- Completed retrieval phase telemetry had route p50/p95/p99 of
  6.876/14.612/23.365s. Semantic retrieval itself was 3.162/8.401/12.401s; proxy hydration was
  1.267/3.602/5.403s; feedback was 1.320/3.977/6.249s.
- The host heartbeat reported maximum scheduling lag of 83.141 ms and no over-100 ms anomalies,
  but API-process runtime telemetry was disabled. Host heartbeat therefore cannot rule out API
  event-loop blocking. This is an observability limitation, not a product failure.

Redis timeouts occurred from the first workload decile and increased as concurrency accumulated:
53, 89, 132, 112, 170, 229, 240, 215, 232, and 174 timeout events by log decile. Qdrant 500s
appeared from decile two onward. This supports a cascading request-path saturation model rather
than a late isolated Redis outage.

## Exact failure chain

1. A memory mutation calls `invalidate_user_cache()`.
2. Routine invalidation performs two wildcard `SCAN` traversals.
3. A scan or connection exceeds the 500 ms Redis timeout.
4. The catch path force-opens the process-wide Redis circuit.
5. Auth, quota, cache, and other Redis consumers enter fallback together.
6. Authentication cache misses execute database lookup plus synchronous bcrypt.
7. API concurrency remains occupied longer, increasing Redis/Qdrant I/O latency and new Redis
   connection churn.

The first boundary is cache invalidation; connection creation and authentication are downstream
amplifiers. PostgreSQL durability, Celery drain, claim correctness, and outbox convergence are not
the initiating failures.

## One isolated proposed repair

Replace wildcard `SCAN` during **routine per-user cache invalidation** with a durable per-user
cache-key registry:

- register each dynamic retrieval/hot-tier cache key in a Redis set scoped to that user;
- invalidate by reading that bounded set and deleting/unlinking only its registered keys plus the
  set itself;
- preserve existing key TTLs and cache-miss behavior;
- keep exhaustive hard/privacy deletion explicit and distinct, so privacy cleanup cannot depend
  only on TTL expiry;
- do not change Redis timeouts, pool type/size, circuit behavior, authentication, extraction,
  retrieval ranking, or lifecycle semantics in the experiment.

This targets the confirmed 70% command-timeout source without hiding Redis failures or combining
another auth/pool optimization.

Proposed acceptance for a frozen MODERATE comparison:

- zero routine wildcard `SCAN` calls after warm-up;
- Redis command timeouts reduced at least 80% from 753;
- connection and mirrored pool-acquisition timeouts reduced at least 80% from 446;
- warm authentication cache hit rate at least 95%;
- database/bcrypt fallback at most 1% after warm-up;
- API errors at most 0.5%, with existing latency thresholds unchanged;
- cache invalidation correctness 100%, including stale-result and deleted-memory non-leakage;
- hard/privacy deletion removes all indexed cache material;
- all durable correctness, drain, outbox, tenant isolation, and provider-cost gates remain green.

Wait for approval before implementing this repair.
