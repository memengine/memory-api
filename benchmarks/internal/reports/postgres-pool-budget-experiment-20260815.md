# PostgreSQL Pool-Budget Experiment — 2026-08-15

Status: **failed; no production default changed**.

Benchmark-only Compose overrides were added so candidate pool budgets can be exercised without changing normal application defaults. Focused safety tests passed 11/11. Holdout was not accessed and provider cost was zero.

## Controlled diagnostics

| Pool + overflow | Iterations / dropped | API errors | PG peak / final | Add p95 | Retrieval p95 | Job p95 | Correctness |
|---|---:|---:|---:|---:|---:|---:|---:|
| 8 + 4 | 347 / 13 | 0% | not captured / 44 | 4.393 s | 3.841 s | 6.487 s | pass |
| 6 + 3 | 350 / 11 | 0.286% | 49 / 29 | 5.055 s | 3.351 s | 6.433 s | pass |
| 4 + 2 | 332 / 29 | 0% | 43 / 34 | 5.625 s | 4.659 s | 7.398 s | pass |

The 6+3 candidate was selected for the full frozen LOW validation because it met the short-run connection criteria while providing the best throughput/latency balance. The tighter 4+2 allocation increased dropped work and ended above the final-session criterion.

## Full 10-minute LOW: 6 + 3

- 1,076 completed iterations; 125 dropped arrivals
- API error rate 0.093%; HTTP request failure rate 0.059%
- PostgreSQL sessions: peak 60, final observer sample 41
- PostgreSQL exhaustion/pool timeout: 0/0
- Redis timeout/HTTP 500: 0/0
- Add p50/p95/p99: 2.661/4.950/5.925 s
- Retrieval p50/p95/p99: 1.947/5.383/10.500 s
- Job p50/p95/p99: 4.425/7.451/8.823 s
- Jobs: 497 completed, 0 unfinished
- Outbox: 533 done, 0 pending
- All six durability/correctness invariants passed
- FAST gate: 8/8 passed

The consolidated integration gate first encountered relative-output-path harness drift. Its absolute-path retry produced three suite artifacts but exceeded the 10-minute orchestration timeout before an aggregate result, so the post-load integration gate is inconclusive rather than green.

## Decision

Do not promote 6+3 and do not change production PostgreSQL pool defaults. The candidate failed the predefined peak <=50 and post-drain <=30 limits, and frozen add/retrieval latency gates still failed. Reducing each SQLAlchemy pool independently does not enforce a true process-wide budget because API, region, Celery, sync/direct, and auxiliary pool owners remain separate.

The next investigation should attribute and bound the remaining Celery/direct sync sessions before proposing another production repair. It should not continue blind pool-size tuning.

