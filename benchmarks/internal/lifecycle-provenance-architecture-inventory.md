# Durable provenance and lifecycle architecture inventory

Scope: tenant memory backend only. Development data only; extraction quality, retrieval ranking, SDKs, public benchmarks, scale and holdout are excluded.

## Real flow and ownership

`POST /v1/memories` enters `MemoryService`, resolves authenticated writer/source identity through `ProvenanceService`, creates/deduplicates `MemorySourceEvent`, persists `Conversation`/`ExtractionJob`, and dispatches extraction. `extraction_tasks` builds the immutable provenance snapshot and passes source-event/evidence context into conflict persistence. `ConflictResolver` owns memory activation/archive transitions and claim reconciliation. `ClaimLedgerService` owns semantic claim identity, revisions, authority and the activated winner. `VersionService` records historical memory snapshots. PostgreSQL is authoritative. `VectorSyncOutbox` carries upsert/delete payloads to Qdrant; payloads include memory/version/source-event/provenance fields. Retrieval and memory APIs provide readback, while cache invalidation and outbox reconciliation guard stale state.

## Durable field map

| Boundary | Durable identifiers/evidence | Main risk |
|---|---|---|
| Request → source event | tenant, writer, external event ID, occurred-at, authority, evidence refs, payload hash | wrong writer, duplicate or hash collision |
| Source event → extraction job | source-event UUID and retained job context | retry loses source link |
| Extraction → memory | conversation, source-event UUID, metadata provenance snapshot | evidence dropped during persistence |
| Memory → claim revision | memory/source-event IDs, evidence refs, decision evidence | revision split or winner loses evidence |
| Conflict/version transition | previous-version ID, archive flag, claim winner/revision state | multiple winners or disconnected chain |
| PostgreSQL → outbox → Qdrant | memory/version/source-event/provenance payload | stale vector, missing delete, retry duplication |
| Storage → API/retrieval | active state, source event and provenance | archived data or cross-scope leakage |

## Existing infrastructure deliberately reused

- `test_provenance_phase2`: source hashes, writer derivation, authority and retention contracts.
- `test_claim_ledger_service`, claim reconciliation and conflict tests: revision/winner behavior.
- `test_version_service`: ordered versions, tenant isolation and archived exports.
- `test_outbox_pattern`: upsert/delete, retry, batching and reconciliation.
- PostgreSQL tests: concurrent source-event deduplication, retention and concurrent single-winner enforcement.
- Existing integration reliability tests: API dispatch/source forwarding, extraction-to-conflict persistence and rollback.

The new pack composes these into one frozen 24-scenario result surface. It extends rather than replaces the existing suites. Failures are classified as product failures or harness errors and summarized by lifecycle area and integrity metric.

## Known coverage gaps after this first baseline

The repository has strong component and PostgreSQL-boundary coverage, but no single automated scenario currently drives a real API request through an external Celery worker, PostgreSQL commit, outbox worker, real Qdrant indexing and final API retrieval while snapshotting every intermediate row. Cache-stale archival and out-of-order multi-source revision chains also lack full real-service PostgreSQL/Qdrant scenarios. These should be added after the composed baseline identifies whether existing paths are already failing; production logic must remain unchanged during baseline capture.
