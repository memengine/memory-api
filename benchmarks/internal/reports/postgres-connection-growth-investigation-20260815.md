# PostgreSQL Connection Growth Investigation - 2026-08-15

Status: diagnosis complete; no production repair applied.

The valid diagnostic used the unchanged frozen ten-minute LOW workload, Candidate C Redis defaults,
the deterministic provider, and a fresh disposable stack. PostgreSQL activity was sampled every two
seconds while benchmark-only SQLAlchemy instrumentation attributed engine and physical-connection
creation to call sites. Holdout was inaccessible and provider cost was zero.

Two earlier attempts are excluded: the first observer used an invalid psycopg2 DSN; the second used
agent IDs from a previous disposable stack and produced no valid memories. Both were cleaned.

## Connection growth

| Minute | PostgreSQL sessions |
|---:|---:|
| 0 | 2 |
| 1 | 37 |
| 2 | 39 |
| 3 | 36 |
| 4 | 53 |
| 5 | 48 |
| 6 | 33 |
| 7 | 42 |
| 8 | 48 |
| 9 | 52 |
| 10 | 64 |

Across 309 successful samples, sessions peaked at 73 of PostgreSQL's 100-connection limit. Peak
idle sessions were 69, peak active sessions were 8, and peak `idle in transaction` sessions were
11. The observer had zero failures. This is pool/engine retention, not sustained query concurrency.

The prior uninstrumented LOW run produced 406 `too many clients` errors. This repeat did not reach
the hard limit, but reached 73% capacity and reproduced cumulative, nondeterministic growth. The
variation is consistent with short-lived engines being reclaimed at nondeterministic times.

## Ownership attribution

| Owner | Engines created | Physical connections opened | Explicit closes observed |
|---|---:|---:|---:|
| Vector outbox cycle | 126 | 138 | 0 |
| Global async engines/import path | 4 | 16 | not observed |
| Region async pools | 3 | 14 | 0 |
| Watchdog cycle | 5 | 5 | 0 |
| Extraction process pools | 4 | 4 | 0 during active workers |
| Queue-router fallback | 1 | 0 | 0 |
| Webhook service construction | 2,700 | 0 | 0 |

`process-vector-sync-outbox` runs every five seconds. Each cycle calls
`build_vector_sync_session_factory()`, which creates a new SQLAlchemy engine and QueuePool. The
session is closed, but the owning engine is neither reused nor disposed. This is the dominant source
of physical connection churn and the confirmed exhaustion boundary.

`WebhookEventService` is constructed through request-scoped quota management and eagerly creates a
sync session factory even when the webhook path never uses it. It created 2,700 engines but opened
no physical connections in this run. That is a separate allocation/latency inefficiency, not the
primary connection-exhaustion repair.

## Workload and correctness context

The run completed 915 iterations and dropped 286 arrivals. API error rate was 4.153%. Add p50/p95/
p99 was 2,857/30,000/30,001 ms; retrieval was 1,765/5,788/30,001 ms; job completion was
4,394/8,277/10,142 ms. All 404 persisted jobs eventually completed, all 439 outbox rows converged,
and the frozen correctness audit passed with zero winner, idempotency, provenance, version-chain, or
outbox violations. The long maximum database-recorded job completion was 104.6 seconds, showing a
temporary severe stall even though the drain eventually recovered.

## One proposed isolated repair

Make only the vector-outbox sync session factory process-scoped per Celery worker process, matching
the already-retained extraction-worker ownership pattern, and dispose its engine on worker-process
shutdown. Do not alter pool sizes, PostgreSQL limits, beat cadence, batch sizes, retries, outbox
semantics, worker concurrency, or any business logic.

Acceptance for the unchanged LOW rerun:

- vector-outbox engine creation is one per worker process, not one per five-second cycle;
- connection count reaches a stable post-warmup plateau, with peak at most 50/100;
- zero PostgreSQL connection-exhaustion errors and zero pool checkout timeouts;
- zero unfinished jobs after drain and complete outbox convergence;
- all correctness invariants remain green;
- no regression in API errors or add/retrieval/job latency relative to the valid diagnostic;
- focused unit, FAST, and relevant integration gates remain green.

The eager webhook session-factory allocation should remain a separately approved later repair.

## Approved vector-outbox repair result

The process-scoped vector-outbox session factory was implemented and retained. Under the unchanged
ten-minute LOW workload, vector-outbox engine creation fell from 126 to 2 and physical connections
opened fell from 138 to 2. PostgreSQL connection exhaustion and pool-checkout timeouts were both
zero. The relevant integration-reliability suite passed, focused tests passed 12/12, and the FAST
product assertions passed after rerunning the extraction contract under a workspace basetemp to
avoid the known Windows temporary-directory ACL harness error.

The repair materially improved load behavior versus the valid diagnostic: iterations increased
from 915 to 1,080, dropped arrivals fell from 286 to 121, API errors fell from 4.153% to 0.093%, and
all 498 jobs completed with zero retries. All 551 outbox rows converged and correctness remained
100%.

The full LOW baseline is still not accepted. PostgreSQL sessions peaked at 60/100 rather than the
predefined <=50 target, even though the minute samples fluctuated within a non-monotonic 32-55
post-warmup band and ended at 42. Frozen add and retrieval latency thresholds also remained failed.
No second connection/pool change was made.
