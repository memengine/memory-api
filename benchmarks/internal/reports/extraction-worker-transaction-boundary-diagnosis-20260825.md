# Extraction-worker transaction-boundary diagnosis — 2026-08-25

## Decision

The captured MODERATE run does **not** prove that row-lock contention on `proxy_users` is the cause of the worker backlog. The telemetry label identifies the last statement executed before commit, not the latency of that individual `UPDATE`.

The confirmed issue is broader: the extraction worker opens a PostgreSQL transaction while loading extraction context, keeps it open through extraction and conflict processing, and commits only after memory/claim/version/provenance/outbox work plus the proxy-user statistics refresh. This unnecessarily couples read/extraction time and the atomic persistence unit.

No production code was changed during this diagnosis.

## Evidence used

- Frozen MODERATE candidate artifact: `artifacts/internal-benchmarks/scale/auth-singleflight-ownership-20260824/postgres-transaction-analysis.json`
- Raw service events: `artifacts/internal-benchmarks/scale/auth-singleflight-ownership-20260824/service-logs.raw.log`
- PostgreSQL observer: `artifacts/internal-benchmarks/scale/auth-singleflight-ownership-20260824/postgres-observer.json`
- Production path: `api/tasks/extraction_tasks.py`

Holdout was not accessed and no provider calls were made.

## Exact production boundary

`run_extraction_pipeline` first commits creation of the source conversation. Its next SQL operation is `_load_existing_memories_for_context`, which starts a new implicit transaction. That same transaction remains open across:

1. context, tenant schema, and source-event reads;
2. extraction;
3. pending-candidate persistence;
4. importance scoring;
5. conflict detection and resolution;
6. memory, claim, revision, provenance, version, and outbox persistence;
7. `_refresh_proxy_user_memory_count`, including a full per-user memory count;
8. the final commit.

The final ORM flush updates `proxy_users.last_active_at` and `proxy_users.memory_count`, so transaction telemetry reports that update as the last statement shape.

## Measured behavior

- 862 worker transactions ended with the proxy-user statistics update.
- Transaction duration: p50 3.069s, p95 21.621s, p99 34.142s, maximum 39.229s.
- 493 were at least 2s; 336 were at least 5s.
- Four worker processes accumulated repeated long transactions while 196 jobs remained queued and four processing after two drain intervals.
- Observer collection was healthy: 723 samples, zero failures, peak 78 PostgreSQL connections.
- Wait observations contained 6,759 `ClientRead`, one `WALWrite`, and **zero recorded PostgreSQL `Lock` waits**.
- The observer also saw long active cross-user-conflict queries and long idle-in-transaction memory reads inside the same worker unit.

## Diagnosis

### Confirmed

- The primary worker transaction begins too early, before extraction has finished.
- The proxy statistics refresh is inside the atomic memory persistence transaction.
- A failure or delay in the denormalized statistics refresh can delay or roll back otherwise valid memory/claim/outbox persistence.
- The statistics value is operational metadata used by tenant/stat endpoints and scorer context; retrieval independently counts memories when it needs a correctness-sensitive count.

### Not confirmed

- No captured evidence shows workers blocked on a `proxy_users` row lock.
- The 3–39s durations cannot be attributed to the final proxy update alone.
- Coalescing or weakening `memory_count` consistency is therefore not justified yet.

The `proxy_users` update is a visible transaction endpoint and possible contention amplifier, but the primary confirmed defect is transaction ownership and duration.

## One isolated repair proposal

Introduce a **two-phase worker transaction boundary**:

1. Load the immutable extraction inputs/context, copy the required values, and explicitly close the read transaction before extraction/provider work.
2. Start one fresh atomic write transaction immediately before pending-memory/conflict/persistence work. Keep memory, claims, revisions, provenance, versions, and outbox in this transaction.
3. After that atomic commit succeeds, refresh `proxy_users.memory_count` and `last_active_at` in a separate short best-effort statistics transaction.

This changes transaction ownership only. It must not change extraction, conflict decisions, authority, claim/version semantics, idempotency, provenance, outbox content, ranking, or lifecycle behavior. It also does not introduce count coalescing.

## Acceptance criteria for an experiment

- Frozen FAST and integration correctness gates remain green.
- Memory/claim/version/provenance/outbox writes remain atomic.
- Extraction output and conflict/winner decisions are unchanged.
- Proxy-user count is correct after successful refresh; a refresh failure cannot roll back the durable logical memory write.
- Duplicate/redelivered event invariants remain 100%.
- Worker transaction p95 improves materially from 21.621s and p99 from 34.142s.
- Worker `idle in transaction` time across extraction is eliminated.
- No new lock waits, connection exhaustion, unfinished-job regression, outbox divergence, or leakage.
- The frozen MODERATE workload is rerun only after focused transaction-failure and retry tests pass.

Implementation should wait for explicit approval.
