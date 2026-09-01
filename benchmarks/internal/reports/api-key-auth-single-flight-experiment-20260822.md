# API-key authentication single-flight experiment

Date: 2026-08-22  
Run: `moderate-auth-singleflight-20260822`  
Decision: **failed acceptance and reverted**

## Change tested

The experiment added one process-local in-flight task per hashed API-key cache key. The elected
leader executed the existing database/bcrypt/cache-fill fallback and offloaded its one bcrypt
check; same-key followers awaited the shared task with cancellation shielding. No cache TTL,
Redis deadline/circuit, authentication decision, query, quota, feedback, webhook, extraction,
retrieval, claim, or worker behavior changed.

Focused concurrency tests covered valid and invalid coalescing, different-key isolation, follower
cancellation, exception cleanup, retry, and absence of raw keys in flight identities.

The disposable `memoryos-scale` stack used the deterministic provider. Holdout was excluded and
provider cost was zero. Pre-load FAST passed 8/8 and INTEGRATION passed 5/5.

## Frozen MODERATE result

| Metric | Result | Acceptance |
|---|---:|---:|
| Completed / dropped iterations | 2,991 / 6,608 | capacity target not met |
| Interrupted iterations | 1 | 0 |
| API error rate | 4.75% | <=0.50% |
| HTTP failure rate | 1.86% | <=0.50% |
| Add p50 / p95 / p99 | 4.018s / 19.559s / 30.007s | p95 <0.5s; p99 <1s |
| Retrieval p50 / p95 / p99 | 5.371s / 21.937s / 30.010s | p95 <0.75s; p99 <1.5s |
| Job p50 / p95 / p99 | 13.524s / 25.983s / 30.950s | p95 <10s |
| Cache hits / lookups | 1,072 / 13,488 = 7.95% | >=95% after warm-up |
| Database fallbacks / bcrypt checks | 728 / 728 | <=1% after warm-up |
| HTTP 500 log lines | 3 | 0 Redis-related 500s |
| Jobs after drain snapshot | 313 queued, 4 processing | 0 unfinished |

The durable audit passed single-winner, winner-alignment, event-idempotency, provenance,
version-chain, and then-current outbox checks with zero violations. Capacity/drain acceptance
failed because 317 jobs remained unfinished.

## What improved and what did not

Single-flight materially worked as a deduplication mechanism:

- non-hit cache lookups: 12,416;
- database/bcrypt leaders: 728;
- approximately 94.14% of non-hit requests avoided their own fallback;
- observed API CPU early in the run was about 250%, versus about 881% under unrestricted thread
  offload;
- completed iterations increased from 2,556 to 2,991.

It did not restore the Redis authentication cache. There were only 1,072 hits and 121 explicit
cache-lookup timeouts. The existing 200 ms authentication wrapper force-opens the shared Redis
circuit, after which later non-overlapping request groups elect new fallback leaders. Single-flight
protects concurrent bursts but provides no result once a flight finishes, so prolonged
circuit-open windows continue to create repeated leaders and database work.

Latency and errors remained far outside the frozen thresholds. The repair therefore cannot be
retained independently even though its local coalescing contract was correct.

## Decision and next investigation

The single-flight implementation and temporary tests were reverted. Post-revert focused tests
passed 24/24 and FAST passed 8/8. Run-scoped cleanup removed every proxy, memory, claim, revision,
job, source event, outbox row, Qdrant point, and Redis key; disposable containers and volumes were
destroyed.

Do not immediately combine single-flight with another production change. Next perform a
diagnosis-only replay of authentication cache state transitions under the coalesced evidence:
correlate each 200 ms wrapper timeout, `force_open`, 30-second circuit interval, cache SET outcome,
and the next successful hit. Determine whether the dominant remaining issue is wrapper-induced
circuit opening, cache-fill failure, or shared-circuit coupling. Only then propose one isolated
repair.
