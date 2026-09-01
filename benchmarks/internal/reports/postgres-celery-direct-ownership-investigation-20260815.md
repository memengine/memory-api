# PostgreSQL Celery/Direct Ownership Investigation — 2026-08-15

Status: diagnosis complete; no production connection-lifecycle repair applied.

## Method

The disposable scale stack was labelled with benchmark-only `PGAPPNAME` values per service. The quality-gate task's direct SQLAlchemy engines were additionally passed through the existing benchmark-only instrumentation. Normal production behavior, pool defaults, task semantics, retries, and business logic were unchanged. Holdout was not accessed.

Focused instrumentation/safety tests passed 12/12.

## Findings

At 2 req/s with default 20+30 async pool settings, PostgreSQL reached 48 sessions and the background Celery service owned 24—half the total. API module-level and region async pools held 17 combined.

The confirmation run at 1 req/s directly identified the dominant churn boundary:

- `increment_tenant_budget_usage` created a new SQLAlchemy engine/pool for every task delivery;
- 69 physical PostgreSQL connections were opened by that call site in three minutes;
- the engine is local to the task function and is not explicitly disposed;
- connections remain until SQLAlchemy engine/pool garbage collection, producing timing-dependent retained-session peaks;
- the run peaked at 29 sessions and ended at 16 after some garbage collection.

This explains why independently reducing API/region pool sizes did not reliably bound total sessions: background quality-gate tasks create a parallel stream of short-lived pools outside those budgets.

`WebhookEventService` also created 525 eager sync engine objects in the API process during the confirmation run, but opened no physical connections. It remains a significant allocation inefficiency, not the confirmed PostgreSQL session-growth repair.

Vector outbox remained process-scoped and created one labelled pool per participating worker process. Watchdog created infrequent labelled pools. Neither matched the quality-gate connection creation rate.

## Integration-gate clarification

The earlier absolute-path post-load integration process continued after the shell timeout and eventually emitted its aggregate: 4/5 suites passed. Fault injection, integration reliability, governance integrity, and lifecycle activation passed. Temporal memory remained 17/18 with the already-visible out-of-order/event-time product failure; it is separate from PostgreSQL pool ownership.

## One isolated proposed repair

Make the quality-gate task session factory process-scoped per Celery child, shared by `increment_tenant_budget_usage` and `send_budget_alert`, and dispose its bound engine on `worker_process_shutdown`. This mirrors the retained vector-outbox ownership pattern.

Do not change task payloads, budget calculations, alerts, retries, queues, transaction boundaries, circuit behavior, pool sizes, or PostgreSQL defaults.

Acceptance:

- quality-gate engines: at most one per participating Celery worker process;
- quality-gate physical connections no longer scale with task count;
- frozen 10-minute LOW PostgreSQL peak <=50 and post-drain sessions <=30;
- zero PostgreSQL exhaustion and pool-checkout timeouts;
- API error rate <=0.5%, zero unfinished jobs, and complete outbox convergence;
- budget increments remain exactly once per accepted logical operation;
- all six durability invariants, focused unit tests, FAST, and relevant integration reliability suites remain green;
- no holdout/provider cost.

