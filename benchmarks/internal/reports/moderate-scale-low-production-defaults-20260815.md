# LOW Scale Run With Approved Redis Defaults - 2026-08-15

Status: **not accepted as the LOW baseline**.

The production-equivalent Redis defaults were 500 ms connection timeout, 500 ms command timeout,
and 750 ms circuit execution deadline. TCP preflight, retry, fallback, pool, cache, authentication,
and product semantics were unchanged. The disposable stack used the deterministic provider, made
no paid provider calls, and did not access holdout.

## Workload result

| Metric | Result |
|---|---:|
| Completed iterations | 1,026 |
| Dropped arrivals | 175 |
| API error rate | 0.390% |
| HTTP request failure rate | 0.257% |
| Add p50 / p95 / p99 | 2,992.5 / 5,987.2 / 6,866.6 ms |
| Retrieval p50 / p95 / p99 | 2,695.5 / 5,589.7 / 6,728.5 ms |
| Job completion p50 / p95 / p99 | 5,276.5 / 9,293.3 / 11,373.9 ms |
| Unfinished jobs after drain | 0 |

Redis recorded zero circuit-deadline failures, zero HTTP 500 responses, 15 failed TCP preflights,
one 515 ms pool-acquisition timeout, and 13,543 circuit-open fallbacks. The API error rate met the
0.5% acceptance ceiling, but Redis pool-timeout acceptance was not completely clean.

All 464 extraction jobs completed with zero retries. Queue wait p50/p95/p99 was
56.25/1,018.72/1,906.46 ms and maximum queue wait was 2,594.56 ms. Database-recorded job completion
p50/p95/p99 was 1,115.85/4,202.79/5,628.03 ms. Queue depth after the drain was zero.

PostgreSQL did not satisfy acceptance: logs contained 406 `too many clients` events during the
sustained window. The post-drain connection snapshot was 46 of 100 configured connections. Enum
and call-quality database telemetry errors remained zero. The database grew to 36,977,687 bytes.

The transactional outbox converged completely: all 505 events were `done`, with zero pending age.
The run-scoped audit passed every invariant: single winner, claim/winner alignment, event
idempotency, provenance, version chains, and outbox convergence. It found zero violations.

## Gates and decision

- Pre-load FAST: 8/8 passed.
- Pre-load INTEGRATION: 0/5 passed. The first relative-path invocation also exposed an orchestrator
  artifact-path harness bug; the corrected run reported existing product failures.
- Post-load FAST: 8/8 passed.
- Post-load INTEGRATION: 4/5 passed. Integration reliability, governance integrity, lifecycle
  activation, and temporal memory passed; fault-injection reliability remained failed with four
  product failures and 87.5% success.

The Redis defaults are retained because the Redis deadline boundary improved and no regression was
attributed to the three approved values. However, this run is not an accepted LOW baseline because
PostgreSQL connection exhaustion, dropped arrivals, frozen add/retrieval latency thresholds, one
Redis pool timeout, and the post-load fault-injection gate failed acceptance. MODERATE traffic must
not start.

Cleanup passed: 20 run-scoped proxy users, 431 Qdrant points, and all run-scoped memories, claims,
revisions, jobs, source events, and outbox rows were removed. All disposable containers, volumes,
and the Compose network were destroyed.
