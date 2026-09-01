# Originating-agent deletion correctness baseline v1

## Result

Frozen development baseline: **9/12 passed (75%)**. Production behavior was not changed and
holdout was not accessed. The run used real PostgreSQL foreign-key deletion, Qdrant, the
configured embedding provider, worker authorization and universal API readback.

| Metric | Result |
|---|---:|
| Private-memory deletion correctness | 0% / undefined policy |
| Shared-memory continuity | 100% |
| Provenance/source preservation | 0% relationally |
| Claim-chain integrity | 100% |
| Current-winner correctness | 100% |
| Grant cleanup | 100% |
| Queued-work revocation | 100% |
| Qdrant lifecycle consistency | 0% |
| Cross-agent/user/tenant leakage | 0 |
| Privacy deletion correctness | 100% |

Regression suite: **35 passed**. All temporary PostgreSQL and Qdrant fixtures were cleaned.

## Actual current behavior

Deleting a `GlobalAgent` directly in PostgreSQL:

- deletes its API keys and grants by cascade;
- preserves universal memory, claim, revision and version rows;
- sets memory/revision/version source-agent foreign keys to null;
- does not recalculate active memory or claim winners;
- does not enqueue any Qdrant operation;
- leaves vectors retrievable to surviving authorized agents;
- leaves their Qdrant `source_agent_id` payload pointing at the deleted agent;
- blocks queued work because the worker can no longer find an active write grant.

Observed state: five memory rows survived; four memory source IDs, four revision source IDs and
four version actor IDs became null. All four tested vectors survived and three retained stale
source-agent payloads. Shared API retrieval continued. A recent memory's metadata snapshot still
made its source readable through the API, but older rows without snapshots lost source identity.

## Confirmed weaknesses

1. **No governed operation:** global-agent deletion is not an API/service lifecycle at all.
2. **Undefined private ownership:** Passport memories have user/category grants but no
   private/shared ownership field, so “delete agent-private memory” has no enforceable meaning.
3. **Provenance orphaning:** relational source and version actor IDs are nulled. Metadata helps
   only for newer rows and is not a complete historical guarantee.
4. **Vector drift:** Qdrant retains a source-agent value that no longer exists in PostgreSQL and
   no outbox event reconciles it.

Claims and winners did not become orphaned, surviving grants remained scoped correctly, and no
leakage occurred.

## Privacy deletion is separate

UUI privacy erasure physically removed the user's Qdrant vectors and cascaded database state.
Source-agent account deletion retained user-owned shared memories. These must remain separate
operations.

## One explicit governed lifecycle policy/repair

Implement **agent retirement/tombstoning instead of physical source-agent deletion**:

- set `GlobalAgent.is_active=false`;
- revoke all agent API keys and grants atomically;
- preserve the agent row as an immutable source tombstone so memory/revision/version foreign keys
  and Qdrant source IDs remain valid;
- keep existing user-owned Passport memories and claims unchanged and available only through
  surviving grants;
- rely on the existing final worker grant check to reject queued writes;
- reserve UUI privacy erasure/hard deletion for actual data removal.

Under this policy, current Passport memories are user-owned shared records; agent-private memory
does not exist and should not be inferred. If agent-private storage is later required, it needs a
separate explicit ownership/scope design rather than deletion heuristics.

Acceptance for the proposed repair: 100% provenance/source preservation, shared continuity,
grant/key cleanup and queued-work blocking; no stale Qdrant source identity; claims/winners
unchanged; zero leakage; privacy erasure unchanged.
