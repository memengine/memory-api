# Redis per-user cache-key registry experiment

Date: 2026-08-23  
Run: `moderate-redis-cache-index-20260823`  
Decision: **failed frozen acceptance and reverted**

## Candidate

The candidate registered retrieval and hot-tier cache keys in per-user Redis sets, used those sets
for routine reads/invalidation, and retained a lazy one-time legacy-key scan guarded by a 24-hour
migration marker. Redis timeouts, connection pools, circuit behavior, authentication, ranking,
and lifecycle semantics were unchanged.

Focused candidate tests passed 15/15, broader cache/retrieval/memory/auth tests passed 51/51,
FAST passed 8/8, and the corrected absolute-path INTEGRATION run passed 5/5. The disposable stack
used the deterministic provider, holdout was inaccessible, and provider cost was zero.

## Frozen MODERATE comparison

| Metric | Reference | Registry candidate | Acceptance |
|---|---:|---:|---:|
| Completed iterations | 2,066 | 1,776 | capacity target not met |
| Dropped iterations | 7,534 | 7,825 | capacity target not met |
| API error rate | 8.86% | 10.70% | <=0.50% |
| HTTP failure rate | 19.63% | 27.14% | <=0.50% |
| Add p50 / p95 / p99 | 16.371 / 30.001 / 30.003s | 19.822 / 30.002 / 30.008s | failed |
| Retrieval p50 / p95 / p99 | 17.787 / 30.002 / 30.007s | 21.082 / 30.002 / 30.006s | failed |
| Job p50 / p95 / p99 | 24.767 / 46.217 / 51.504s | 31.702 / 51.795 / 56.929s | failed |
| Auth cache hits | 134/3,255 = 4.12% | 156/2,959 = 5.27% | >=95% |
| Database/bcrypt fallbacks | 3,121 | 2,803 | <=1% after warm-up |
| Redis `SCAN` timeouts | 527 | 529 | >=80% reduction |
| Connection timeouts | 446 | 641 | >=80% reduction |
| Mirrored pool-acquisition timeouts | 446 | 641 | >=80% reduction |
| Shared fallback logs | 28,189 | 26,244 | materially reduced |
| HTTP 500 responses | 21 | 13 | zero Redis-related |

After drain, all 650 accepted jobs completed and all 685 outbox rows converged. Single-winner,
winner-alignment, idempotency, provenance, version-chain, and outbox audits passed with zero
violations. PostgreSQL durability was not regressed.

## Failure analysis

The registry did not remove startup/warm-up scans. Concurrent first reads and invalidations saw no
migration marker and independently entered the lazy migration path before any caller committed the
marker. This produced a migration-scan stampede: `SCAN` timeouts remained 529 instead of falling
from 527. Transactional registry writes added Redis commands, while new-connection and enclosing
pool-acquisition timeouts increased to 641. The local unit contract therefore did not generalize
to multi-request startup concurrency.

This result does not disprove indexed invalidation as an architecture. It rejects lazy per-user
migration in the request path and rejects retaining this candidate without a separate migration
design.

## Decision

The registry implementation and its four temporary tests were reverted. Post-revert broader tests
passed 47/47 and FAST passed 8/8 with zero product failures, harness errors, or provider cost.

Do not immediately attempt another request-path marker/coalescing variant. The next proposal should
be design-only: eliminate legacy migration work from live requests, for example through an
explicit deployment migration/cache-namespace rollover with independently governed privacy purge.
That design must prove privacy deletion, rollback, mixed-version deployment behavior, and zero
request-path wildcard scans before another load experiment.

Harness note: the first integration invocation exposed an existing relative-output-path serializer
error. Re-running with an absolute output directory passed all suites; no threshold or product
behavior changed.
