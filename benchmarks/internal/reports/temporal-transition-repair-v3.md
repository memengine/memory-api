# Temporal transition repair v3

## Isolated production change

`MemoryLifecycleManager` now processes due semantic-validity transitions inside its
existing PostgreSQL transaction. Candidate memory rows and their claim rows are locked.
Expiration archives the memory and activated revision, clears the claim winner, records a
version, and writes a retained-vector lifecycle payload to the transactional outbox. A
valid direct predecessor may be restored without rerunning conflict or authority logic.

Activation is deliberately restricted to archived rows explicitly marked
`metadata.lifecycle_state=scheduled`; ordinary archived conflict losers cannot be
reactivated accidentally. Concurrent/retried processing is idempotent. No Celery schedule
was added in this slice.

Legacy inactivity auto-archive now enqueues its vector deletion transactionally rather
than calling Qdrant directly.

## Results

- Frozen development lifecycle baseline: 14 scenarios, 13 product-evaluable.
- Passed: 12/13 (92.31%), improved from 9/13 (69.23%).
- State-transition, claim-alignment and outbox areas: 100%.
- Remaining product failure: restart-safe/timely Celery validity-transition schedule.
- PostgreSQL interval guard: passed when run with the configured database environment.
- New real PostgreSQL concurrency regression: passed. Two concurrent expiration attempts
  produced one logical transition, one version, one outbox row, no activated revision and
  no claim winner.
- Relevant unit regressions: 57 passed.
- PostgreSQL transition/constraint tests: 2 passed.
- Holdout used: no.

## Integration defect discovered and corrected

The first PostgreSQL run exposed an async lazy-load attempt while constructing the vector
payload. Transition queries now eager-load the embedding model, keeping payload generation
inside valid async SQLAlchemy execution.

## Next separate slice

Add restart-safe Celery scheduling/catch-up for this already-tested transition operation.
Do not combine that scheduling work with extraction, conflict, authority, ranking, or
temporal-interpretation changes.
