# Quality-Gate Process-Scoped Session Factory Repair — 2026-08-15

Status: **repair retained; overall LOW baseline still not accepted**.

## Change

`increment_tenant_budget_usage` and `send_budget_alert` now share one plain SQLAlchemy session factory per Celery child PID. The bound engine is disposed on `worker_process_shutdown`. Task payloads, transactions, budget calculations, alert semantics, retries, queues, circuit behavior, PostgreSQL pool defaults, and other business logic are unchanged.

## Ownership result

- Quality-gate PostgreSQL connection opens: 69 in the three-minute baseline -> 2 in ten-minute LOW.
- Quality-gate engines: one in each of two participating background-worker children.
- PostgreSQL peak: 60 before repair -> 43 after repair.
- PostgreSQL exhaustion and pool checkout timeouts: 0.

The isolated lifecycle boundary is fixed and does not scale engine count with quality-gate task count.

## Frozen LOW result

- Iterations/dropped: 1,068/133
- API errors: 0.187%; HTTP failures: 0.376%
- PostgreSQL peak/observer-final/drained: 43/36/37
- Add p50/p95/p99: 2.528/5.470/10.371 s
- Retrieval p50/p95/p99: 1.750/4.967/6.543 s
- Job p50/p95/p99: 4.452/8.956/20.127 s
- Jobs: 456 completed, 0 unfinished
- Outbox: 477 done, 0 pending
- All six durability invariants passed
- Provider cost: $0; holdout not accessed

## Regression gates

- Focused tests: 24/24 passed
- FAST before LOW: 8/8 passed
- FAST after LOW: 8/8 passed
- Focused integration reliability: 1/1 passed

## Decision

Retain the repair because the confirmed per-task engine churn was eliminated, PostgreSQL peak fell below 50, and correctness/regression gates remained green.

Do not establish an accepted LOW baseline yet. The drained-session target remained failed at 37 (>30), and frozen add/retrieval latency thresholds still failed. These remaining failures are not grounds to restore the proven 69-connection task-local churn.

The next scale investigation should target request latency/worker scheduling and the remaining stable idle owners separately; no further blind pool-size tuning is justified.

