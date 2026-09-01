# PostgreSQL/request attribution under frozen MODERATE — 2026-08-23

Status: diagnosis complete; run failed performance gates and full-path evaluation was partially invalidated by two harness failures. No production behavior was changed.

## Scope

- Disposable `memoryos-scale` stack only.
- Frozen MODERATE workload: 8 iterations/s for 20 minutes.
- Deterministic provider; zero paid calls; holdout excluded.
- Existing Redis/cache configuration retained.
- Benchmark-only pool, SQL, transaction, request-phase and runtime telemetry added.

## Workload result

| Metric | Result |
|---|---:|
| Completed / dropped | 2,477 / 7,123 |
| API error rate | 15.54% |
| HTTP failure rate | 23.41% |
| Add p50 / p95 / p99 | 16.341 / 28.000 / 30.002 s |
| Retrieval p50 / p95 / p99 | 14.093 / 25.103 / 30.001 s |
| Job p50 / p95 / p99 | 25.226 / 49.024 / 55.977 s |

MODERATE performance acceptance failed.

## PostgreSQL attribution

The API process had two active async pool families, each configured for 20 pooled plus 30 overflow connections.

| Owner | Peak checked out | Peak overflow |
|---|---:|---:|
| Global/import-path async pool | 45 / 50 | 25 |
| Regional async pool | 40 / 50 | 20 |

Manual server snapshots generally plateaued near 45–48 application sessions. Late samples reached 58 total sessions with 22 idle-in-transaction and 57 with 25 idle-in-transaction, then recovered. No PostgreSQL `too many connections` or QueuePool timeout was observed.

Sampled/slow SQL telemetry:

| Owner/operation | p95 | Maximum |
|---|---:|---:|
| Global UPDATE | 5.977 s | 31.301 s |
| Global SELECT | 1.999 s | 10.461 s |
| Regional SELECT | 1.302 s | 4.025 s |
| Regional INSERT | 1.154 s | 3.702 s |

Transactions were retained for long periods:

| Owner/outcome | p50 | p95 | p99 | Maximum |
|---|---:|---:|---:|---:|
| Global commit | 0.989 s | 5.847 s | 14.474 s | 32.646 s |
| Regional commit | 0.787 s | 3.627 s | 5.814 s | 12.081 s |
| Regional rollback | 0.531 s | 2.205 s | 3.595 s | 6.118 s |

## Initiating-boundary ordering

Timestamped first signals:

1. Auth cache timeout at `11:15:54.347Z`.
2. API event-loop lag reached 1.184 s at `11:15:55.031Z` with about 100% process CPU and no cgroup throttling.
3. Cache Redis circuit deadline at `11:15:55.034Z`.
4. First transaction exceeding 5 s at `11:16:01.053Z`.
5. Regional pool reached 40 checkouts at `11:16:10.349Z`.

PostgreSQL pool pressure therefore followed the auth/event-loop/cache failure cascade. It amplified latency but was not the initiating boundary in this run.

Authentication evidence supports the same ordering:

- cache misses: 3,209; hits: 681; explicit timeouts: 76;
- database fallbacks and inline bcrypt checks: 3,285;
- bcrypt p50/p95/p99: 279/494/600 ms;
- complete database fallback p50/p95/p99: 1.660/8.404/18.551 s;
- full API-key auth p50/p95/p99: 1.650/8.191/16.491 s.

Redis pool capacity itself was not exhausted: observed auth/cache pool use peaked at 6/100 and 16/100. Cache circuit deadlines and shared-circuit fallback were consequences of event-loop delay, consistent with the preceding role-attribution diagnostic.

## Correctness and harness classification

Durable invariants that could be evaluated passed: single winner, winner alignment, event idempotency, provenance and version chains. At snapshot time 844 jobs completed and 21 remained queued.

Two harness failures prevent accepting this as the consolidated MODERATE baseline:

1. The independent PostgreSQL observer used a stale benchmark password: 0 valid samples and 641 `password authentication failed` errors. Manual server snapshots and in-process telemetry remain valid.
2. `celery-background` exited after the post-migration Compose restart because `/tmp/celery-background.pid` already existed. Consequently 784 outbox rows remained pending and Qdrant convergence was not evaluable. This is harness/service startup drift, not a demonstrated outbox product failure.

## Conclusion and next step

Do not increase PostgreSQL or Redis pool limits. The confirmed product-side cascade begins with event-loop starvation around repeated inline bcrypt/database authentication fallback; PostgreSQL transaction/pool pressure follows.

Before another product experiment, make one isolated benchmark-harness reliability repair: ensure disposable service restart removes/avoids stale Celery pidfiles, require every expected service to be running before traffic starts, and derive the observer connection from the actual disposable stack credentials. Then rerun the same frozen MODERATE workload. Only after a fully evaluable rerun should a bounded authentication-admission experiment be approved; previous unbounded thread offload and single-flight experiments already failed acceptance and were reverted.
