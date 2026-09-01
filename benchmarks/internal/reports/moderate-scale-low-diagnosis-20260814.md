# Moderate Scale LOW Reliability Diagnosis - 2026-08-14

Status: diagnosis complete; no production repair implemented.

Two isolated three-minute windows used the frozen mixed workload and deterministic provider. The
first ran at 1 operation/s and the second at 2 operations/s. Holdout and paid providers remained
inaccessible.

## Comparative results

| Metric | 1 op/s | 2 op/s |
|---|---:|---:|
| Completed iterations | 180/180 | 329/360 |
| Dropped iterations | 1 | 31 |
| API error rate | 0% | 0.91% |
| Add p95 / p99 | 2,529 / 3,327 ms | 4,883 / 5,916 ms |
| Retrieval p95 / p99 | 665 / 3,899 ms | 4,004 / 4,603 ms |
| Job completion p95 / p99 | 2,857 / 3,580 ms | 6,758 / 8,244 ms |
| DB completion p95 / p99 | 965 / 1,184 ms | 1,619 / 1,871 ms |
| Queue wait p95 / p99 | 48 / 59 ms | 875 / 1,360 ms |
| Jobs unfinished after drain | 0/71 | 2/168 |
| HTTP 500 | 0 | 3 |
| Correctness invariants | pass | pass |

## Localized failure boundaries

1. **PostgreSQL connection lifecycle - highest reliability risk.** Extraction job helpers create a
   new SQLAlchemy engine/session factory for processing, pipeline, and completion transitions. The
   engines are not shared or disposed. At 2 op/s PostgreSQL reported `too many clients` 12 times,
   and two jobs remained unfinished after the 60-second drain.
2. **Redis circuit/client timeout - API availability risk.** Redis recorded no rejected or failed
   commands and server execution remained in microseconds, while the application emitted 760
   circuit-open fallbacks at 1 op/s and 5,694 at 2 op/s. The 200 ms circuit execution deadline and
   100 ms connection deadline cancelled client work; three retrieval requests returned HTTP 500.
3. **Call-quality enum persistence - correctness/overhead bug.** Every extraction job attempted to
   persist SQLAlchemy enum member name `none`, while PostgreSQL accepts `NONE`. This produced 71
   database errors at 1 op/s and 168 at 2 op/s. The gate catches the error, so durable memory
   correctness survived, but call-quality telemetry is lost and rollback/log overhead is added.

All claim-winner, idempotency, provenance, version-chain, and outbox convergence checks passed in
both windows. These are product failures, not Redis/PostgreSQL server saturation or benchmark
annotation failures.

## One proposed repair

Repair only extraction-worker PostgreSQL session-factory ownership: create one process-scoped sync
engine/session factory per Celery worker process and reuse it for processing, pipeline, completion,
failure, and dead-letter transitions. Dispose it during worker shutdown. Do not change pool sizes,
Redis timeouts, enum mapping, extraction, claims, conflicts, or workload thresholds in the same
experiment.

Acceptance for the identical 2 op/s diagnostic and frozen 10-minute LOW run:

- zero PostgreSQL `too many clients` errors;
- zero unfinished jobs after the 60-second drain;
- zero unexpected HTTP 500 responses caused by database exhaustion;
- no dropped iterations in the three-minute diagnostic and materially fewer than the failed LOW;
- all correctness invariants and FAST gates remain green;
- database connection count reaches a stable plateau rather than growing with job count.

The Redis circuit and enum mismatch remain separately confirmed repairs after this experiment.

## Session-factory repair result

The isolated process-scoped session-factory repair was implemented and evaluated with the
identical three-minute 2 op/s diagnostic. Direct database reliability passed:

- PostgreSQL `too many clients`: 12 -> 0;
- unfinished jobs after 60-second drain: 2 -> 0 (142/142 completed);
- HTTP 500: 3 -> 0;
- API error rate: 0.91% -> 0%;
- queue-wait p95: 875 -> 556 ms;
- DB completion p95: 1,619 -> 1,482 ms;
- all correctness invariants remained green.

The database connection snapshot moved from 5 before traffic to 53 after traffic. This is bounded
by process-local pools, but a longer run was not performed because the full diagnostic gate did not
pass: 28/360 arrivals were dropped, add/retrieval latency thresholds still failed, and the
call-quality enum mismatch still generated one rejected insert per extraction job (142 errors).
Accordingly, the frozen 10-minute LOW rerun was not started. The repair is retained because its
isolated failure boundary was removed without correctness regression; it is not yet a passing LOW
baseline.

## Call-quality enum repair result

The ORM mapping now persists `CallQualityBlockedLayer` values (`L1`-`L4`, `NONE`) instead of Python
member names. The identical three-minute 2 op/s diagnostic confirmed:

- rejected call-quality enum inserts: 142 -> 0;
- PostgreSQL `too many clients`: remained 0;
- completed jobs: 150/150, with zero retries;
- DB completion p95 / p99: 1,203 / 1,530 ms;
- queue-wait p95 / p99: 530 / 719 ms;
- API-level workload errors: 0%;
- all correctness invariants remained green.

The broader load gate still failed: 346/360 iterations completed, 14 arrivals were dropped, add
p95 was 4,023 ms, retrieval p95 was 3,461 ms, and logs contained 5,199 Redis circuit-open
fallbacks plus two HTTP 500 responses on auxiliary requests. The 10-minute LOW rerun therefore
remains blocked. Redis client/circuit timeout behavior is now the next isolated boundary.

## Redis TCP-preflight removal experiment

Removing the TCP preflight failed and was reverted. With actual Redis commands still limited by
the existing 200 ms circuit deadline and 100 ms connection timeout, the identical 2 op/s diagnostic
regressed to 293/360 completed iterations, 68 dropped arrivals, 19.45% API errors, 60 HTTP 500
responses, and one queued job after drain. Correctness invariants still passed, and PostgreSQL
connection and enum failures remained zero.

The preflight is currently shielding requests from the more serious execution-timeout behavior.
The next experiment must leave the probe intact and address how Redis command deadlines are
configured and converted to fallbacks. No second Redis change was made in this slice.
