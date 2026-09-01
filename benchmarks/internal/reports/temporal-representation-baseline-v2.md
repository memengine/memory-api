# Temporal representation baseline v2

## Scope

Development-only temporal validity representation. Holdout was not accessed. Extraction,
conflict resolution, authority, lifecycle activation, and retrieval semantics were unchanged.

## Implemented

- Nullable `effective_from` / `effective_until` on tenant memories and claim revisions.
- Source values are read only from provenance `scope`, normalized to UTC, and validated.
- Closed intervals require `effective_until > effective_from` in application and PostgreSQL.
- Claim revisions copy the memory interval unchanged.
- Transactional outbox/Qdrant payloads carry both interval fields.
- Backward-compatible Alembic migration; legacy rows remain null.

## Verification

- Focused unit suites: 31 passed.
- Temporal representation subset: 16 passed.
- PostgreSQL temporal constraint plus concurrent-claim regression: 2 passed before the
  subsequent power interruption.
- Migration applied and verified at `add_temporal_validity_fields (head)` before interruption.
- Frozen temporal benchmark: 18 scenarios, 17 product-evaluable, 15 passed, 2 product
  failures, 1 harness error; product success 88.24%.

The two product failures are intentionally unchanged: historical `as_of` retrieval and
event-time-aware retrieval are not implemented. The harness error is the PostgreSQL source
event deduplication node missing `DATABASE_URL` in the host subprocess. A container rerun was
attempted after the benchmark, but Docker Desktop was unavailable following the power cut.

## Result

The isolated representation repair passes its intended schema, propagation, vector-payload,
and database-constraint boundaries. It does not activate future memories, expire intervals,
or provide historical retrieval. Those remain separate future experiments.
