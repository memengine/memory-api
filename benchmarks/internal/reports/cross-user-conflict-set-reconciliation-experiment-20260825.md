# Cross-user conflict set-reconciliation experiment — 2026-08-25

## Decision

Retain the isolated set-based cross-user conflict persistence repair.

The repair passed its semantics, database-integrity, transaction-tail, and correctness acceptance criteria. It materially improved MODERATE throughput and worker transaction latency. The overall frozen MODERATE capacity baseline still failed its absolute API latency/error thresholds, so MODERATE is not accepted and no higher load stage should start.

Holdout was not accessed. The deterministic benchmark provider was used and provider cost was zero.

## Implemented scope

- Canonicalize unordered memory pairs for already-detected cross-user conflicts.
- Deduplicate candidates in memory.
- Fetch existing conflicts for the new memory and involved entity types with one reconciliation query.
- Add all missing conflict rows and flush once before running the unchanged automatic resolver.
- Add tenant/entity/memory indexes for both pair orientations.
- Add a PostgreSQL partial unique expression index over the unordered memory pair. The migration
  preserves pre-existing audit rows and enables the invariant for every newly inserted row, so
  legacy duplicates cannot make deployment destructive or block the migration.

Unchanged:

- conflict candidate generation;
- semantic/entity conflict rules;
- resolution actions and winner selection;
- authority, timestamp, extraction, archive, claim, version, provenance, retrieval, and lifecycle behavior.

## Correctness gates

- Focused resolver/conflict tests: 28 passed.
- Frozen conflict and coordination contract tests: 12 passed.
- Database unordered-pair uniqueness regression: passed.
- Fresh full migration-chain validation after the load run: passed.
- Pre-load FAST: 8/8 suites passed.
- Pre-load integration: 5/5 suites passed.
- Post-load FAST: 8/8 suites passed.
- Post-load integration: 5/5 suites passed.
- Product failures: 0.
- Harness errors in accepted runs: 0.

The first pre-load integration invocation encountered the known relative-output-path orchestrator error. The unchanged rerun using an absolute artifact path passed 5/5; this was harness drift, not a product failure.

## Frozen MODERATE result

Workload remained unchanged: 8 arrivals/second for 20 minutes, 20 preallocated VUs, 40 maximum VUs.

| Metric | Previous reference | Set reconciliation | Change |
|---|---:|---:|---:|
| Completed iterations | 1,588 | 2,078 | +30.9% |
| Dropped arrivals | 8,004 | 7,522 | -6.0% |
| Interrupted iterations | 9 | 0 | improved |
| API error rate | 48.22% | 5.44% | -42.78 pp |
| HTTP request failure rate | 49.39% | 14.23% | -35.16 pp |
| Add p50/p95/p99 | 26.656/30.002/30.005 s | 17.968/26.207/30.004 s | improved, still failed |
| Retrieval p50/p95/p99 | 29.143/30.002/30.006 s | 17.967/27.486/30.004 s | improved, still failed |
| Job p50/p95/p99 | 36.044/53.462/55.994 s | 27.424/42.464/52.726 s | improved, still failed |
| k6 correctness-probe failures | 0 | 0 | unchanged |

The frozen API error, HTTP failure, add, retrieval, job-completion, and dropped-arrival thresholds still failed. This repair is therefore retained as a validated bottleneck repair, not as an accepted MODERATE baseline.

## Worker transaction result

The production worker transaction ending at the proxy-user statistics update changed from:

- reference count: 862;
- p50/p95/p99: 3.069/21.621/34.142 s;
- transactions at least 5 s: 336.

To:

- observed count: 716 comparable completed durable-write transactions;
- p50/p95/p99: 1.137/5.751/8.281 s;
- maximum: 10.094 s;
- transactions at least 5 s: 54.

Worker transaction p95 improved by 73.4% and p99 by 75.7%, exceeding the required 25% p95 improvement and avoiding p99 regression.

Sanitized SQL telemetry recorded only 11 conflict reconciliation `SELECT` events while processing 985 conflict inserts and 996 conflict updates. Unit regression coverage proves one reconciliation query and one flush for multiple candidates. Individual cross-user-conflict SQL remained fast: p50 3.463 ms, p95 7.567 ms, p99 33.321 ms, maximum 266.262 ms, zero SQL errors.

## Drain and durable correctness

At traffic end:

- 777/778 jobs completed;
- one queued job had no Celery task ID;
- 802/802 outbox rows done;
- retries: 0.

The normal 120-second watchdog recovered the stranded API-dispatch job. Final state:

- 778/778 jobs completed;
- 717 memories, 206 claims, and 717 revisions;
- 803/803 outbox rows done;
- queue wait p50/p95/p99: 2.799/6.953/11.172 s;
- durable completion p50/p95/p99: 4.263/10.750/15.276 s;
- retries: 0;
- duplicate unordered conflict pairs: 0.

Final invariant audit passed:

- single winner;
- winner alignment;
- source-event idempotency;
- provenance preservation;
- version-chain integrity;
- outbox convergence.

## Cleanup

Run-scoped cleanup removed 80 proxy users, 717 Qdrant points, and 16 Redis keys. Remaining run-scoped proxies, memories, claims, revisions, jobs, events, and outbox rows were all zero. The disposable PostgreSQL, Redis, Qdrant, API, and Celery containers/network were then destroyed. Shared development resources were not used.

## Remaining boundary

The conflict N+1/flush amplification is fixed, but the frozen MODERATE profile still saturates all 40 VUs and produces broad add, retrieval, and job-status timeouts. The next step should be diagnosis only of the remaining shared API request-occupancy tail using this retained repair as the new code baseline. Do not tune conflict semantics or start HIGHER traffic.
