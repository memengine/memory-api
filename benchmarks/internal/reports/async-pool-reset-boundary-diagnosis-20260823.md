# Async pool reset boundary diagnosis — 2026-08-23

Status: diagnosis complete; pool return/reset defect not reproduced. Production behavior unchanged.

## Scope and method

- Dedicated disposable `memoryos-scale` stack with deterministic provider mode.
- Holdout excluded; no memory writes; zero paid-provider calls/cost.
- Controlled probe: 100 concurrent authenticated job-read requests with a 500 ms client deadline.
- PostgreSQL sampled every 500 ms for 120 seconds.
- Benchmark-only SQLAlchemy pool events captured checkout, reset, check-in, invalidation, process role, backend PID, and asyncpg transaction/closed state.
- Benchmark-only session events correlated ORM session exit with pool behavior.

The Docker rebuild was blocked by a throttled dependency download. To avoid changing the experiment, the current benchmark-only instrumentation and reverted production authentication module were injected into the already isolated disposable API container. This run is boundary-diagnostic evidence, not a production-equivalent performance baseline.

## Results

### Request and session lifecycle

- Client outcomes: 100/100 deliberate client timeouts.
- Session events: 294 entered, 294 exit-started, 294 exit-completed.
- Cancelled exits: 0.
- Exit errors: 0.
- Sessions still reporting an ORM transaction after exit: 0.

### Pool lifecycle

- Captured pool event lines: 233.
- Checkout events: 207.
- Reset events: 16.
- Check-in events: 10.
- Invalidations: 0.
- Distinct PostgreSQL backend PIDs observed: 37.
- Checkouts reporting an open asyncpg transaction: 0.
- Resets reporting an open asyncpg transaction: 0.
- Check-ins reporting an open asyncpg transaction: 0.

Checkout/check-in/reset events are sampled unless an open driver transaction is detected. Open-transaction events are always logged, so their absence is material even though ordinary event counts are sampled.

### PostgreSQL observer

- Samples: 220; observer failures: 0.
- Peak sessions: 51.
- Peak idle-in-transaction: 23.
- Maximum observed transaction age: 4.879 seconds.
- Final sessions: 22, all `idle`.
- Final idle-in-transaction sessions: 0.

## Conclusion

This repeat did not reproduce a connection being checked in, reset, or checked out with an open driver transaction. The transient idle-in-transaction population fully drained within the observation window. The prior remaining `BEGIN` backend is therefore not sufficient evidence of a pool reset defect and was most likely an in-flight/draining transaction captured before completion.

Do not change pool reset, pre-ping, invalidation, or asyncpg transaction behavior based on this evidence.

The confirmed MODERATE-scale failure remains request/session backlog under authentication and clarification work, but the pool is returning connections cleanly after those operations complete. The next isolated diagnosis should attribute long-lived clarification-path transactions and lock/wait time to the exact query and ownership boundary before considering a repair.
