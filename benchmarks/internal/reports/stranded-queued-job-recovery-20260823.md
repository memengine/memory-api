# Stranded queued extraction-job recovery

Date: 2026-08-23  
Decision: **accepted**

## Repair

- Successful API-to-Celery dispatch now persists the broker task ID on the queued extraction job.
- The existing watchdog also selects jobs older than the 60-second dispatch grace period only when
  they remain queued, have no broker task ID, and have never started processing.
- Recovery uses a compare-and-set update to reserve a generated Celery task ID before redispatch.
  Concurrent watchdogs therefore produce one dispatch.
- The original tenant/plan queue and persisted payload are preserved.
- If recovery dispatch fails, only that reservation is cleared so a later watchdog cycle can retry.
- Worker start locks the job row and accepts only a queued/failed job whose reserved task ID is
  empty or matches the incoming task. Late original deliveries and concurrent duplicate tasks are
  returned as `duplicate_ignored` before extraction or persistence.
- Existing stale-processing recovery and retry/dead-letter semantics remain unchanged.

No extraction, conflict, authority, provenance, retrieval, Redis, pool, timeout, or ranking logic
was changed. No schema migration was required.

## Validation

- Focused lifecycle/memory/extraction tests: 30/30 passed.
- New real PostgreSQL concurrent-watchdog regression: 1/1 passed.
- Frozen integration-reliability suite: 13/13 passed, zero product or harness failures.
- FAST tier: 8/8 passed.
- Provider calls/cost: zero.
- Holdout: untouched.

The disposable PostgreSQL/Redis/Qdrant/Celery validation stack and volumes were destroyed after
testing.

## Next boundary

Do not rerun MODERATE immediately. The correctness gap is repaired, but the failed MODERATE run
also confirmed a separate Redis connection/pool saturation loop: 245 connection/pool failures,
23,324 circuit fallbacks, 32.68% auth-cache hit rate, and 2,861 database/bcrypt fallbacks. The next
step should be a read-only/instrumented investigation of that boundary before one isolated capacity
experiment.
