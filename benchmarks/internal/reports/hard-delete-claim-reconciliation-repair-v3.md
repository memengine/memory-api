# Hard-delete claim reconciliation repair v3

## Isolated production change

Hard deletion now locks every claim referenced by the target memory before deletion. After
the memory and its revisions are removed, claims with no revisions are deleted. Claims with
remaining revisions retain an already-activated winner; if none is activated, the claim is
archived and its active value/winner pointers are cleared. Superseded or disputed revisions
are never promoted by deletion.

Conflict detection, authority, extraction, ranking, lifecycle semantics and vector-delete
outbox behavior were not changed. Holdout was not accessed.

## Verification

- Focused memory/claim/conflict regressions: 23 passed in 19.10 seconds.
- Added tests cover deletion of the final claim revision and preservation of an independently
  activated surviving winner.
- The first post-change live run used the prior API process and reproduced the old failure;
  only the API container was restarted to load mounted source.
- The loaded live run passed the original repair boundary:
  - memory rows removed: 100%
  - claim rows/revisions containing deleted values removed: 100%
  - active-without-winner claims: zero
  - vector-delete outbox completion: 100%
  - Qdrant point removal: 100%
  - direct deleted-memory GET: 404

## New downstream failure discovered

The scenario progressed to `privacy_api_readback` and failed because retrieval returned the
deleted corrected memory from cache (`cached=true`) even though PostgreSQL contained no
memory/claim/revision rows and Qdrant contained no point. Duration to this boundary was
24,441.16 ms.

The repaired claim-ledger boundary is successful and retained. The full privacy lifecycle is
not yet correct because deletion cache invalidation does not cover the retrieval cache key(s)
used by this query/user.

## Next isolated repair proposal

Repair hard-delete retrieval-cache invalidation only. Inventory L1 process cache, Redis user
cache, hot tier and query-result cache identities; invalidate every cache namespace that can
return the deleted memory after the PostgreSQL commit. Do not change retrieval ranking,
semantic cutoff, Qdrant behavior, claim logic or deletion semantics.

Acceptance:

- immediate post-delete retrieval never returns either deleted revision
- direct GET remains 404
- PostgreSQL claim/memory absence remains 100%
- Qdrant point absence remains 100%
- deletion retries are safe
- unrelated users/tenants are not invalidated or leaked
- cached retrieval behavior for non-deleted memories remains correct
