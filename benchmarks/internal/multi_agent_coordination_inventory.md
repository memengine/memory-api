# Multi-agent coordination architecture inventory

Development baseline only. Holdout is excluded.

## Write and coordination path

`/v1/universal/memories/add` authenticates a `GlobalAgent` plus `UniversalUser`, checks an
active grant, optionally deduplicates the API request in Redis, and queues
`extract_universal_memory`. The worker rechecks an active `read_write` grant before extraction,
creates `UniversalMemory` plus a version, calls `UniversalClaimLedgerService`, and enqueues a
transactional vector upsert only when the new memory remains active.

## Claim behavior

- Claim identity is derived from normalized semantic subject/predicate/value parsing.
- PostgreSQL advisory locks serialize writes by `(user_uui_id, claim_fingerprint)`.
- First assertion activates. Same-value repetitions become archived `asserted` revisions.
- A different value from a Passport agent becomes archived/disputed; the existing winner stays.
- `user_correction` can replace the winner, but another Passport agent cannot supersede it.
- Universal claims do not contain authority priority, source-event time, or observation time.
- Unlike tenant claim revisions, universal claim revisions have no database partial unique index
  enforcing one activated revision per claim.
- Worker retries are transaction-safe, but the worker payload has no durable source-event or
  idempotency key. API Redis idempotency does not make repeated worker delivery exactly once.

## Revocation and deletion

- The worker rechecks the grant, so revocation before worker execution blocks persistence.
- A grant can be revoked while extraction is running after the check; no lock or second check
  exists before commit.
- There is no explicit global-agent deletion/deactivation lifecycle service. Database deletion
  cascades API keys/grants and sets `UniversalMemory.source_agent_id` and claim-revision source
  agent IDs to null. Whether provenance survives then depends on metadata snapshots.
- `Agent.memory_scope` belongs to tenant-local agents and is not a Passport coordination rule.

## Existing coverage

Unit tests cover basic universal disputes, user correction, backfill, API idempotency, grant
revocation and outbox writes. Tenant PostgreSQL tests cover concurrent single winners and source
event deduplication, but those constraints do not apply automatically to universal claim tables.
No frozen full coordination suite previously covered universal concurrency, duplicate worker
delivery, authority/time ordering, deletion, or cross-agent provenance together.
