# MODERATE scale baseline — 2026-08-21

Status: **failed performance/capacity; durable correctness passed after drain**.

The frozen workload ran for 20 minutes at 8 scheduled iterations/s with 20 preallocated and 40 maximum VUs. It used only the disposable `memoryos-scale-moderate-20260821` stack, deterministic extraction/embedding fixtures, no holdout, and zero paid-provider cost.

## Result

| Metric | Result |
|---|---:|
| Completed / dropped iterations | 2,003 / 7,598 |
| Achieved iteration rate | 1.64/s |
| API error rate | 17.62% |
| HTTP request failure rate | 24.67% |
| HTTP 500 responses | 9 |
| Add p50 / p95 / p99 | 16.936s / 30.001s / 30.003s |
| Retrieval p50 / p95 / p99 | 18.897s / 30.002s / 30.004s |
| Client-observed job p50 / p95 / p99 | 24.634s / 45.955s / 51.712s |
| Durable queue-wait p50 / p95 / p99 | 2.514s / 7.206s / 16.472s |
| Durable job completion p50 / p95 / p99 | 3.723s / 13.952s / 25.734s |
| Accepted jobs completed after drain | 749 / 749 |
| Outbox converged after drain | 785 / 785 |

The frozen latency and error thresholds failed and were not changed.

## Boundary diagnosis

This was a capacity/performance failure, not observed durable corruption. The API process reached 206.8% sampled CPU and 844 MiB RSS; an observed Docker snapshot showed API 107%, scale worker 116%, PostgreSQL 71%, Redis 4%, and Qdrant 9%. The host heartbeat had no scheduling anomaly and cgroup CPU-throttling counters remained zero.

The earliest concrete amplification boundary is request-scoped construction in `WebhookEventService.__init__`: sampled telemetry recorded 249 synchronous engine creations from that owner and its monotonic owner counter reached 6,100. Redis then showed 695 command, 383 connection, 383 pool-acquisition, and 12 circuit-execution timeouts. Redis server rejected zero connections and evicted zero keys, while the Celery broker queue remained at zero in the observed snapshot. This points to application-side connection/session churn and fallback amplification before ordinary broker backlog.

Qdrant was green after drain with 689 points and an empty update queue. The current harness does not expose separate Qdrant write/search latency, so it must not be inferred from aggregate retrieval latency.

## Correctness

After drain there were zero unfinished jobs and zero pending outbox records. The audit passed:

- exactly one winning revision and winner alignment;
- durable source-event idempotency;
- provenance preservation;
- version-chain integrity;
- outbox convergence.

The k6 correctness counter recorded zero terminal-job failures. FAST passed 8/8 before and after load. Required pre-load integration coverage passed 5/5 after separating a deterministic-provider environment contamination from four mocked-provider tests; that incident was harness configuration, not a product failure.

The frozen k6 workload uses one tenant and authorized agents, so it did not independently generate an adversarial cross-tenant/user/agent leakage probe during load. Existing preflight security suites remained green, but load-time leakage is therefore **not directly measured** and is not claimed as a new zero-leakage result.

## Next isolated investigation

Do not start HIGHER or SUSTAINED. Trace `WebhookEventService` construction and database session-factory ownership per request, then compare the unchanged MODERATE workload with factory reuse in an isolated experiment. The experiment must not alter extraction, retrieval, Redis semantics, claims, permissions, or ranking.

Run-scoped fixtures were removed: 80 proxy users and 689 Qdrant points deleted, with zero scoped memories, claims, revisions, jobs, events, or outbox records remaining before volume teardown.
