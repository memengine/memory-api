# Frozen MODERATE scale baseline

Date: 2026-08-23  
Run: `moderate-command-health-20260823`  
Decision: **failed; do not start HIGHER or SUSTAINED**

## Environment and gates

The unchanged 20-minute MODERATE workload ran only in the disposable `memoryos-scale` stack at
8 scheduled iterations/second, with 20 preallocated VUs and a 40-VU ceiling. The deterministic
provider was active, holdout was inaccessible, and provider cost was zero. Accepted command-driven
Redis health and generation-based cache invalidation were enabled.

Pre-load FAST passed 8/8 and INTEGRATION passed 5/5. Post-load FAST passed 8/8. A full post-load
INTEGRATION run was not started after the frozen failure/stop condition; the run-specific database
audit was performed instead.

## Performance result

| Metric | Result | Frozen target |
|---|---:|---:|
| Completed / dropped iterations | 2,322 / 7,278 | sustain scheduled arrivals |
| API error rate | 16.84% | <=0.5% |
| HTTP failure rate | 21.01% | <=0.5% |
| HTTP 500 | 0 | 0 Redis-related |
| Add p50 / p95 / p99 | 15.532 / 30.001 / 30.004 s | <0.5 / <0.5 / <1.0 s |
| Retrieval p50 / p95 / p99 | 14.296 / 30.001 / 30.004 s | <0.75 / <0.75 / <1.5 s |
| Job client p50 / p95 / p99 | 23.021 / 43.651 / 51.450 s | p95 <10 s |
| Queue wait p50 / p95 / p99 | 2.440 / 22.922 / 51.151 s | observed |
| DB job completion p50 / p95 / p99 | 3.768 / 28.022 / 56.997 s | observed |

The 40-VU ceiling was reached during the first minute and remained saturated or nearly saturated
for most of the run. This is a capacity failure, not a threshold-edge failure.

## Saturated boundaries

The dominant request-path feedback loop was:

1. Redis connection/pool operations failed under concurrency (245 each), despite zero TCP-preflight
   calls and zero request-path SCANs.
2. The Redis circuit produced 23,324 fallbacks.
3. Authentication cache hit rate fell to 32.68%; 2,861 requests performed database plus bcrypt
   authentication.
4. Authentication database fallback reached p95 12.15 s and p99 23.69 s.
5. API workers saturated, producing 30-second add/retrieval timeouts, while extraction queue wait
   reached p95 22.92 s.

PostgreSQL reported no connection-exhaustion signature, HTTP 500 count was zero, and all 910 outbox
rows converged. Qdrant/outbox consistency was therefore not the primary boundary.

## Correctness and reliability

Single-winner correctness, winner alignment, event idempotency, provenance, version chains, and
outbox convergence all passed with zero violations.

However, 4 of 853 persisted extraction jobs remained `queued` after bounded drain. Each had zero
attempts and no `celery_task_id`. They were created early in the overload window and never reached
Celery dispatch. The code commits the job row before dispatch, while the watchdog only recovers
stale `processing` jobs. A request timeout/cancellation between those boundaries can therefore
leave a durable but permanently undispatched queued job. This is a genuine integration-reliability
failure, separate from the capacity failure.

## One isolated next repair

Extend the existing watchdog with one atomic recovery path for jobs that:

- remain `queued` beyond a defined dispatch-grace interval;
- have no `celery_task_id` and zero processing start time;
- are claimed with a compare-and-set/row-lock transition so concurrent watchdogs dispatch once;
- retain the existing tenant queue route and job payload;
- remain idempotent if the original dispatch eventually arrives.

Acceptance should require all deliberately stranded queued jobs to reach one durable completion,
no duplicate processing or memories, exactly one task dispatch outcome, and no regression in the
existing stale-`processing` watchdog behavior. Do not tune Redis pools, worker counts, timeouts, or
load thresholds in the same repair. After that repair is validated, separately investigate Redis
connection/pool saturation before rerunning MODERATE.

All run fixtures were removed (80 proxy users, 793 Qdrant points, 12 Redis keys), database run rows
returned to zero, and the disposable containers, network, and volumes were destroyed.
