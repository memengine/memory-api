# Conflict-resolution benchmark architecture inventory

## Scope and baseline boundary

This phase benchmarks the tenant/core-memory conflict lifecycle. Retrieval, load,
public benchmarks, and the locked extraction holdout are excluded. Production
conflict behavior is frozen while the baseline is captured.

## Production flows

### Tenant conversation/event flow

`POST /v1/memories` -> `MemoryService.add_memories` -> `ExtractionJob` and
`MemorySourceEvent` -> Celery `process_extraction_job` ->
`run_extraction_pipeline` -> `ExtractionService` -> `ConflictResolver` ->
`Memory`, `MemoryVersion`, `MemoryClaim`, `MemoryClaimRevision`, audit log and
vector outbox -> transaction commit -> memory/job APIs.

The worker creates and commits the source `Conversation` before extraction. The
memory/conflict/claim/version writes are committed together later. On failure it
rolls that transaction back, marks the conversation failed in a separate commit,
and schedules a retry through the extraction job lifecycle.

### Manual conflict flow

Tenant/operator conflict APIs load `CrossUserConflict` and call
`apply_conflict_selection`. That service changes archive state, records
`MemoryVersion`, updates the vector outbox, attaches decision evidence, and asks
`ClaimLedgerService` to reconcile claim revisions. The route owns the transaction.

### Cross-user/shared-context flow

Stored memories emit `SharedContextSignal` records. Detected contradictions create
`CrossUserConflict` records, may auto-resolve through domain rules, or route to a
clarification queue/webhook. Equal-authority cross-writer contradictions in the
same-user resolver also create an archived pending memory plus a tenant-review
conflict.

### Universal/passport flow

Universal memory uses a separate `UniversalClaimLedgerService` and universal
versions. Existing PostgreSQL coverage is materially stronger here than on the
tenant extraction path. This phase records it in the inventory but does not mix
passport behavior into tenant conflict scores.

## State model

- `Memory.is_archived` is the active/superseded state used by tenant memory reads.
- `Memory.previous_version_id` links replacement memory rows.
- `MemoryVersion` snapshots mutations to one memory row.
- `MemoryClaim` holds the logical claim winner, status and authority.
- `MemoryClaimRevision` holds assertions, source event/writer, evidence,
  authority, timestamps, decision evidence and schema/processor versions.
- `MemorySourceEvent` holds writer/source provenance and payload identity.
- `VectorSyncOutbox` makes vector updates transactional with database state.
- `CrossUserConflict` and `ClarificationQueue` represent unresolved review paths.

## Authority and temporal rules

When both sides have explicit category/default authority, higher authority wins
and lower authority is rejected. Equal authority from distinct registered writers
routes to clarification. Provenance observation time can reject a stale incoming
event. Explicit years in both memory propositions can produce `KEEP_BOTH` for
different temporal contexts. Otherwise semantic classification can use the
configured conflict model.

## Duplicate and retry boundaries

- The API idempotency key is cached at the memory-add boundary.
- Source events have a database uniqueness boundary for tenant/source/event ID.
- Cross-user conflict insertion checks both memory orderings.
- Extraction retries reuse the job payload, but end-to-end duplicate memory/claim
  behavior needs benchmark coverage.
- Claim identity has a unique tenant/user/fingerprint constraint.

## Existing test inventory

Unit coverage exists for conflict detection, resolver actions, improved conflict
classification, routing, manual resolution, claim ledger rules, claim versions,
memory versions, provenance and universal claims. Most resolver and manual-flow
tests use fake sessions, fake Qdrant and fake model responses.

PostgreSQL integration coverage exists for source-event deduplication, provenance
retention/redaction, provenance readback, and broad universal/passport claim
governance. Before this benchmark there was no development golden set exercising
the tenant conversation -> extraction worker -> resolver -> tenant claim/version
-> readback chain across revisions.

## Baseline risks found during inventory

1. Tenant claim recording is fail-open: exceptions are swallowed so a memory can
   commit without its claim/revision.
2. `_record_claim_for_memory` passes a `decision_evidence` name that is not in its
   local parameters. This must be treated as a baseline failure, not fixed before
   measurement.
3. The source conversation is committed before the main memory transaction, so a
   mid-pipeline failure intentionally leaves a failed conversation record.
4. Semantic candidate discovery depends on Qdrant being current; vector outbox lag
   can affect whether the next event is considered a conflict.
5. Active state is represented in both `Memory.is_archived` and claim winner state;
   consistency is not enforced by one database constraint.
6. `previous_version_id`, `MemoryVersion`, and claim revisions represent different
   histories and can diverge unless integration assertions cover all three.

## Existing baseline execution

- Focused unit suite: 67 passed.
- Host PostgreSQL tests initially could not run because `DATABASE_URL` was absent;
  this is a harness/environment error, not a product failure.
- Configured-container PostgreSQL suite: 3 passed, covering source-event
  deduplication/redaction and broad passport governance.

