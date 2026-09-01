# Celery crash/recovery experiment v2

## Result

Passed. A development-only, one-shot synchronization barrier held one uniquely identified job
after its PostgreSQL state changed to `processing` and before extraction began. The consuming
`celery-starter` worker was killed with SIGKILL, recreated, and the existing watchdog recovered
the stale job through its normal queue and production extraction path.

The barrier is disabled by default, requires an explicit internal metadata flag, is unavailable
when `APP_ENV` is `production` or `prod`, and uses a job-scoped Redis marker with a five-minute
expiry. A redelivered job consumes no second barrier.

## Live measurements

- Queue: `free-extraction`
- State after worker restart: `processing`
- Watchdog: 1 checked, 1 requeued, 0 dead
- Attempts: 0 before crash, 1 after recovery
- Terminal status: `completed`
- Recovery command to terminal/measurement: 24,904.42 ms
- Memories: 1
- Claims/revisions: 1 revision, 1 activated winner
- Source events: 1
- Outbox rows: 1
- Qdrant points: 1
- Provenance preserved: yes
- Duplicate durable state: none

All eight experiment checks passed: exactly-once logical memory, single revision/winner, single
source event/vector, provenance preservation, completion, and exactly one watchdog requeue.

## Regressions

- Focused unit/contract tests: 8 passed.
- Frozen fault-injection baseline: 29/29 product-evaluable scenarios passed; 0 product failures.
- Three frozen scenarios remained harness errors because host `DATABASE_URL` was absent. These
  are unchanged environment failures, not regressions.

## Cleanup

The benchmark proxy, memory/claim/event/outbox rows, Qdrant point, writer, and Redis barrier keys
were removed. Tenant plan remained/restored to `free`. Both `celery-starter` and `celery-scale`
were healthy after the run. Holdout data was not accessed.
