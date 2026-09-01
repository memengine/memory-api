# Redis circuit failure-ownership deep analysis — 2026-08-24

Status: diagnosis complete; **no production change implemented**.

Evidence source: frozen MODERATE run `circuit-registry-identity-moderate-20260824`.
The registry-identity candidate was already reverted after failing durable correctness.

## Executive finding

The failed experiment did prove that one process-local breaker identity is technically possible.
It failed because the breaker currently mixes three different failure domains:

1. Redis endpoint availability;
2. per-client connection/pool pressure;
3. request-local deadlines and event-loop scheduling delays.

Any caller encountering any of these conditions can invoke `force_open()`. With a shared registry,
that local condition becomes a process-wide Redis outage. The identity repair therefore exposed an
existing failure-ownership defect; identity itself was not the root cause.

## Confirmed failure mechanics

### 1. Caller code bypasses the configured threshold

The Redis breaker is configured for five failures within ten seconds. `CircuitBreaker.call()`
already records Redis command failures. Its callers then catch the same Redis timeout/connection
exception and invoke `_mark_redis_unavailable()`, which calls `force_open()` immediately.

During the run:

- breaker execution failures: 262;
- caller-forced opens: 564;
- force opens within 10 ms of another measured Redis error: 287;
- every non-auth force-open source except one correlated within 10 ms with a breaker execution
  timeout (181/182).

This proves that normal Redis data-path failures are commonly counted by the breaker and then
escalated again by the caller. The five-failure threshold is not governing those transitions.

### 2. Authentication has a contradictory 200 ms outer deadline

The accepted Redis defaults are 500 ms for connect, 500 ms for command execution, and 750 ms for
the circuit execution deadline. API-key authentication still wraps both cache read and cache write
in `asyncio.wait_for(..., timeout=0.2)`.

Therefore authentication cancels the Redis operation after 200 ms, before the configured Redis and
circuit deadlines can do their jobs. It then calls `force_open()`.

Observed authentication behavior:

- auth force opens: 382 of 564 (67.7%);
- cache lookup outcomes: 541 hits, 2,271 misses, 222 explicit timeouts;
- database fallback authentications: 2,493, exactly equal to misses plus timeouts.

The cache-write timeout path also calls `force_open()` but is not represented by the cache-lookup
timeout counter. This explains why auth force opens exceed the 222 read-timeout observations.

Operational consequence: once the shared breaker opens, authentication falls back to PostgreSQL
and bcrypt verification. That fallback is correct but expensive and becomes part of the latency
feedback loop.

### 3. Repeated force-open calls postpone recovery

`force_open()` writes a fresh `opened_at` even when the circuit is already OPEN. The run contained:

- 201 `CLOSED -> OPEN` force transitions;
- 342 `OPEN -> OPEN` force transitions;
- 21 `HALF_OPEN -> OPEN` force transitions;
- median interval between force opens: 13 ms;
- only 32 intervals at least as long as the configured 30-second recovery timeout.

Thus redundant force-open calls continually restart the recovery clock. They are not harmless
duplicate telemetry events.

### 4. Concurrent successes close an OPEN breaker prematurely

`_record_success()` unconditionally writes CLOSED unless the breaker is already cleanly CLOSED.
An operation admitted before another request opens the circuit can complete later and close it.

There were 188 `CLOSED -> OPEN` force transitions less than 30 seconds after a previous force-open.
Because legitimate OPEN recovery requires 30 seconds, those intervening CLOSED states cannot be
explained by normal recovery. They are evidence of late-success state overwrite. The prior isolated
success-guard experiment addressed this local race, but it could not succeed while separate breaker
instances and caller-forced opens remained.

## What the telemetry does and does not prove

### Actual connection pressure

The telemetry recorded 475 connection errors and 475 pool-acquisition errors. However, 457 pool
errors occurred within 1 ms of a connection error. The pool metric wraps the entire
`get_connection()` call, including connection establishment, so these are largely two observations
of one failure—not proof of 475 independent pool-exhaustion events.

The observed boundary is connection establishment/handshake timing out under API saturation. The
available evidence does not prove that the Redis server itself was unavailable or that the pool had
no free slot.

### Instrumentation noise

There were 1,015 `CLIENT` `ResponseError` events (952 cache, 63 auth). These arise during Redis
client connection setup and are not followed consistently by breaker execution failure. They must
not be treated as 1,015 product-visible Redis failures. Future analysis should distinguish ignored
handshake capability responses from command failures returned to application code.

### Outbox failure

All 651 accepted jobs completed, but 38/654 vector outbox rows reached terminal failed state. The
outbox worker stores `last_error` and sends a Sentry message, but does not log the exception locally.
The benchmark snapshot records counts, not error reasons. Exact attribution is therefore impossible
from the retained artifact.

The API logged 52 Qdrant `ResponseHandlingException` occurrences. A plausible inference is that
shared Redis OPEN state increased cache bypass and direct PostgreSQL/Qdrant work, creating downstream
contention that also harmed outbox convergence. This is not confirmed because terminal outbox error
details were not captured before cleanup.

## Harness drift separated from product behavior

- Celery beat/background workers started before migrations and briefly queried a missing
  `vector_sync_outbox` table. These errors occurred before benchmark fixtures and are startup-order
  harness drift.
- The PostgreSQL observer exited without producing its artifact, so peak connection count is
  unavailable for this run.
- Terminal vector-outbox `last_error` values were not included in the snapshot/audit artifact. This
  is an observability gap, not evidence that Redis directly caused the failures.

## Architectural conclusion

A single process-wide breaker is appropriate only for failures that demonstrate shared Redis
endpoint unavailability. Pool acquisition pressure, a 200 ms request-local timeout, or one client's
connection churn should not automatically declare Redis unavailable for every client.

The intended architecture should separate:

- shared endpoint-health circuit state;
- client/pool-local bulkhead pressure;
- request-local timeout/fallback behavior.

Registry identity should not be retried until failure ownership is corrected.

## One isolated repair proposed

Implement **Redis failure ownership** only, while keeping the currently reverted/split registry
behavior unchanged:

1. Redis operations executed through `CircuitBreaker.call()` remain owned and counted by the
   breaker. Callers catch and apply their existing fallback but do not call `force_open()` for the
   same exception.
2. The auth 200 ms outer deadline is treated as one external failure observation, not an immediate
   open. It must not bypass the five-failure threshold.
3. Reserve `force_open()` for explicit operator action or independently verified endpoint-wide
   unavailability—not ordinary command, pool, or request-deadline failures.

This is one policy-boundary repair; it does not change Redis commands, retry behavior, fallback
values, cache semantics, authentication results, thresholds, or registry identity.

### Acceptance criteria

- normal Redis command/connection timeouts cause exactly one failure observation;
- zero caller-forced opens from ordinary auth/cache/quota/quality/proxy/rate-limit operations;
- first automatic OPEN occurs only after the configured five failures within ten seconds;
- no `OPEN -> OPEN` recovery-clock reset from ordinary callers;
- auth database fallback remains correct;
- API error and HTTP failure rates do not regress from the frozen reference;
- all accepted jobs complete;
- outbox convergence and every durable correctness invariant remain 100%;
- focused circuit/auth/cache tests and existing correctness gates remain green.

If this repair passes independently, registry identity and then the late-success guard can be
re-evaluated as separate later experiments.
