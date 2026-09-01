# Clarification transaction and lock diagnosis — 2026-08-23

Status: diagnosis complete; clarification-lock hypothesis rejected. Production behavior unchanged.

## Validity

- Dedicated disposable `memoryos-scale` stack only.
- Frozen MODERATE profile: 8 arrivals/second for 20 minutes, 20 preallocated and 40 maximum VUs.
- Production-equivalent application image rebuilt from the current tree.
- Deterministic provider; zero paid-provider cost.
- Holdout excluded.
- PostgreSQL blocker attribution sampled every 500 ms through traffic and drain.

## Workload result

| Metric | Result |
|---|---:|
| Completed / dropped iterations | 1,460 / 8,138 |
| API error rate | 62.72% |
| HTTP failure rate | 69.98% |
| Add p50 / p95 / p99 | 30.001 / 30.011 / 30.051 s |
| Retrieval p50 / p95 / p99 | 30.002 / 30.013 / 30.078 s |
| Job completion p50 / p95 / p99 | 43.274 / 57.681 / 58.814 s |
| DB queue wait p50 / p95 / p99 | 4.966 / 50.361 / 70.091 s |
| DB completion p50 / p95 / p99 | 7.864 / 61.019 / 85.249 s |

MODERATE performance and drain acceptance failed.

## Clarification boundary

Clarification work was slow but was not a PostgreSQL lock owner or waiter:

- 1,324 observer samples whose last query referenced `clarification_queue`.
- Blocked clarification observations: **0**.
- Wait state: 1,274 `ClientRead`, 50 active/no wait.
- Clarification phase p50/p95/p99: 0.663 / 3.750 / 7.006 s.
- Clarification SQL p50/p95/p99: 0.325 / 2.025 / 4.279 s; zero SQL errors.
- Transactions ending at a clarification statement had p50/p95/p99 9.673 / 41.624 / 51.874 s.

The high transaction age does not represent clarification row-lock contention. The retrieval request opens its transaction earlier; clarification is often the final database statement before commit. Under general backlog, the final statement inherits the age of the broader request transaction.

## Confirmed blocking boundary: API-key usage timestamp

PostgreSQL recorded 2,591 blocked-transaction observations. Of these:

- 2,365 waited on a transaction-ID lock while running `UPDATE api_keys SET last_used_at=...`.
- 223 waited on a tuple lock for the same update.
- All blocked rows belonged to the API authentication engine.
- 49 backend PIDs were blocked by 41 distinct blocker PIDs.
- Maximum observed blocked transaction age: 29.059 s.

Authentication telemetry explains why a normally small metadata write became a hot-row serialization point:

- Cache lookups: 2,167 misses, 60 timeouts, only 7 hits.
- Redis circuit-open fallbacks: 2,227; Redis command errors: 92.
- Database/bcrypt fallbacks: 2,225.
- Bcrypt p50/p95/p99: 0.375 / 0.572 / 0.677 s.
- API-key update transaction p50/p95/p99: 1.579 / 8.249 / 16.488 s; maximum 36.307 s.
- Complete database fallback p50/p95/p99: 2.883 / 11.266 / 19.860 s.

Every cache fallback authenticating the same benchmark key synchronously updates the same `api_keys` row. PostgreSQL correctly serializes those writes, creating the confirmed lock queue and contributing directly to pool growth.

## Correctness and drain

- Single winner, winner alignment, event idempotency, provenance, version chains, and outbox convergence passed.
- Outbox converged: 656 done, zero pending.
- Jobs: 619 completed, 2 processing, 6 queued after the extended drain.
- Celery reported no active or reserved tasks while those eight rows remained unfinished; this is a separate stranded-job reliability failure, not a clarification lock.
- PostgreSQL peaked at 99 connections and rejected 33 observer connections with `too many clients`.
- Final PostgreSQL state remained 67 idle plus two `celery-background` idle-in-transaction `BEGIN` backends, aged approximately 426 and 1,026 seconds. This is separate background-session evidence and was not attributed to clarification.

## Decision

Do not change clarification selection, claiming, status transitions, or retrieval transaction behavior based on this run. The proposed clarification atomic-claim repair is not supported by blocker evidence.

## One isolated proposed experiment

Remove only synchronous per-request contention on `ApiKey.last_used_at` while preserving authentication decisions and a bounded-accuracy usage timestamp. The experiment should coalesce usage touches for the same API key into at most one durable update per short interval, using the existing cache/worker infrastructure rather than creating a second authentication cache.

Keep API-key lookup, bcrypt verification, permissions, Redis failure fallback, rate limiting, and request authentication semantics unchanged.

Acceptance on the same frozen MODERATE profile:

- zero PostgreSQL lock waits attributable to `api_keys.last_used_at`;
- `last_used_at` remains durably refreshed within the documented coalescing interval;
- zero PostgreSQL connection rejections and peak connections below 80;
- materially lower database-fallback and API latency without additional authentication failures;
- no unfinished jobs after drain;
- all correctness/security invariants and post-run regression gates remain green.

Redis circuit behavior and the two background `BEGIN` sessions must remain separately diagnosed; they should not be changed in the same experiment.
