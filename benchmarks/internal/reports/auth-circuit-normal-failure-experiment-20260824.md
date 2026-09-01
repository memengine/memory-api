# Authentication circuit normal-failure experiment — 2026-08-24

Status: **failed acceptance and reverted**.

## Isolated change tested

Only API-key authentication's 200 ms Redis cache lookup/fill wrapper timeouts were changed. Instead
of calling `force_open`, each timeout recorded one normal circuit failure and therefore used the
existing five-failures-in-ten-seconds threshold. Redis deadlines, TCP preflight mode, cache
semantics, fallback, authentication results, and every non-authentication caller were unchanged.

Run: `auth-normal-failure-moderate-20260824`  
Reference: `redis-circuit-transition-moderate-20260824`

Both runs used the frozen 20-minute MODERATE workload, disposable `memoryos-scale` stack,
deterministic provider, generation invalidation v1, cache namespace v2, disabled benchmark TCP
preflight, zero paid-provider cost, and no holdout access.

## Before/after

| Measurement | Reference | Candidate | Outcome |
|---|---:|---:|---|
| Completed iterations | 2,153 | 1,885 | **12.4% worse** |
| Dropped iterations | 7,448 | 7,715 | **3.6% worse** |
| API error rate | 24.71% | 21.21% | improved 3.50 points |
| HTTP request failure rate | 36.75% | 33.51% | improved 3.24 points |
| Circuit opens | 285 | 653 | **129.1% worse** |
| Direct `force_open` transitions | 219 | 293 | **33.8% worse overall** |
| Auth direct `force_open` | 135 | 0 | local mechanism passed |
| Circuit-open gates | 1,050 | 2,377 | **126.4% worse** |
| Circuit execution errors | 920 | 424 | improved 53.9% |
| Auth cache hits / timeouts | 383 / 77 | 224 / 123 | worse |
| PostgreSQL maximum connections | 92 | 83 | improved |
| Add p50 / p95 / p99 | 19.243 / 29.892 / 30.005 s | 21.253 / 30.005 / 30.021 s | worse |
| Retrieval p50 / p95 / p99 | 16.901 / 28.924 / 30.006 s | 19.905 / 30.004 / 30.027 s | worse |
| Job completion p50 / p95 / p99 | 30.968 / 53.648 / 60.056 s | 31.623 / 50.301 / 55.863 s | mixed |

The candidate's 653 open transitions consisted of:

- 119 threshold opens from accumulated authentication wrapper timeouts;
- 241 threshold opens from circuit execution failures;
- 293 direct forced opens from other services, led by quota management (124), cache (62), proxy
  user service (57), rate limiting (28), and quality gate (22).

Removing authentication's threshold bypass therefore worked locally but did not stabilize the
shared circuit. Other direct-open paths remained and the candidate produced materially fewer
completed iterations, more drops, more circuit opens, more circuit-open fallbacks, and worse API
latency. The improvement in API/HTTP error rates and PostgreSQL peak was insufficient to satisfy
the predefined throughput and circuit acceptance conditions.

## Correctness and cleanup

- 624/624 accepted extraction jobs completed.
- 654/654 outbox records converged.
- Single winner, winner alignment, idempotency, provenance, version-chain integrity, and outbox
  convergence passed with zero violations.
- PostgreSQL observer: 2,678 samples, zero observer failures, peak 83 connections.
- Focused post-revert tests: 24 passed.
- Cleanup removed 80 proxy users, 578 Qdrant points, and 9 run-scoped Redis keys; no scoped database
  rows remained.
- Disposable containers, network, and volumes were destroyed.

## Decision

The candidate was reverted. Production behavior remains at the pre-experiment reference state.

Before another behavior change, perform one read-only shared-circuit policy inventory/sequence
diagnosis across every remaining `force_open` caller. The evidence shows that changing one caller
in isolation can shift failures to other callers rather than improve the shared circuit globally.

## Artifacts

- `artifacts/internal-benchmarks/scale/auth-normal-failure-moderate-20260824/redis-circuit-transition-analysis.json`
- `artifacts/internal-benchmarks/scale/auth-normal-failure-moderate-20260824/k6-moderate.json`
- `artifacts/internal-benchmarks/scale/auth-normal-failure-moderate-20260824/postgres-observer.json`
- `artifacts/internal-benchmarks/scale/auth-normal-failure-moderate-20260824/traffic-end-snapshot.json`
- `artifacts/internal-benchmarks/scale/auth-normal-failure-moderate-20260824/final-audit.json`
