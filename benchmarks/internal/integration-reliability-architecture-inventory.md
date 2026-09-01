# Integration reliability architecture inventory

Scope: development-only core backend. Holdout, SDK, MCP, dashboard, retrieval quality, and load testing are excluded.

## Production path

`POST memory add` delegates to `MemoryService.queue_memory_add`, which performs tenant-scoped cache idempotency, persists an `ExtractionJob` and optional unique `MemorySourceEvent`, dispatches `process_extraction_job` through Celery, and exposes job/memory readback from PostgreSQL. The worker calls `run_extraction_pipeline`, creates a source conversation, extracts, invokes `ConflictResolver` with `auto_commit=False`, commits memory/claim/version/provenance/outbox state, and then records job completion.

## Reliability boundaries

- Request duplication: Redis idempotency is tenant scoped. Source events also have a PostgreSQL uniqueness and payload-mismatch boundary.
- Worker retry: job attempts/dead-letter state are persisted; the stale-job watchdog uses a guarded transition.
- Transactions: the conversation is committed first. Memory, claim, version, provenance and outbox changes share the main transaction; failure rolls it back and separately marks the conversation failed.
- Manual resolution: tenant/UUI routes call `apply_conflict_selection`, which changes archive state, records versions, updates claims, and enqueues vectors before route commit.
- Qdrant: PostgreSQL is authoritative. Outbox rows are atomically claimed, retried, and terminally fail after three attempts.
- Claim concurrency: fingerprint uniqueness exists, but no database invariant or row-lock protocol guarantees exactly one activated revision during concurrent winner updates.

## Coverage composition

Existing endpoint, lifecycle, extraction, conflict-resolution, provenance PostgreSQL, and outbox tests are reused as scenario executors. The frozen development manifest composes them into one baseline. Narrow contracts cover transaction rollback and the missing concurrent-claim winner invariant. Production behavior is unchanged.
