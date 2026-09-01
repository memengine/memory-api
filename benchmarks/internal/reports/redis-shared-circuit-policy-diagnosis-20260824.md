# Redis shared-circuit policy diagnosis — 2026-08-24

Status: read-only diagnosis complete; no production behavior changed.

## Architecture inventory

The Redis breaker is a singleton within each process. API middleware and services in one API
process share the same local Redis breaker, but API and Celery processes do not share Redis circuit
state: `CircuitBreaker._load_state` deliberately returns local state for the Redis breaker.

Configuration: five failures in ten seconds, 30-second recovery timeout, and a 750 ms execution
deadline. Every `CircuitBreaker.call` already records Redis execution failures before re-raising.

Seven Redis-facing components additionally force the same breaker open after catching the
re-raised exception:

| Component | Catch/wrapper call sites | Candidate forced opens |
|---|---:|---:|
| Authentication middleware | 7 | 0 after the reverted candidate change |
| Generic cache service | 15 | 62 |
| Quota manager | 3 | 124 |
| Proxy-user service | 3 | 57 |
| Rate limiter | 1 | 28 |
| Quality gate | 4 | 22 |
| UUI service | 5 | 0 in this workload |

Qdrant and LLM services also use `force_open`, but on their own non-Redis breakers; they are outside
this diagnosis.

## Confirmed duplicate transition ownership

In the candidate run, all 293 Redis `force_open` calls occurred within 100 ms of a normal breaker
failure record from the same API process; 292/293 were within 20 ms and 278/293 within 5 ms.

Typical sequence:

1. Redis `GET` times out at 534.8 ms.
2. `CircuitBreaker.call` records the execution failure at 765.8 ms.
3. Quality gate catches the same exception and calls `force_open` 0.7 ms later.

Therefore, callers and the breaker both own state transitions for the same failure. The caller's
second transition bypasses the configured five-failure policy. In the reference run, 108/219
forced opens were similarly paired within 100 ms; the unpaired majority were primarily
authentication's separate 200 ms wrapper timeout.

## Confirmed state-machine race

The candidate emitted 653 events whose resulting state was `OPEN`, but many were redundant:

- forced open: 122 `CLOSED -> OPEN`, 171 `OPEN -> OPEN`;
- threshold failure: 13 `CLOSED -> OPEN`, 310 `OPEN -> OPEN`;
- half-open failure: 37 `HALF_OPEN -> OPEN`.

There were 135 genuine `CLOSED -> OPEN` transitions. Of the 134 gaps between them, 114 were under
the configured 30-second recovery timeout, with a minimum gap of 1 ms.

The implementation explains this: an operation admitted while the circuit is closed can finish
successfully after another concurrent operation opens it. `_record_success` then unconditionally
writes `CLOSED`, even when the current state is `OPEN`. The next failing request immediately opens
it again. Concurrent in-flight operations also continue recording `OPEN -> OPEN` failures.

This means the 30-second recovery timeout is not reliably enforced under concurrency. The system
oscillates between Redis attempts and fallback, amplifying both Redis work and PostgreSQL fallback
instead of providing a stable degraded interval.

## Failure boundary ranking

1. **Circuit state-machine invariant:** late successes can close an open circuit before recovery.
2. **Duplicate transition ownership:** callers force open after the breaker has already recorded the
   same failure.
3. **Wrapper deadline mismatch:** authentication's 200 ms wrapper can expire before the Redis
   500 ms socket and 750 ms circuit deadlines.
4. **Broad exception scope:** several generic cache paths catch `Exception` and force the Redis
   breaker open even when the exception is not proven to be a Redis availability failure.
5. **Observability terminology:** Redis circuit state is process-local, not cross-process shared.

## One isolated repair proposed

Repair only the state-machine success transition in `CircuitBreaker._record_success`:

- a success may clear accumulated failures while the state is `CLOSED`;
- a successful probe may transition `HALF_OPEN -> CLOSED`;
- a late success must never transition `OPEN -> CLOSED` before the 30-second recovery timeout.

Do not yet remove caller `force_open` calls or change thresholds, timeouts, retries, fallbacks,
Redis commands, cache semantics, or authentication behavior.

Acceptance criteria:

- a delayed success from an operation admitted before opening cannot close the circuit;
- recovery timeout remains enforced under concurrent successes/failures;
- exactly one half-open success closes the circuit normally;
- premature `CLOSED -> OPEN` reopen gaps under 30 seconds fall to zero, excluding an explicitly
  completed half-open recovery cycle;
- redundant `OPEN -> OPEN` transitions are measured but do not extend/corrupt recovery state;
- frozen authentication/cache correctness tests remain green;
- frozen MODERATE durable correctness remains 100%;
- API errors, drops, PostgreSQL connections, and circuit gates are reported without weakening
  thresholds.

Wait for approval before implementing this repair.

## Evidence

- `artifacts/internal-benchmarks/scale/redis-circuit-transition-moderate-20260824/`
- `artifacts/internal-benchmarks/scale/auth-normal-failure-moderate-20260824/`
- `benchmarks/internal/reports/redis-circuit-transition-diagnosis-20260824.md`
- `benchmarks/internal/reports/auth-circuit-normal-failure-experiment-20260824.md`
