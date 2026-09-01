# API-key single-flight after failure-ownership repair — 2026-08-24

## Decision

The candidate failed frozen acceptance and was reverted. It materially improved throughput, HTTP reliability, authentication latency, and fallback duplication, but did not restore sufficient cache availability or prevent a large extraction-job backlog.

Production behavior remains the accepted Redis failure-ownership baseline. No single-flight or bcrypt offload code remains active.

## Candidate

- Full API-key fingerprint used only as the in-process flight identity.
- Same-key cache-miss followers awaited one shared fallback task.
- Different API keys remained independent.
- Leader bcrypt verification was offloaded behind a four-operation semaphore.
- Follower cancellation was shielded from the leader.
- Failed flights were removed and retryable.
- Redis timeouts, circuit behavior, cache TTL/payload, key selection, permissions, last-used persistence, quota, extraction, retrieval, and the workload were unchanged.

## Frozen MODERATE comparison

| Metric | Ownership reference | Single-flight candidate | Acceptance |
|---|---:|---:|---:|
| Completed iterations | 1,939 | 2,716 | improvement; capacity still failed |
| Dropped arrivals | 7,658 | 6,878 | improvement; failed |
| Interrupted | 3 | 7 | failed zero |
| API error rate | 16.01% | 10.50% | failed <=0.50% |
| HTTP failure rate | 27.31% | 3.65% | failed <=0.50% |
| HTTP 500 log responses | 90 | 1 | improved; failed zero |
| Add p50 / p95 / p99 | 20.507 / 30.001 / 30.002s | 4.882 / 30.001 / 30.002s | failed |
| Retrieval p50 / p95 / p99 | 18.874 / 30.001 / 30.002s | 4.081 / 30.001 / 30.002s | failed |
| Job p50 / p95 / p99 | 30.538 / 52.411 / 55.296s | 15.306 / 34.299 / 38.246s | failed p95 <10s |

## Local single-flight behavior

- Authenticated requests observed: 14,363.
- Cache hits/misses/timeouts: 8,991 / 4,891 / 481.
- Cache hit rate: 62.60%; failed >=95%.
- Fallback leaders: 389 (2.71%); failed <=1%.
- Followers: 4,983.
- Bcrypt checks: 389, exactly one per leader.
- The candidate avoided approximately 92.76% of fallback work among leader/follower requests.
- Authentication p50/p95/p99 improved to 115/1,038/1,717ms from 1,775/6,895/12,900ms.

The local mechanism worked. It was insufficient because prolonged cache-unavailable/open-circuit intervals continued after each completed flight. Later non-overlapping request groups elected new leaders, so single-flight reduced stampedes without restoring the cache itself.

## Drain and correctness

At the traffic-end snapshot:

- jobs: 851 completed, 271 queued, 4 processing;
- outbox: 891 done, 1 pending;
- winner, alignment, idempotency, provenance, and version-chain checks passed;
- complete audit failed because one outbox record was pending.

After two bounded drain intervals:

- jobs: 926 completed, 196 queued, 4 processing;
- outbox: 969 done, 2 pending;
- queue-wait p95: 414.057s;
- completion p95: 416.294s.

Zero-unfinished-jobs and outbox convergence therefore failed decisively.

## PostgreSQL observer

The observer harness drift was repaired for this run:

- 723 valid samples;
- zero observer failures;
- peak connections observed: 78;
- 7,452 long-transaction observations.

The largest worker transaction boundary was the extraction-worker update of `proxy_users.last_active_at` and `memory_count`: 862 observations, p50 3.069s, p95 21.621s, p99 34.142s, with 336 transactions >=5s. API idle-in-transaction observations also reached approximately 113 seconds. A background-worker `BEGIN` reached approximately 530 seconds.

This explains why better API admission created more accepted jobs than the four extraction workers could drain. It is not evidence to retain the failed authentication candidate or to change multiple boundaries together.

## Tests and cleanup

- Candidate focused auth/security tests before load: 26 passed.
- Candidate pre-load FAST: 8/8 passed.
- Candidate reverted after failed acceptance.
- Post-revert focused auth/security tests: 21 passed.
- Post-revert FAST: 8/8 passed.
- Deterministic provider; cost `$0`; holdout excluded.
- Scoped cleanup was blocked by active worker transactions, so the disposable workers were stopped and the entire isolated PostgreSQL, Redis, and Qdrant volumes were destroyed. Shared development resources were not used.

## Remaining highest-risk capacity boundary

Do not immediately retry authentication single-flight or tune Redis again. The best next step is diagnosis-only of the extraction-worker hot transaction around `proxy_users.last_active_at` and `memory_count`, including row-lock wait, transaction ownership, whether repeated updates can be safely coalesced, and its relationship to the 196 queued jobs. Propose one isolated repair only after confirming that boundary independently of this reverted candidate.
