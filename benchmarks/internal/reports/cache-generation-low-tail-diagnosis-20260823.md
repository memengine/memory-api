# Cache-generation LOW tail diagnosis

Date: 2026-08-23  
Source run: `cache-generation-v2-low-20260823`  
Scope: captured-artifact diagnosis only; no runtime behavior changed

## Conclusion

Generation-based invalidation is not the primary remaining tail source. It removed request-path
`SCAN`, and its Redis commands (`GET`, `INCRBY`, `HVALS`, `SET`, `EXPIRE`) were normally fast.

The strongest supported causal chain is:

`TCP preflight false/slow failure` -> `shared Redis circuit opens` -> `auth cache becomes
unavailable` -> `database plus bcrypt authentication fallback` -> `API p95/p99 inflation`.

The captured telemetry is sufficient to isolate this chain, but it cannot fully attribute the
remaining time inside add/retrieval route cores because events do not share request IDs and route
core segments were not emitted in this run.

## Evidence

### Authentication

| Metric | Result |
|---|---:|
| Cache hits | 3,355 |
| Cache misses | 332 |
| Cache timeouts | 3 |
| Hit rate | 90.92% |
| Database/bcrypt fallbacks | 335 |
| Cache-hit p50 / p95 / p99 | 1.30 / 3.81 / 7.68 ms |
| Bcrypt p50 / p95 / p99 | 272.01 / 452.54 / 572.30 ms |
| Full DB fallback p50 / p95 / p99 | 353.07 / 1,675.23 / 2,912.69 ms |
| Full DB fallback maximum | 3,275.88 ms |

Across all authenticated requests, auth p50/p95/p99 was 1.57/332.10/1,286.77 ms. There were 65
auth phases above 750 ms and 32 above 1,500 ms.

By endpoint:

- add auth p50/p95/p99: 1.69/614.54/1,661.79 ms; 21 calls exceeded 750 ms;
- retrieval auth p50/p95/p99: 1.68/454.59/1,662.52 ms; 20 calls exceeded 750 ms;
- job-status auth p50/p95/p99: 1.54/20.30/719.63 ms; 24 calls exceeded 750 ms.

Because misses/fallbacks are approximately 9% of authentication calls, they land directly in the
endpoint p95 region. Cache hits themselves are not slow.

### Redis and circuit behavior

| Boundary | Result |
|---|---:|
| Request-path `SCAN` | 0 |
| TCP preflight successes | 362 |
| TCP preflight failures | 48 |
| Failed preflight p50 / p95 / max | 301.11 / 1,041.10 / 1,134.77 ms |
| Actual Redis GET timeouts | 2 |
| Circuit execution deadline failures | 1 |
| Connection acquisition failures | 0 |
| Redis connection failures | 0 |
| Circuit-open fallback logs | 3,229 |
| Redis-related HTTP 500 | 0 |

Successful Redis GET p50/p95/p99 was 0.387/2.105/7.126 ms. Connection acquisition p95/p99 was
0.174/4.092 ms and connection p95/p99 was 0.055/4.195 ms. Thus Redis and its pool were normally
responsive while the separate TCP preflight produced 48 failures and opened the shared circuit.

The 521 Redis `CLIENT` response errors come from connection-label instrumentation compatibility;
they are not GET/SET failures and did not produce HTTP 500 responses.

### Other observed boundaries

- Quota envelope add p95/p99: 380.24/848.31 ms; retrieval p95/p99: 38.99/488.52 ms.
- Region resolution remained below 0.3 ms.
- Webhook session-factory construction event p95/p99 was 3.07/4.94 ms, maximum 197.66 ms.
- PostgreSQL telemetry recorded 320 engine-creation samples, 300 attributed to
  `WebhookEventService`; this is architectural overhead but is not large enough in the captured
  construction timings to explain the multi-second p95 alone.
- All 508 jobs and 563 outbox records converged, with no PostgreSQL correctness or connection
  exhaustion failure.

### Unlocalized remainder

K6 measured add p95 4,155.25 ms and retrieval p95 3,199.60 ms, larger than their auth p95 values.
Auth fallback and shared contention explain a material part of the tail, but the exact remainder
inside route execution cannot be assigned from aggregate, uncorrelated events. It would be unsafe
to change retrieval, database, webhook, or event-loop behavior based on this artifact alone.

## Failure classification

1. **Confirmed reliability boundary:** TCP preflight can fail while actual Redis
   connections/pool remain healthy, opening a shared circuit for otherwise usable Redis.
2. **Confirmed amplification:** circuit-open periods convert one Redis/preflight problem into
   hundreds of expensive database/bcrypt authentication fallbacks.
3. **Secondary architecture concern:** repeated webhook session-factory construction remains, but
   its observed construction time is not the primary tail.
4. **Telemetry limitation:** no request-correlated route-core timing, so the residual endpoint
   tail is not yet localized.
5. **Not implicated:** generation invalidation, Redis `SCAN`, claim correctness, outbox, Qdrant
   convergence, or PostgreSQL durability.

## One proposed isolated experiment

Run a **benchmark-only TCP-preflight bypass experiment** while keeping generation invalidation,
Redis command/connect timeouts, circuit thresholds/deadlines, retry/fallback semantics, auth,
PostgreSQL, and workload frozen.

The circuit breaker would call the real Redis command directly and use its existing connection,
command timeout, and failure result as the circuit signal. Production defaults remain unchanged.
Before LOW, controlled Redis-unavailability tests must prove that real failures still open the
circuit, invoke fallbacks, recover after service restoration, and never leak or return incorrect
data.

Acceptance:

- controlled Redis failure/recovery correctness: 100%;
- request-path preflight calls: 0 in the candidate;
- actual Redis command, connection, and pool failures remain explicitly observable;
- auth cache hit rate at least 95%;
- database/bcrypt fallbacks at most 5% after warm-up;
- circuit-open fallbacks reduced at least 80% from 3,229;
- API error rate at most 0.5% and Redis-related HTTP 500 equal zero;
- add p95/p99 below 500/1,000 ms;
- retrieval p95/p99 below 750/1,500 ms;
- job p95 below 10 seconds;
- zero unfinished jobs and all correctness invariants pass;
- FAST and INTEGRATION gates green;
- no MODERATE run unless LOW passes all frozen thresholds.

Wait for approval before implementing this experiment.

Machine-readable analysis:
`artifacts/internal-benchmarks/scale/cache-generation-v2-low-20260823/tail-diagnosis.json`.
