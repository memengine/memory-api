# Historical retrieval and Qdrant retention inventory

Scope: tenant backend, development-only investigation. Production retrieval is unchanged.

## Current supersession and deletion behavior

- Conflict `UPDATE`/`MERGE` records a version, marks the predecessor archived and enqueues a
  transactional Qdrant delete. The replacement receives a new vector upsert.
- Manual conflict selection similarly archives/deletes losing vectors and upserts a newly
  activated winner.
- Soft memory deletion archives PostgreSQL state and enqueues physical vector deletion.
- Hard memory deletion enqueues vector deletion and removes the PostgreSQL memory row.
- Proxy-user GDPR deletion enumerates every memory ID, enqueues every vector deletion, deletes
  memories and the proxy user, and records an audit event.
- Stale-memory decay uses transactional vector deletion; the separate weekly lifecycle manager
  still has a legacy direct-delete path.
- The outbox worker retries physical Qdrant operations; PostgreSQL remains authoritative.

## Effects of retaining superseded vectors

### Current retrieval

Retained points would require an explicit lifecycle state plus validity filters on every
Qdrant/current-cache path. Temporal bounds alone are insufficient for ambiguous, rejected,
manually deleted, or unbounded superseded rows. A missing/legacy payload field could leak old
state.

### Conflict and version semantics

PostgreSQL uses `is_archived`, predecessor links, versions and claim winners. Qdrant currently
represents only retrievable active state. Retention would turn Qdrant into a partial historical
store and require atomic synchronization of claim/lifecycle status metadata.

### Storage growth

Index size would grow with every revision instead of active memories. Conflict-heavy tenants
would accumulate embeddings indefinitely unless a separate history retention policy existed.

### Privacy and deletion

Supersession retention must never apply to hard deletion, user erasure, consent revocation or
retention expiry. The outbox schema would need distinct archive-for-history and erase actions;
reusing current delete semantics would be unsafe.

### Lifecycle filtering

Future/expired filters now protect current reads, but lifecycle state transitions and hot-cache
invalidation are still incomplete. Retaining old vectors before those invariants are complete
would increase leakage risk.

## Baseline question

The frozen scenario pack measures whether PostgreSQL validity filtering followed by importance,
effective-start and access-time ordering is adequate when several semantically different
memories overlap the same period. Only a material relevance failure can justify the retention
complexity above.
