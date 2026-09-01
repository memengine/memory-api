# Privacy retrieval-cache invalidation repair v4

## Isolated change

Memory mutation/deletion now invalidates every retrieval cache owned by the affected
proxy/user identity:

- Redis query-result keys
- legacy Redis hot-memory key
- Redis hot-tier memory keys
- process-local retrieval L1 entries
- process-local hot-tier entries
- process-local memory-count entries

Invalidation uses identity prefixes and does not clear unrelated user or tenant entries.
Retrieval ranking, semantic cutoff, Qdrant behavior, claim logic and deletion semantics were
not changed. Holdout was not accessed.

## Before/after

| Measurement | Before | After |
|---|---:|---:|
| Full-path checks passed | 25/26 | 26/26 |
| Post-delete retrieval returned deleted memory | yes | no |
| Post-delete response served from cache | yes | no |
| Post-delete retrieval result count | 1 | 0 |
| Direct deleted-memory GET | 404 | 404 |
| Deleted PostgreSQL memory rows | 0 remaining | 0 remaining |
| Deleted claim/revision rows | 0 remaining | 0 remaining |
| Deleted Qdrant points | 0 remaining | 0 remaining |
| Vector-delete outbox completion | 100% | 100% |

## Verification

- Focused cache/retrieval/deletion tests: 36 passed in 21.38 seconds.
- Broader cache, retrieval, claim, outbox, supersession and conflict tests: 55 passed in
  19.17 seconds.
- Live API -> Celery -> PostgreSQL -> claim/version/evidence -> correction -> outbox -> real
  Qdrant -> retrieval/history -> hard deletion -> final readback: 26/26 checks passed.
- Live scenario duration: 25,326.57 ms.
- Post-delete retrieval: `cached=false`, zero results.
- Temporary PostgreSQL proxy-user fixtures remaining: zero.

## Status

The full-path governance/privacy regression is now established and passing. The earlier
claim-ledger repair and this cache invalidation repair are both retained.
