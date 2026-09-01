# PostgreSQL Async Pool Ownership Investigation — 2026-08-15

## Scope

Read-only attribution under the frozen 10-minute LOW workload in disposable project `memoryos-scale-pooldiag2-20260815`. Production pool behavior and settings were not changed. Holdout was not accessed.

## Result

The LOW run is **not accepted**. PostgreSQL did not exhaust connections, but total sessions peaked at 61/100 and two independently sized async pool families retained 33 sessions after drain.

| Owner | Evidence label | Peak observed | Post-drain | Configured capacity |
|---|---:|---:|---:|---:|
| Module-level `SessionLocal` | `mosb:7:a:2` | 18 | 18 | 20 + 30 overflow |
| Region pool | `mosb:7:a:5` | at least 12 | 15 | 20 + 30 overflow |
| Unlabelled worker/direct paths | empty application name | 39 | 8 | not fully attributable |

The module-level pool is used by auth/universal-auth and background task paths. Request dependencies and region-aware middleware use the region pool. Both point to the same PostgreSQL instance in the scale stack, but own independent SQLAlchemy pools. Their combined theoretical capacity alone equals PostgreSQL `max_connections` (100), before worker, observer, migration, or administrative sessions.

`api.db.database` also constructs two module-level async engines: exported `engine`, then a second engine bound to `SessionLocal`. The first did not open a connection during this run, so it is construction waste but not the measured session-growth cause.

The region pool constructed three async engines for configured regions; only the active benchmark region opened material connections. This is expected architecture rather than evidence of a leak.

Webhook service construction created many sync engine objects, but they did not account for the retained async connections in this run and remain a separate concern.

## Workload outcome

- Completed/dropped iterations: 907 / 294
- API error rate: 4.08%; HTTP request failure rate: 3.71%
- Add p50/p95/p99: 2.441 s / 18.718 s / 30.002 s
- Retrieval p50/p95/p99: 1.460 s / 6.974 s / 30.002 s
- Job completion p50/p95/p99: 4.284 s / 8.090 s / 10.349 s
- PostgreSQL pool timeout/exhaustion events: 0 / 0
- Drained jobs: 384 completed, 0 unfinished
- Outbox: 422 done, 0 pending
- Correctness audit: passed all six invariants

## Boundary diagnosis

This is a pool-ownership/capacity architecture issue, not a transaction leak: sessions are retained idle by separate healthy pools, no pool timeout or PostgreSQL exhaustion occurred, all jobs drained, and correctness passed. The late request timeouts occurred while the system remained overloaded and are not attributable to a confirmed connection-exhaustion event.

The benchmark label gap for some Celery/direct sessions is harness coverage drift. It limits full worker attribution but does not invalidate the confirmed 33-session duplication between the two API async pool owners.

## One proposed isolated repair

Introduce one shared, explicit **per-process PostgreSQL connection budget** and apply it to both module-level and region async engine construction. Keep separate pool ownership because pre-region authentication/control-plane access and region-routed data access are different boundaries; do not merge their semantics.

First test a small benchmark-only set of pool-size/overflow allocations under the unchanged LOW workload. Promote a production default only if evidence supports it.

Acceptance criteria:

- peak PostgreSQL sessions <= 50 and post-drain idle sessions <= 30;
- zero connection exhaustion and pool timeouts;
- API error rate <= 0.5%, zero unfinished jobs, and converged outbox;
- no material p95/p99 regression versus the accepted Redis Candidate C behavior;
- all correctness invariants and post-load FAST/INTEGRATION gates remain green;
- region isolation/routing and auth behavior remain unchanged.

