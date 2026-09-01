# Historical semantic-ranking materiality baseline v1

## Decision

The current PostgreSQL historical path is validity-correct but not sufficiently accurate when
several unrelated memories overlap the requested period. Semantic ranking is materially needed.
No production behavior was changed and holdout was not accessed.

## Frozen development pack

Eight independently designed scenarios cover past location, employer, database stack, summary
preference, manager, recurring procedure, multi-fact project context and a reverted schedule.
Every case contains at least three temporally overlapping memories, active and superseded rows,
high-importance semantic distractors and one or more relevant historical answers.

## Current PostgreSQL baseline

- Historical Precision@K: **39.58%**
- Historical Recall@K: **100%**
- Historical MRR: **0.375**
- Historical nDCG: **0.542**
- Incorrect historical filler results: **14**
- Current-state leakage into historical queries: **0**
- Historical-state leakage into current queries: **0**
- Provenance preservation: **100%**
- Mean evaluator ranking latency: **0.0167 ms**
- P95 evaluator ranking latency: **0.0439 ms**

Latency above measures the deterministic in-process ranking evaluator, not networked PostgreSQL
or API latency. A live latency comparison belongs in the approved architecture experiment.

The relevant memory appeared third in six scenarios, second in two scenarios, and never first.
Importance/time ordering consistently placed durable identity, goals and preferences ahead of
the memory semantically answering the historical question.

## Qdrant retention risk inventory

Supersession currently physically deletes the old point through the transactional outbox.
Soft deletion, hard deletion, proxy-user GDPR erasure, manual conflict resolution and decay
also delete vectors. Retaining superseded vectors without distinguishing lifecycle archive from
privacy erasure would be unsafe. It would also grow the index with every revision and require
strict current-query filters on all Qdrant/cache paths.

## One proposed isolated architecture experiment

Change only **supersession archival** from Qdrant physical delete to an outbox-driven payload
state update that retains the point with:

- `is_archived=true`
- `lifecycle_state="superseded"`
- existing `effective_from` / `effective_until`
- predecessor/source/provenance metadata

Normal retrieval must continue requiring `is_archived=false` plus current validity. Historical
`as_of` retrieval may query both active and superseded points with tenant/user/agent/category
and validity filters, then apply the existing semantic/importance/recency ranking. Hard delete,
proxy-user erasure, consent/privacy deletion and retention purge must continue physically
deleting points; they are explicitly outside retention.

This is one isolated change: supersession becomes a retained Qdrant lifecycle state. It does
not change extraction, conflict decisions, claim winners, validity interpretation, ranking
weights, semantic cutoff or current retrieval behavior.

## Acceptance criteria for that experiment

- Historical Precision@K improves from 39.58% to at least 75%.
- Historical Recall@K remains 100%.
- Historical MRR reaches at least 0.80.
- Historical nDCG reaches at least 0.85.
- Historical filler decreases from 14 by at least 60%.
- Current-state leakage into historical queries remains 0.
- Historical-state leakage into current queries remains 0.
- Provenance preservation remains 100%.
- Hard delete and proxy-user erasure physically remove active and superseded vectors: 100%.
- Supersession retry/idempotency and outbox recovery remain correct.
- Measure active-point count, retained-history count, projected index growth and live p50/p95
  latency separately.

If any privacy-erasure or current-leakage invariant fails, revert the experiment regardless of
ranking gains.
