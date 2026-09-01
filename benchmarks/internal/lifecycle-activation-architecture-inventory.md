# Lifecycle activation and expiration architecture inventory

Scope: tenant core backend, development-only baseline. Holdout, extraction prompts,
conflict/authority rules, ranking changes, SDKs and scale testing are excluded.

## Current production flow

Source provenance can now carry `effective_from` and `effective_until`. They are stored on
`Memory` and `MemoryClaimRevision`, validated in PostgreSQL, and copied into outbox/Qdrant
payloads. Optional `as_of` retrieval reads validity-correct historical state from PostgreSQL.

Current retrieval without `as_of` still uses active/archive state, caches and Qdrant; it does
not filter future or expired intervals. `MemoryLifecycleManager` runs weekly per active tenant
and performs inactivity decay, low-importance archival, hot-tier promotion and deterministic
baseline rescoring. It does not inspect temporal validity.

## Transition boundaries

- Active state: `Memory.is_archived`.
- Logical winner: `MemoryClaim.active_memory_id`, `winning_revision_id`, claim/revision status.
- History: predecessor memory links, memory versions and claim revisions.
- Vector state: transactional outbox for normal conflict/write paths, but lifecycle auto-archive
  currently calls Qdrant deletion directly.
- Cache state: hot-tier payloads omit temporal validity.
- Schedule: semantic lifecycle is weekly; no activation/expiration transition task exists.

## Existing coverage reused

The frozen pack reuses lifecycle decay/idempotency/timezone tests, outbox retry tests,
claim reconciliation, conflict versioning, optional historical retrieval, provenance timestamp
normalization and integration retry contracts. New architecture contracts expose only missing
semantic-validity integration boundaries.

## Expected baseline risks

1. Future memories can be returned by normal retrieval before `effective_from`.
2. Expired memories can remain current after `effective_until`.
3. No atomic memory/claim/version transition exists for activation or expiration.
4. Lifecycle vector deletion can diverge from PostgreSQL on Qdrant failure.
5. Hot-tier cache entries can remain temporally stale.
6. Weekly cadence cannot provide timely scheduled transitions.
