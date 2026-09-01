# Lifecycle current-validity retrieval repair v2

## Isolated change

Current retrieval now excludes candidates that have not reached `effective_from` or have
reached `effective_until`. The same deterministic, UTC-aware predicate is applied to Redis
retrieval cache, hot-tier cache, Qdrant payload candidates, cold-start results and PostgreSQL
fallback results. Missing validity fields remain backward-compatible and mean unbounded.

Hot-tier lifecycle payloads now preserve `effective_from` and `effective_until` so the same
read-time predicate can be applied. Historical `as_of` retrieval remains PostgreSQL-owned and
unchanged.

No stored active/archive state, claim status, scheduler, lifecycle transition, ranking weight,
semantic cutoff, conflict rule or extraction behavior changed. Holdout was not accessed.

## Verification

- Current validity/lifecycle/retrieval/vector/as-of suites: 38 passed; four expected frozen
  lifecycle-state contracts failed.
- Broader retrieval/outbox/temporal regression suites: 16 passed.
- Frozen lifecycle baseline: 14 scenarios; 13 product-evaluable; 9 passed; four product
  failures; one environment harness error; success improved from 53.85% to 69.23%.

## Acceptance result

- Premature activation leakage contract: passed.
- Expired-memory leakage contract: passed.
- Valid/unbounded memory preservation: passed.
- Historical `as_of`: passed.
- Existing retrieval ranking and cutoff tests: passed.
- Existing agent/category/vector retrieval tests: passed.

## Remaining frozen failures

1. No semantic activation/expiration state-transition manager.
2. No claim winner/revision synchronization during temporal transitions.
3. Lifecycle vector changes do not yet use the transactional outbox.
4. No restart-safe Celery validity-transition schedule.

The PostgreSQL interval guard remains a harness error in the host runner because
`DATABASE_URL` is absent; it previously passed in the configured container.

## Next proposed isolated improvement

Implement a deterministic, idempotent PostgreSQL temporal-transition service that processes
due `effective_from`/`effective_until` rows under row locks and updates memory, claim winner,
claim revisions, versions and transactional outbox in one transaction. Schedule wiring should
remain a later separate slice; first validate the transition operation directly and under
retry/concurrency.
