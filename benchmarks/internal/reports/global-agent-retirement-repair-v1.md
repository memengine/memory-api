# Governed global-agent retirement repair v1

The approved tombstone policy passed and is retained.

## Implementation

`POST /v1/agents/global/{agent_id}/retire` is tenant-authenticated and ownership-scoped. In one
database transaction it locks and deactivates the `GlobalAgent`, deactivates all active agent API
keys, and revokes all active grants. It does not delete memories, claims, revisions, versions,
outbox rows or vectors.

Preserving the inactive agent row keeps relational provenance and existing Qdrant
`source_agent_id` payloads valid. The existing final worker grant check blocks queued writes.
UUI privacy erasure remains the separate physical row/vector deletion operation.

Passport memories remain user-owned and grant-governed. No agent-private ownership semantics
were invented; a memory outside surviving grant categories remains inaccessible but retained for
the user.

## Frozen post-repair baseline

- Scenarios: **12/12 passed** (baseline 9/12)
- End-to-end lifecycle success: **100%**
- Private/inaccessible memory governance: **100%**
- Shared-memory continuity: **100%**
- Provenance/source preservation: **100%**
- Claim-chain integrity: **100%**
- Current-winner correctness: **100%**
- Grant/key cleanup: **100%**
- Queued-work blocking: **100%**
- Qdrant lifecycle consistency: **100%**
- Cross-agent/user/tenant leakage: **0**
- Privacy deletion correctness: **100%**

Final focused regression suite: **38 passed**. Temporary PostgreSQL/Qdrant fixtures were removed.
Holdout was not accessed. Conflict, authority, extraction, ranking and sharing semantics were
unchanged.

Machine-readable artifact:
`artifacts/internal-benchmarks/originating-agent-retirement-development-v2.json`.
