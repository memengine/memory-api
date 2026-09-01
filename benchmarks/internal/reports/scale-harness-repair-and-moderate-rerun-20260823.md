# Scale harness repair and frozen MODERATE rerun — 2026-08-23

Status: harness repair passed; frozen MODERATE performance failed; correctness passed. This run is valid and fully evaluable. No product behavior was changed.

## Isolation and gates

- Disposable Compose project: `memoryos-scale`.
- Deterministic provider active; paid-provider calls and cost: zero.
- Holdout excluded.
- Pre-run FAST gate: 8/8 suites passed.
- All seven required services were running; API, PostgreSQL, Redis, and Qdrant were healthy.
- Celery restart succeeded with pidfiles disabled.
- PostgreSQL observer derived credentials from the resolved disposable Compose configuration: 635 samples, zero observer failures.

## Frozen MODERATE result

Workload: 8 arrivals/second for 20 minutes, 20 initial VUs, 12,000 VU cap.

| Metric | Result |
|---|---:|
| Completed / dropped iterations | 2,364 / 7,236 |
| API error rate | 26.14% |
| HTTP request failure rate | 33.98% |
| Add p50 / p95 / p99 | 16.957 / 30.002 / 30.004 s |
| Retrieval p50 / p95 / p99 | 15.831 / 30.002 / 30.007 s |
| Job p50 / p95 / p99 | 26.455 / 52.521 / 58.039 s |
| DB queue wait p50 / p95 / p99 | 2.337 / 16.678 / 84.539 s |
| DB job completion p50 / p95 / p99 | 3.900 / 22.140 / 84.700 s |

MODERATE performance acceptance failed.

## PostgreSQL attribution

- Connections: first 2, last 59, maximum 92 of 100.
- Peak state: 25 active, 50 idle, 17 idle-in-transaction.
- Peak application ownership included two API async pool identities totaling 65 sessions and the background worker role totaling 19 idle sessions.
- Explicit `too many connections`, QueuePool timeout, and logged timeout exceptions: zero.
- Server-side logged HTTP 500 responses: zero.

The system approached the server connection ceiling and accumulated long-held transactions. This is capacity/latency failure, not a correctness failure. Raising the connection limit would hide rather than resolve the waiting boundary.

## Correctness and convergence

- Jobs: 731 completed, zero retrying, zero unfinished after drain.
- Single winning claim revision: pass.
- Winner alignment: pass.
- Durable event idempotency: pass.
- Provenance preservation: pass.
- Version-chain integrity: pass.
- Tenant/user/agent isolation: pass.
- Outbox: one row briefly pending at snapshot, then zero pending; eventual convergence passed.

## Classification and next isolated investigation

The harness defects are resolved and retained. The valid rerun confirms that MODERATE is not an accepted baseline: arrival capacity collapses while database session growth and transaction waiting approach the PostgreSQL ceiling.

The next change should not enlarge pools. Run one isolated transaction-ownership experiment that attributes long transactions and idle-in-transaction sessions to exact API request phases and SQL call sites, with production behavior unchanged. The experiment should identify the smallest transaction-scope boundary that can be shortened before proposing a production repair.
