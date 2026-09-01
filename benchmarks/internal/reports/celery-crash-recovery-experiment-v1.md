# Celery crash/recovery experiment v1

## Scope

Development-only live experiment against the production API, extraction task, PostgreSQL,
Redis/Celery, claim ledger, transactional outbox, and Qdrant paths. Holdout data was not used
and production behavior was not changed.

## Architecture observed

- Scale jobs use `scale-extraction`; the worker runs with concurrency 4 and prefetch 1.
- Extraction jobs enter `processing` with a ten-minute `stale_after` deadline.
- The watchdog requeues only jobs that remain `processing` past that deadline.
- The extraction task and Celery app do not explicitly enable late acknowledgement.

## Runs

| Run | Result | Classification |
|---|---|---|
| v1 | Job completed before interruption | Harness timing miss |
| v2 | Job completed before interruption | Harness timing miss |
| v3 | Worker killed; recovery measurement reached Qdrant network error | Harness/environment error; fixture cleaned |
| v4 | Job completed before direct kill took effect | Harness timing miss |

The worker restart logs for v4 contain no receipt of the benchmark task after restart. Therefore
v4 cannot be treated as broker redelivery or watchdog recovery evidence.

## Valid durable observations

For every completed attempt:

- one memory row
- one claim revision and one activated winner
- one source event
- one outbox row
- one Qdrant point
- provenance preserved
- no duplicate logical state observed

The final measured completion path took 5,030.48 ms after the recovery command began. This is
not a crash-recovery latency because the job was already complete.

## Conclusion

The live crash/recovery outcome is **inconclusive**. The extraction call completes inside the
Docker CLI interruption latency, so none of the fully measured runs proved a job remained stale
after worker death. Existing deterministic watchdog/fault-injection coverage remains the current
recovery evidence; this experiment does not replace or strengthen that baseline.

The next experiment should add one development-only synchronization barrier immediately after
`_set_db_job_processing` and before extraction. It should be disabled by default, scoped to a
unique benchmark job ID, and release only after the worker is killed. That creates a provable
crash boundary without changing extraction, retry, idempotency, or production behavior.

## Cleanup

All temporary proxy users, memories, claims/revisions, source events, outbox rows, Qdrant points,
and benchmark service writers were removed. The tenant plan was restored to `free`.
