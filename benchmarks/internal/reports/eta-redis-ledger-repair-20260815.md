# Request-Path ETA Redis-Ledger Repair — 2026-08-15

Status: **repair retained; LOW baseline not accepted**.

## Change

`get_queue_depth()` now reads `ZCARD` from the existing Redis reservation ledger maintained by extraction-slot reserve/release operations. Redis failure returns depth zero/no ETA. Live Celery `active()` and `reserved()` inspection remains available through the separate `inspect_queue_depth()` operational helper and is no longer called by `/v1/memories/add`.

Worker concurrency, prefetch, queues, reservation semantics, thresholds, ETA calculation, extraction, Redis retry behavior, and business logic are unchanged.

## Result

Compared with the prior quality-factory LOW:

- completed iterations: 1,068 -> 1,141;
- dropped arrivals: 133 -> 60;
- API errors: 0.187% -> 0%;
- add p95: 5.470 s -> 3.922 s;
- retrieval p95: 4.967 s -> 3.917 s;
- PostgreSQL peak: 43 -> 44, still below 50;
- processing ETA p95: 2.068 s diagnostic baseline -> 421 ms under full LOW.

The strict ETA p95 <=50 ms target was not met under full load, but live Celery broadcasts were completely removed from the request path and the system improved materially without correctness regression.

## Correctness and gates

- Jobs: 486 completed, 0 unfinished
- Outbox: 540 done, 0 pending
- All six durability invariants passed
- Focused tests: 25/25 passed
- FAST before/after: 8/8 and 8/8 passed
- Integration reliability: passed
- Provider cost: $0; holdout untouched

## Decision

Retain the isolated repair. Restoring request-time Celery control broadcasts would reintroduce a proven two-second tail and reduce throughput.

Do not accept LOW yet: absolute add/retrieval thresholds and the post-drain PostgreSQL session target remain failed. The next investigation should profile middleware/dependency/database contention shared by add and retrieval; ETA tuning should stop.

