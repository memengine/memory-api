# Multi-agent durable event idempotency repair v1

The isolated repair passed and is retained.

## Reused identity and scope

The worker resolves one existing logical identity in this order:

1. `source_event_id`
2. `event_id`
3. API `idempotency_key`
4. extraction `job_id`

It is scoped by `(universal_user_id, source_agent_id, event identity)`. Tenant ownership is
implicitly fixed by the globally unique source agent. Identical text from another agent, user,
or event is not deduplicated.

Before extraction, PostgreSQL takes a transaction advisory lock on that scoped identity and
checks committed `UniversalMemory.metadata.source_event_id`. An existing outcome returns as an
idempotent replay. New memories store the identity in memory provenance metadata and the outbox
payload. Memory, version, claim revision and outbox creation remain in the same transaction, so
a failed attempt leaves no marker and a retry can complete.

## Validation

- Sequential same-event delivery: **1 memory, 1 version, 1 claim revision, 1 outbox row**
- Concurrent same-event delivery: **1 durable memory; one execution reported replay**
- Distinct event IDs with identical content: **2 durable observation rows**
- Exactly one winning revision: **yes**
- Duplicate active revisions: **0**
- Qdrant identity: one memory ID and one outbox upsert produce one point; redelivery creates no
  additional point or outbox operation

## Frozen coordination baseline

- Passed: **11/17** (previous 10/17)
- Durable duplicate/idempotency correctness: **100%** (previous 0%)
- Revocation enforcement: **100%**
- Conflict detection: **100%**
- Winner correctness: **100%**
- Concurrent single-winner correctness: **100%**
- Cross-agent/user/tenant leakage: **0**
- All unrelated metrics unchanged

Regression suite: **44 passed**. Fixtures were removed and holdout was not accessed. Conflict,
authority, permission, extraction, ranking and sharing semantics were unchanged.

## Remaining highest-risk weakness

Originating-agent deletion has no explicit governed lifecycle. Foreign keys null source-agent
IDs, Passport has no private/shared deletion policy, and immutable source provenance is not
guaranteed for older rows. This combines security semantics with audit loss and should be the
next isolated investigation before authority tuning.

Machine-readable artifact:
`artifacts/internal-benchmarks/multi-agent-coordination-development-v3.json`.
