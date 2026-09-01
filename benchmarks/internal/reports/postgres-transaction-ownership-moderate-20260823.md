# PostgreSQL transaction ownership under frozen MODERATE — 2026-08-23

Status: diagnosis complete. Production behavior unchanged. MODERATE performance failed; correctness passed.

## Run validity

- Dedicated disposable `memoryos-scale` stack only.
- Frozen workload: 8 arrivals/second for 20 minutes, 40-VU ceiling.
- Deterministic provider; zero paid-provider calls/cost.
- Holdout excluded.
- Pre-run FAST gate: 8/8 passed.
- PostgreSQL observer: 634 successful samples and 8 product-relevant connection failures.

## Workload result

| Metric | Result |
|---|---:|
| Completed / dropped iterations | 1,617 / 7,964 |
| Interrupted at graceful stop | 19 |
| API error rate | 26.96% |
| HTTP failure rate | 43.27% |
| Add p50 / p95 / p99 | 21.224 / 30.001 / 30.003 s |
| Retrieval p50 / p95 / p99 | 24.071 / 30.002 / 30.004 s |
| Job p50 / p95 / p99 | 31.114 / 54.136 / 56.914 s |
| DB queue-wait p50 / p95 / p99 | 3.108 / 28.819 / 63.835 s |
| DB job-completion p50 / p95 / p99 | 5.554 / 38.260 / 76.657 s |

## Confirmed PostgreSQL exhaustion

- Observed connection maximum: 98 of 100.
- Maximum active: 13; maximum idle-in-transaction: 45.
- Eight consecutive observer connections were rejected with PostgreSQL `too many clients already`.
- Final observer sample still had 71 application sessions after traffic ended.

This is a confirmed connection/transaction-lifetime failure, not merely near-capacity telemetry.

## Exact ownership boundaries

### 1. Global API/authentication pool retains transactions

The global async engine (`mosb:7:a:2`) is the engine used by `AuthMiddleware` through `SessionLocal`. Twenty-two distinct PostgreSQL backends were repeatedly observed idle-in-transaction with `BEGIN`; one remained open for at least 926.035 seconds. The same engine's other observed statements were API-key lookup and `api_keys.last_used_at` update.

The responsible scope is `_authenticate_api_key`: it opens a database session, selects candidate API keys, performs synchronous bcrypt verification while the transaction remains open, updates `last_used_at`, commits, and then writes the auth cache. Under overloaded/cancelled requests, this scope retains database transactions while non-database work and event-loop delay occur.

### 2. Retrieval clarification claim serializes request transactions

The largest completed API transaction boundary ended at:

`UPDATE clarification_queue SET status=... WHERE clarification_queue.id=...`

| Count | >2 s | >5 s | p95 | Maximum |
|---:|---:|---:|---:|---:|
| 649 | 601 | 476 | 23.029 s | 51.661 s |

This maps to `_pop_next_clarification_question`, which reads the next pending clarification, mutates it to `triggered`, and commits inside the retrieval request. Concurrent retrievals therefore contend while sharing the broader request-scoped session.

### 3. Other amplified transaction boundaries

| Boundary | p95 | >5 s | Maximum |
|---|---:|---:|---:|
| Worker proxy-user update | 15.456 s | 172 | 26.672 s |
| API extraction-job insert | 6.390 s | 80 | 13.145 s |
| API retrieval-event insert | 7.056 s | 78 | 37.111 s |
| API-key last-used update | 3.257 s | 45 | 13.068 s |

These amplify the overload, but the durable global authentication transactions and clarification serialization are the strongest confirmed ownership failures.

## Correctness and convergence

- 722/722 jobs completed after drain; zero retries and zero unfinished jobs.
- 760/760 outbox events converged.
- Single winner, winner alignment, event idempotency, provenance, version chains, and outbox correctness passed.

## Diagnostic limitation

Python call-stack capture inside SQLAlchemy's async greenlet boundary returned `unknown`. Attribution remains reliable through Compose service, engine/application identity, sanitized SQL shape, observed backend PID, and direct source mapping. Future benchmark labels should include `MEMORYOS_PROCESS_ROLE` to remove the possibility of container-local PID collisions; this is telemetry hygiene, not a product repair.

## One isolated proposed repair

Shorten only the API-key authentication database transaction:

1. Read the minimum candidate key identity/hash data inside a short session and close/rollback it before bcrypt.
2. Perform bcrypt verification outside any PostgreSQL transaction.
3. On success, use a separate short transaction to update `last_used_at`.
4. Keep cache, authentication result, retry, permission, and key-selection semantics unchanged.

Acceptance for the same frozen MODERATE rerun:

- no authentication-pool transaction older than 5 seconds;
- zero persistent idle-in-transaction authentication sessions after drain;
- zero PostgreSQL `too many clients` failures;
- peak connections below 80;
- API error rate materially lower than 26.96% without correctness regression;
- all jobs/outbox drain and all correctness invariants remain 100%.

Clarification-queue atomic claiming should remain a separate later experiment so the effect of the authentication transaction repair can be measured independently.
