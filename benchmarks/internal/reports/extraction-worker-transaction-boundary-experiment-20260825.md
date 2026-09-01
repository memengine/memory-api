# Extraction-worker transaction-boundary experiment — 2026-08-25

## Decision

The isolated two-phase transaction candidate failed its frozen acceptance criteria and was reverted.

It successfully removed the PostgreSQL transaction from extraction/provider work and isolated proxy-user statistics refresh from the durable memory write. It improved median and p99 worker transaction latency and preserved all durable correctness invariants. However, durable-write p95 regressed, the frozen MODERATE capacity gate still failed decisively, and overall request throughput/reliability did not improve enough to justify retaining the added transaction complexity.

Production behavior is restored to the pre-experiment Redis failure-ownership baseline.

## Candidate scope

- Read extraction context into immutable values and explicitly close the read transaction before extraction.
- Keep memory, claim, revision, version, provenance, conflict, and outbox persistence atomic.
- Refresh `proxy_users.memory_count` and `last_active_at` in a separate post-commit transaction.
- No extraction, conflict, authority, claim, version, provenance, idempotency, retrieval, lifecycle, provider, Redis, or workload changes.

## Correctness gates

- Focused transaction/claim/outbox tests before load: 29 passed.
- FAST before load: 8/8 suites passed, zero failures, zero provider cost.
- INTEGRATION: four suites passed directly; fault injection passed separately after removing deterministic-provider environment contamination from provider-fallback unit tests.
- Final durable audit: all checks passed.
  - single winner: pass
  - winner alignment: pass
  - event idempotency: pass
  - provenance preservation: pass
  - version-chain integrity: pass
  - outbox convergence: pass
- Post-revert focused tests: 28 passed.

The first integration invocation failed because a relative artifact path exposed orchestrator path drift. A second invocation with `psycopg2` produced collection failures because the application async engine requires `asyncpg`. These were harness/configuration failures. The corrected `asyncpg` run had no harness errors. Deterministic-provider contamination affected only four provider-fallback tests and disappeared when that benchmark-only override was removed with all real provider keys still empty.

## Frozen MODERATE result

- Workload: 8 arrivals/s for 20 minutes, 20 preallocated VUs, 40 maximum VUs.
- Completed iterations: 1,588.
- Dropped arrivals: 8,004.
- Interrupted iterations: 9.
- HTTP request failure rate: 49.39%.
- API error rate: 48.22%.
- Add p50/p95/p99: 26.656/30.002/30.005s.
- Retrieval p50/p95/p99: 29.143/30.002/30.006s.
- Job completion p50/p95/p99: 36.044/53.462/55.994s.
- Correctness-probe failures during k6: 0.

The run failed the unchanged API error, HTTP failure, add, retrieval, job-completion, and dropped-arrival thresholds.

## Worker transaction comparison

Reference transaction ending at proxy statistics update:

- count 862
- p50 3.069s
- p95 21.621s
- p99 34.142s
- 336 at least 5s

Candidate durable-write transaction:

- count 703
- p50 1.409s
- p95 23.768s
- p99 29.913s
- maximum 34.522s
- 212 at least 5s

Candidate read-context transaction:

- 71 observed, all explicitly rolled back before extraction
- p50 51.5ms, p95 78.7ms, p99 101.6ms
- zero at least 2s

Candidate proxy-statistics transaction:

- 68 emitted updates
- p50 50.0ms, p95 74.6ms, p99 129.3ms
- zero at least 5s
- zero refresh failures

The candidate reduced median duration by 54%, p99 by 12%, and the proportion of transactions at least 5s from 39.0% to 30.2%. But p95 worsened by 9.9%, violating the experiment's primary acceptance criterion. The remaining long tail is inside the durable conflict/persistence transaction, not the extraction-context or statistics transaction.

## Drain and durable state

Traffic end:

- jobs: 645 completed, 58 queued, 4 processing
- outbox: 687/687 done

Final bounded drain:

- jobs: 707/707 completed, zero unfinished
- queue wait p50/p95/p99: 4.858/127.375/184.337s
- completion p50/p95/p99: 7.798/130.701/190.405s
- outbox: 757/757 done
- retries: 0
- PostgreSQL connection errors: 0
- enum telemetry errors: 0

## Conclusion and next evidence boundary

Do not retry transaction splitting or coalescing proxy statistics next. The experiment localized the remaining capacity problem to the durable conflict/persistence unit and API request occupancy. The next step should be diagnosis-only of long conflict/persistence queries—especially cross-user conflict lookup growth and query plans—before proposing another isolated repair.

Holdout was not accessed. Paid-provider cost was `$0`. The disposable stack is cleaned after post-revert gates.
