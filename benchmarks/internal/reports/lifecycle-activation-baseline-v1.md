# Lifecycle activation and expiration baseline v1

## Scope

Development-only current-production baseline for tenant memory lifecycle. No production
behavior was changed. Holdout, extraction prompts, conflict/authority rules, retrieval
ranking, SDKs and scale testing were excluded.

## Result

- 14 frozen scenarios.
- 13 product-evaluable scenarios.
- 7 passed, 6 product failures, 1 harness error.
- Lifecycle success: 53.85%.

## Strengths

- Importance decay and durable-memory safeguards pass.
- Decay retry/idempotency passes.
- Existing outbox retry behavior passes.
- Existing claim reconciliation and single-winner behavior pass.
- Historical `as_of` validity read passes.
- Timezone handling and stale-job single-requeue behavior pass.

## Confirmed failures and boundaries

1. **Current retrieval boundary:** normal retrieval does not exclude future or expired
   validity intervals. This creates premature-activation and expired-memory leakage risk.
2. **Lifecycle service boundary:** the weekly manager never examines `effective_from` or
   `effective_until`; no scheduled activation/expiration state transition exists.
3. **Claim integration boundary:** lifecycle has no claim-winner/revision transition path.
4. **Outbox integration boundary:** lifecycle auto-archive deletes Qdrant vectors directly
   instead of creating a transactional outbox event.
5. **Cache boundary:** hot-tier lifecycle payloads omit validity fields and have no temporal
   invalidation transition.
6. **Scheduler boundary:** Celery has weekly decay/lifecycle scheduling but no semantic
   validity transition task, so timely activation/expiration and restart catch-up are absent.

The PostgreSQL interval-constraint node was a harness error because the host subprocess had
no `DATABASE_URL`; the same constraint test had passed in the configured container during
the preceding temporal-representation slice.

## Risk ranking

1. Current retrieval leakage before activation or after expiration.
2. Missing atomic memory/claim transition and single-winner synchronization.
3. Direct Qdrant lifecycle mutation outside the transactional outbox.
4. Missing restart-safe scheduler/catch-up execution.
5. Temporally stale hot-tier cache.

## One proposed isolated improvement

Add validity filtering to **current retrieval only** across cache, hot-tier, Qdrant payload,
cold-start and PostgreSQL fallback branches:

`effective_from IS NULL OR effective_from <= now`, and
`effective_until IS NULL OR effective_until > now`.

This first repair should not activate/archive rows, change claims, create scheduler tasks, or
alter lifecycle/outbox behavior. It closes immediate read leakage while leaving state-machine
work isolated for the following experiment.

Acceptance criteria:

- Premature activation rate: 0%.
- Expired-memory leakage: 0%.
- Current valid and unbounded memory recall: 100% on the frozen scenarios.
- Existing `as_of` historical behavior remains 100%.
- Tenant/agent/category isolation remains unchanged.
- Current retrieval ranking and semantic cutoff remain unchanged for eligible memories.
- Existing retrieval, security and temporal suites remain green.
