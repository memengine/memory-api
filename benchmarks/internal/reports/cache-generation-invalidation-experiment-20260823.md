# Cache generation invalidation experiment

Date: 2026-08-23  
Run: `cache-generation-v2-low-20260823`  
Decision: **request-path design validated, but LOW acceptance failed; keep benchmark-only**

## Isolated candidate

In the explicitly guarded disposable benchmark environment only, dynamic cache keys used a
versioned `v2` namespace and an atomic per-identity generation. Routine invalidation incremented
the generation rather than scanning Redis. Hot-tier values used one generation-scoped Redis hash,
so hot-tier reads also avoided wildcard scans.

Normal production/development behavior remains the legacy key path. The disposable Compose stack
defaults were restored to `legacy-scan`/`v1` after the experiment; the candidate now requires
explicit benchmark overrides. Privacy purge, deployment activation, Redis retry/circuit behavior,
authentication, extraction, retrieval ranking, claims, and Qdrant semantics were not changed.

## Gates

- Focused and broader cache/retrieval/memory tests: 50/50 passed.
- Pre-load FAST: 8/8 passed.
- Four live integration suites passed on the first run.
- The fault-injection suite initially inherited the deterministic-provider fixture, causing four
  mocked provider tests to bypass their mocks. Re-running the unchanged suite without that
  fixture passed 32/32. This was configuration/harness drift, not a product failure.
- Post-load FAST: 8/8 passed.
- Holdout was inaccessible; provider calls and cost were zero.

## Frozen LOW result

| Metric | Previous production-default LOW | Generation candidate | Frozen acceptance |
|---|---:|---:|---:|
| Completed iterations | 1,026 | 1,155 | informational |
| Dropped iterations | 175 | 46 | improvement |
| API error rate | 0.390% | 0.173% | <=0.50%, pass |
| HTTP failure rate | 0.257% | 0.054% | <=0.50%, pass |
| Add p50 / p95 / p99 | 2,992.5 / 5,987.2 / 6,866.6 ms | 68 / 4,155.25 / 7,910.24 ms | p95 <500, p99 <1,000; fail |
| Retrieval p50 / p95 / p99 | 2,695.5 / 5,589.7 / 6,728.5 ms | 57 / 3,199.6 / 5,505.28 ms | p95 <750, p99 <1,500; fail |
| Client job p50 / p95 / p99 | 5,276.5 / 9,293.3 / 11,373.9 ms | 1,463 / 7,419.8 / 9,987.18 ms | p95 <10,000; pass |
| Queue wait p50 / p95 / p99 | 56.25 / 1,018.72 / 1,906.46 ms | 13.38 / 387.8 / 847.82 ms | improved |
| Unfinished jobs | 0 | 0 | zero, pass |
| Circuit-open fallback logs | 13,543 | 3,229 | 76.2% reduction |
| HTTP 500 | 0 | 0 | zero, pass |

The run completed 1,155 of 1,201 scheduled arrivals. All 508 accepted extraction jobs completed,
all 563 outbox events converged, and there were zero correctness-probe failures.

## Redis evidence

- Request-path `SCAN`: **0**.
- Actual Redis command timeouts: **2 GET timeouts**.
- Circuit execution deadline failures: **1**.
- TCP preflight failures: **48**.
- Circuit-open fallbacks: **3,229**.
- Authentication cache: 3,355 hits, 332 misses, 3 timeouts; 335 database/bcrypt fallbacks.
- Redis-related HTTP 500 responses: **0**.

The candidate removed the confirmed scan boundary and materially improved median latency,
throughput, drops, queue wait, errors, and fallback volume. It did not eliminate intermittent
tail stalls. Authentication cache hit rate was about 90.9%, below the design target of 95%, and
the remaining circuit/preflight behavior correlates with the slow tail.

Redis `CLIENT` response errors in the log are connection-label instrumentation compatibility
noise, not command timeouts; they were excluded from the two GET timeout count.

## Correctness and cleanup

The post-drain audit passed single-winner, winner alignment, durable event idempotency, provenance,
version-chain integrity, and outbox convergence with zero violations. No jobs or outbox events
remained unfinished.

Cleanup removed 20 proxy users, 473 Qdrant points, and all run-scoped PostgreSQL state. The
disposable containers, network, Redis state, PostgreSQL storage, and Qdrant storage were destroyed.

## Decision and next step

Do not activate generation invalidation in production and do not start MODERATE. Retain the code
only as an explicitly enabled benchmark candidate because it proves that scan-free invalidation is
correct and materially improves LOW reliability, while keeping the normal scale stack on the
legacy behavior.

The next isolated step should be **diagnosis only** of the remaining LOW tail using the captured
request-phase, authentication-cache, TCP-preflight, circuit, and Redis timing evidence. Determine
whether the residual p95/p99 comes from authentication cache misses/circuit-open fallback,
preflight failures, or another event-loop/connection boundary. Do not change generation logic,
authentication, preflight, or Redis deadlines until that evidence is reviewed.

Artifacts:

- `artifacts/internal-benchmarks/scale/cache-generation-v2-low-20260823/k6-low.json`
- `artifacts/internal-benchmarks/scale/cache-generation-v2-low-20260823/snapshot.json`
- `artifacts/internal-benchmarks/scale/cache-generation-v2-low-20260823/audit.json`
- `artifacts/internal-benchmarks/scale/cache-generation-v2-low-20260823/experiment-summary.json`
- `artifacts/internal-benchmarks/scale/cache-generation-v2-low-20260823/stack.log`
