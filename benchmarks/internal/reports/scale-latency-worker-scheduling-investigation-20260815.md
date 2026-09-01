# Scale Latency and Worker-Scheduling Investigation — 2026-08-15

Status: diagnosis complete; no production latency behavior changed.

## Existing LOW evidence

- HTTP waiting p95: 4.970 s; connection establishment p95: 0 ms.
- Extraction database queue wait: 34 ms p50, 718 ms p95, 2.007 s p99.
- Extraction completion: 809 ms p50, 3.425 s p95.

Therefore worker queueing is not the primary add-acknowledgement bottleneck. The delay occurs in API request processing before or alongside asynchronous extraction dispatch.

Stable post-load PostgreSQL ownership remains a separate capacity issue: the last LOW observer sample had 35 idle and one idle-in-transaction session. The largest stable API owners were module-level async (9) and region async (10); background/vector/watchdog owners accounted for most of the remainder. This was not mixed into the latency experiment.

## Add-route phase diagnostic

The 1 req/s disposable diagnostic captured 85 successful add-route phase samples.

| Phase | p50 | p95 | p99 |
|---|---:|---:|---:|
| Quality gate | 6.68 ms | 18.39 ms | 40.50 ms |
| Proxy resolution | 0.81 ms | 7.68 ms | 15.46 ms |
| Durable job/claim queueing | 10.06 ms | 22.18 ms | 36.94 ms |
| Processing ETA | 11.17 ms | 2,067.78 ms | 2,315.18 ms |
| Total route | 31.90 ms | 2,108.90 ms | 2,353.69 ms |

Processing ETA accounts for nearly the entire route tail. `get_queue_depth()` reads a 15-second cache, but on a miss performs Celery control broadcasts for both `active()` and `reserved()`. Those live worker inspections are executed for a user-facing estimate on the synchronous add-response path.

The queue router already maintains a durable-enough Redis job ledger (`queue_depth:<queue>:jobs`) when slots are reserved and released. That ledger represents queued/processing extraction work without requiring a request-time Celery broadcast.

## One isolated proposed repair

For request-path processing ETA, read queue depth from the existing Redis queue job ledger (for example `ZCARD`) and fail open to no ETA when Redis is unavailable. Keep live Celery inspection available only for operational reconciliation/diagnostics, not the add response.

Do not change extraction dispatch, worker concurrency, prefetch, queues, reservations, rate limits, Redis retry behavior, processing thresholds, or ETA calculation semantics.

Acceptance:

- processing-ETA p95 <=50 ms and no live Celery inspect call from `/v1/memories/add`;
- add p95 improves materially from 5.470 s in frozen LOW, with no p99 regression attributable to ETA;
- queue-depth/ETA correctness matches the existing reservation ledger in focused tests;
- API errors <=0.5%, zero unfinished jobs, complete outbox convergence;
- PostgreSQL peak remains <=50 with no exhaustion/timeouts;
- all durability invariants, FAST, and integration reliability remain green;
- no holdout/provider cost.

