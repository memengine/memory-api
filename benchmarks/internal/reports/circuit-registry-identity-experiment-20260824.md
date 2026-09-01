# Circuit registry identity experiment — 2026-08-24

Status: **failed full acceptance and reverted**.

## Isolated candidate

Normal FastAPI lifespan startup reused `CircuitBreakerRegistry.get_instance()` instead of replacing
the registry with `reset()`. This made middleware-captured and startup-created services share one
process-local Redis breaker. No circuit policy, threshold, deadline, retry, fallback, Redis command,
cache, authentication, extraction, retrieval, or persistence behavior was changed.

Run: `circuit-registry-identity-moderate-20260824`  
Reference: `redis-circuit-transition-moderate-20260824`

## Focused verification

The candidate regression test proved that lifespan did not replace a breaker already captured by
authentication middleware and that the cache used the same Redis breaker object. The focused
circuit/auth/configuration suite passed 18/18 before load.

One broader endpoint test exposed unrelated harness drift: `StubWebhookService` lacks
`_verify_svix_signature`. It was not treated as a product failure.

## Frozen MODERATE result

| Measurement | Reference | Candidate | Outcome |
|---|---:|---:|---|
| Completed iterations | 2,153 | 1,796 | 16.6% worse |
| Dropped iterations | 7,448 | 7,801 | 4.7% worse |
| API error rate | 24.71% | 23.83% | slightly better, still failed |
| HTTP request failure rate | 36.75% | 42.17% | worse |
| Add p50 / p95 / p99 | 19.243 / 29.892 / 30.005 s | 23.201 / 30.001 / 30.005 s | worse |
| Retrieval p50 / p95 / p99 | 16.901 / 28.924 / 30.006 s | 20.732 / 30.002 / 30.018 s | worse |
| Job p50 / p95 / p99 | 30.968 / 53.648 / 60.056 s | 32.761 / 54.482 / 57.935 s | mixed |
| Circuit-open gates | 1,050 | 2,113 | 101.2% worse |
| Circuit execution errors | 920 | 262 | improved |
| Force-open transitions | 219 | 564 | worse |

The unified identity was observable: after authentication opened the breaker, cache operations were
gated by the same OPEN state. This removed the split local-state boundary, but it also widened the
blast radius of caller-forced OPEN state across all Redis consumers.

## Correctness and acceptance

All 651 accepted jobs completed with zero retries. Single-winner correctness, winner alignment,
durable event idempotency, provenance preservation, and version-chain integrity passed. However,
38 of 654 outbox rows were failed after drain, so outbox convergence and the aggregate durable audit
failed. The candidate therefore failed the required 100% durable-correctness acceptance gate.

The PostgreSQL observer process did not emit its expected artifact, so peak connection count is a
harness-observation gap for this run and is not inferred from service logs.

## Decision

The registry-identity candidate was reverted. Post-revert focused tests passed 17/17. Normal
production behavior remains at the pre-experiment reference state.

The evidence indicates that registry identity is not safe as a standalone change while every Redis
caller can force the shared circuit OPEN. Before reconsidering identity unification, the next work
should be diagnosis-only: classify which caller failures are true Redis availability failures versus
local pool/deadline failures, and define whether `force_open` is allowed to affect the shared breaker.
No policy repair is included here.

## Cleanup and artifacts

Cleanup removed 80 proxy users, 593 Qdrant points, and 12 run-scoped Redis keys; all scoped database
rows were removed. The disposable containers, network, and volumes were destroyed.

- `artifacts/internal-benchmarks/scale/circuit-registry-identity-moderate-20260824/`
