# Shared API request-occupancy diagnosis — 2026-08-25

## Decision

Diagnosis is complete using the retained frozen MODERATE artifacts from
`conflict-set-reconciliation-moderate-20260825`. No production behavior, benchmark threshold,
or workload was changed. Holdout was not accessed and no provider call was made.

The retained conflict repair removed the worker-side conflict persistence bottleneck, but the API
still enters a Redis/cache/authentication feedback loop. The first observed circuit opening was an
authentication outer-deadline failure. The dominant sustained Redis command failure was routine
per-user cache invalidation using wildcard `SCAN`. Once the shared circuit opens, authentication
loses its positive cache and repeats PostgreSQL lookup plus synchronous bcrypt verification.

Do not attribute the remaining tail primarily to conflict persistence, Qdrant, PostgreSQL
connection exhaustion, or Celery drain. Do not retry authentication single-flight or deadline
tuning without first removing the confirmed request-path `SCAN` load.

## Frozen run status

- Completed iterations: 2,078; dropped arrivals: 7,522; interrupted: 0.
- API error rate: 5.44%; HTTP request failure rate: 14.23%.
- Add p50/p95/p99: 17.968/26.207/30.004 seconds.
- Retrieval p50/p95/p99: 17.967/27.486/30.004 seconds.
- Job p50/p95/p99: 27.424/42.464/52.726 seconds.
- Final durable state: 778/778 jobs complete and 803/803 outbox rows done.
- All frozen durable correctness invariants passed.

## Request occupancy

| Boundary | Count | p50 | p95 | p99 | Maximum |
|---|---:|---:|---:|---:|---:|
| Add authentication | 914 | 1.840s | 4.842s | 8.958s | 18.444s |
| Retrieval authentication | 1,163 | 1.838s | 4.243s | 8.936s | 17.393s |
| Job-poll authentication | 1,056 | 1.804s | 4.079s | 5.668s | 8.909s |
| Add quota envelope | 896 | 1.095s | 3.006s | 4.788s | 6.764s |
| Retrieval quota envelope | 1,141 | 1.064s | 2.703s | 4.279s | 5.737s |
| Job-poll quota envelope | 1,056 | 1.067s | 2.881s | 5.507s | 12.257s |

Region resolution remained negligible. Webhook session-factory construction was 3.25ms at p95
and is not material.

Authentication cache results were 126 hits, 2,892 misses, and 115 explicit timeouts across 3,133
lookups: a 4.02% hit rate. That forced 3,007 authenticated PostgreSQL/bcrypt fallbacks.

- Database fallback p50/p95/p99: 1.545/3.887/7.343 seconds.
- bcrypt p50/p95/p99: 286.51/438.48/536.28ms.
- Aggregate observed database-fallback occupancy: 5,512.8 seconds.
- Aggregate observed bcrypt time: 910.9 seconds.

The bcrypt cost is an amplifier, not the initiating Redis command failure. Prior isolated auth
deadline and single-flight candidates improved symptoms but failed frozen acceptance and were
reverted while request-path invalidation still used `SCAN`.

## Redis failure ownership

Ignoring `CLIENT` response-label instrumentation noise, timeout distribution was:

| Command | Successful | Timed out | Timeout p50 | Timeout p95 | Timeout p99 |
|---|---:|---:|---:|---:|---:|
| `SCAN` | 1,887 | 621 | 695ms | 1,237ms | 1,713ms |
| `GET` (cache client) | 919 | 110 | 670ms | 1,300ms | 4,699ms |
| `HELLO` | 120 | 56 | 625ms | 1,181ms | 1,636ms |
| `EXPIRE` | 232 | 34 | 672ms | 1,397ms | 6,713ms |
| Other real commands | — | 100 | — | — | — |

`SCAN` produced 621 of 921 cache-command timeout events, or 67.4%, and 495.6 seconds of measured
timeout occupancy. It also consumed 195.4 seconds across successful calls. The 2,508 total scan
operations are consistent with repeated two-pattern invalidation traversals during write traffic.

There were 249 circuit OPEN transitions:

- 113 were cache-client TCP-preflight failures on `GET`/`SET`;
- 85 were cache-client execution timeout/deadline failures on `GET`/`SET`;
- 41 were authentication outer-deadline failures;
- 10 involved other cache operations.

The first observed OPEN came from the fifth authentication outer-deadline failure. This means the
auth wrapper can initiate an interval, but it does not explain the sustained command load. During
the full run the cache client generated 1,984 open-circuit gates versus 618 for the auth client,
and routine `SCAN` was the largest concrete Redis timeout source.

Connection/pool telemetry showed 480 connection errors mirrored by 480 pool-acquisition errors.
All were new-connection timeout paths; the pool limit was 100 and the evidence contains no
`MaxConnectionsError`, Redis max-client rejection, or Redis OOM. This is connection churn after
the cascade, not proof of a bounded-pool capacity failure.

## Route and database amplification

Retrieval route p50/p95/p99 was 7.491/14.099/17.778 seconds. Its largest measured components were:

- retrieval core: 3.536/9.211/11.927 seconds;
- proxy resolution: 1.423/3.292/4.765 seconds;
- feedback persistence: 1.297/3.302/4.426 seconds;
- clarification: 0.398/1.435/2.693 seconds.

Add route p50/p95/p99 was 6.774/11.449/15.516 seconds. Queue/persistence was the largest add
component at 3.567/7.023/8.957 seconds.

API SQL was broadly delayed rather than dominated by one slow query. For example, tenant-budget
SELECT p95 was 974ms and API-key SELECT p95 was 1.328 seconds. The longest aggregate transaction
boundary ended at clarification status update (865 commits, p50/p95/p99
5.052/10.790/13.116 seconds), while the individual update SQL itself was only
303/887/1,695ms. This is shared request/event-loop/database waiting, not evidence for changing
clarification semantics.

The repaired worker conflict SQL remained fast and final jobs/outbox converged. Qdrant-specific
failure evidence was not dominant in this artifact.

## Boundary classification

1. **Confirmed sustained initiator:** routine request-path wildcard cache invalidation `SCAN`.
2. **Shared-circuit amplifier:** cache execution/preflight failures open the shared circuit.
3. **Authentication amplifier:** 95.98% cache unavailability causes 3,007 DB/bcrypt fallbacks.
4. **Database/request amplifier:** proxy, quota, retrieval feedback, clarification, and job polling
   share the resulting event-loop and transaction pressure.
5. **Already repaired/not primary:** cross-user conflict reconciliation and worker conflict SQL.
6. **Not confirmed:** PostgreSQL pool exhaustion, Redis max-client exhaustion, Qdrant as the
   initiating boundary, or a correctness failure.

## One isolated experiment proposed

Run the existing deterministic **generation-based per-user cache invalidation** candidate under the
same frozen MODERATE workload, now on top of the retained conflict reconciliation repair.

The candidate already exists behind an explicit benchmark-only switch and previously proved at LOW
that routine invalidation can use one atomic generation increment with zero request-path `SCAN`.
Do not activate it in production during this experiment. Do not redesign it around this run.

Keep unchanged:

- authentication cache keys/TTL, bcrypt, fallback, and API-key selection;
- Redis timeouts, TCP preflight, circuit, retry, pool, and fallback semantics;
- retrieval ranking/query/top-K, extraction, conflict, claims, lifecycle, Qdrant, and workload;
- hard/privacy deletion, which must remain exhaustive and separate from routine invalidation.

### Frozen acceptance

- request-path `SCAN`: zero after setup/warm-up;
- cache-command timeout count reduced at least 80% from 921;
- Redis connection and mirrored pool-acquisition timeouts reduced at least 80% from 480;
- warm authentication cache hit rate at least 95%;
- warm DB/bcrypt fallbacks at most 1% of authenticated requests;
- API and HTTP failure rates each at most 0.5%;
- existing frozen add/retrieval/job latency thresholds pass;
- zero unfinished jobs, complete outbox convergence, and all correctness invariants pass;
- cache invalidation correctness 100%, including no stale/deleted-memory leakage;
- hard/privacy deletion remains exhaustive;
- FAST and integration gates green, holdout inaccessible, provider cost zero.

If it fails, revert the benchmark override and keep generation invalidation benchmark-only. Do not
combine auth single-flight, deadline changes, or another Redis repair in the same experiment.

