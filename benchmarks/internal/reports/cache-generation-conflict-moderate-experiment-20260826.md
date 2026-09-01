# Generation cache invalidation under frozen MODERATE load

Date: 2026-08-26  
Run: `cache-generation-conflict-moderate-rerun-20260826`  
Decision: **failed acceptance; keep generation invalidation benchmark-only**

## Scope

The existing benchmark-only `generation-v1` / `v2` cache candidate was evaluated under the
unchanged frozen MODERATE workload: 8 arrivals/second for 20 minutes, 20 preallocated VUs and 40
maximum VUs. The deterministic benchmark provider remained active, provider cost was zero, and
holdout was inaccessible. No MODERATE traffic was rerun during post-run recovery.

## Outcome

| Metric | Legacy-scan MODERATE reference | Generation candidate | Result |
|---|---:|---:|---|
| Completed iterations | 2,078 | 1,921 | worse |
| Dropped arrivals | 7,522 | 7,649 | worse |
| API error rate | 5.44% | 5.15% | slight improvement; fails <=0.5% |
| HTTP failure rate | 14.23% | 22.38% | worse; fails <=0.5% |
| Add p50/p95/p99 | 17.968/26.207/30.004s | 19.366/28.478/30.001s | worse |
| Retrieval p50/p95/p99 | 17.967/27.486/30.004s | 16.796/25.064/30.000s | improved p50/p95; still fails |
| Client job p50/p95/p99 | 27.424/42.464/52.726s | 30.159/48.860/54.220s | worse |
| Request-path `SCAN` | 2,508 operations | 0 | pass |
| Real cache-command errors | 921 | 9 `GET` errors | >80% reduction, pass |
| Connection/pool errors | 480/480 | 10/10 | >80% reduction, pass |

The candidate successfully removed wildcard invalidation and its direct Redis timeout load. That
was not sufficient to restore service capacity. Authentication cache availability remained only
4.62%: 155 hits, 3,084 misses and 115 timeouts, causing 3,199 PostgreSQL/bcrypt fallbacks. The API
log recorded 590 failed TCP preflights and 34,440 open-circuit fallback messages. Authentication
occupied p50/p95/p99 1.780/4.345/6.201 seconds.

The observed chain remains TCP-preflight/circuit instability -> authentication cache loss ->
database and bcrypt fallback -> request occupancy and arrival drops. Generation invalidation is
not the remaining primary cause.

## Durable state and correctness

- 864/864 jobs completed; retries: 0.
- 903/903 outbox rows converged.
- Single winner, winner alignment, event idempotency, provenance, version-chain integrity and
  outbox convergence all passed with zero violations.
- Post-load FAST passed 8/8.
- The first post-load integration command used incorrect host PostgreSQL credentials and was
  invalid harness configuration, despite being labelled as product failures by the orchestrator.
- The corrected unchanged integration tier passed 5/5 with zero product failures and zero harness
  errors.

The k6 process needed an extended graceful shutdown and reported six interrupted VUs. Durable
PostgreSQL evidence nevertheless showed every accepted job complete and no pending outbox work.
The large durable p99 queue/completion values are retained as evidence and likely include late
accepted work around that client-harness shutdown; they are not hidden or removed.

## Decision

Do not activate generation invalidation in production. Keep it behind the benchmark-only guard.
It resolves the confirmed `SCAN` initiator but fails the frozen end-to-end MODERATE acceptance on
errors, throughput, authentication-cache availability and latency.

The next isolated work should be diagnosis, not another combined repair: explain why positive
authentication cache writes do not remain usable while Redis itself is available, and correlate
the 590 TCP-preflight failures and circuit transitions with cache `SET`/`GET`, expiry and process
ownership. Do not change authentication, Redis deadlines, preflight, circuit semantics or cache
generation until that boundary is proven.

## Cleanup and artifacts

All retained container logs, k6 output, durable snapshot/audit, tail analysis, and corrected gate
artifacts are under
`artifacts/internal-benchmarks/scale/cache-generation-conflict-moderate-rerun-20260826/`.
The explicitly verified `memoryos-scale` containers, network and tmpfs-backed PostgreSQL, Redis and
Qdrant storage were destroyed. Shared development containers/data were not modified.
