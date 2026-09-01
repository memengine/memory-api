# Full-path governance/privacy baseline v2

## Scenario

One frozen development scenario exercised:

API source event -> extraction job -> Celery -> PostgreSQL memory/claim/evidence -> explicit
correction and supersession -> version history -> transactional outbox -> real Qdrant ->
retrieval/API provenance readback -> hard deletion of active and superseded memories.

Holdout was not accessed and production behavior was not changed.

## Result

- Passed: no
- Failure boundary: `privacy_claim_ledger`
- Duration to failure: 21,980.21 ms
- Harness failures: one stale ORM reference on the first run; corrected in the harness only
- Confirmed product failures: one

## Boundaries passing before deletion

All 20 pre-deletion checks passed:

- API requests queued both extraction jobs
- Celery completed initial extraction and correction
- original and corrected memories persisted
- predecessor link and active/superseded state were correct
- exactly one activated claim revision remained
- version history and API history readback were correct
- source-event provenance, claim evidence and decision evidence were preserved
- PostgreSQL remained authoritative during index lag
- outbox rows synchronized successfully
- real Qdrant/current retrieval returned only the corrected memory with provenance
- hard-delete API accepted both active and superseded memory IDs
- both memory rows were removed from PostgreSQL

## Confirmed failure

After both memory rows were hard-deleted:

- claim revisions were removed
- `active_memory_id` was null
- `winning_revision_id` was null
- the parent claim remained
- claim status incorrectly remained `active`
- `active_value` retained the deleted value (`jaipur`)

This is a privacy/governance correctness failure, not only orphan cleanup. Personal claim
data survives a hard deletion and the ledger advertises an active claim with no winner.

The scenario intentionally stopped before evaluating final vector/API absence, as required
by the first-failing-boundary rule. Fixture cleanup still removed the benchmark proxy user
and Qdrant points.

## Exact boundary

`MemoryService.delete_memory(hard_delete=True)` deletes the `Memory` row and enqueues vector
deletion, but does not govern its `MemoryClaim` before ORM/database cascades remove or detach
the associated revisions.

## One isolated repair proposal

Add hard-delete claim-ledger cleanup inside the same PostgreSQL transaction:

1. Lock claims/revisions referencing the target memory.
2. Delete the memory and its revisions as today.
3. If a claim has no remaining revisions, delete the claim.
4. If revisions remain, deterministically reconcile winner/status/value from those remaining
   revisions without changing conflict or authority semantics.
5. Keep the existing transactional vector-delete outbox operation.

Acceptance:

- no deleted value/evidence remains when the last claim revision is hard-deleted
- no active claim exists without active memory and winning revision
- deletion of one revision does not erase legitimate remaining revisions
- active/superseded hard deletion both behave correctly
- retry is idempotent
- PostgreSQL and Qdrant deletion complete
- API current/history readback exposes no deleted memory
- tenant isolation and existing conflict/lifecycle suites remain green
