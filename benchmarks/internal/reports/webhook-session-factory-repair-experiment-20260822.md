# Webhook session-factory repair experiment — 2026-08-22

Status: **failed acceptance and reverted**.

The isolated repair created one process-owned synchronous webhook session factory during FastAPI lifespan, injected it into API quota/webhook services, and disposed it on shutdown. No worker, quota, webhook-delivery, Redis, extraction, retrieval, claim, or ranking semantics changed.

## Validation

- Focused lifecycle/quota/webhook tests before load: 13/13 passed.
- FAST before load: 8/8 passed.
- Integration before load: 5/5 passed.
- Deterministic provider active; paid-provider cost $0; holdout unused.
- One initial MODERATE attempt was excluded because its host launcher used the wrong source-service identity and caused HTTP 422 harness failures.
- The valid rerun used the registered `scale-benchmark` writer and the unchanged frozen workload.

## Valid MODERATE result

| Metric | Reference | Repair |
|---|---:|---:|
| Completed iterations | 2,003 | 1,749 |
| Dropped iterations | 7,598 | 7,852 |
| API error rate | 17.62% | 20.01% |
| HTTP request failure rate | 24.67% | 36.34% |
| Add p50 / p95 / p99 | 16.936s / 30.001s / 30.003s | 17.883s / 30.001s / 30.004s |
| Retrieval p50 / p95 / p99 | 18.897s / 30.002s / 30.004s | 22.398s / 30.002s / 30.011s |
| Job p50 / p95 / p99 | 24.634s / 45.955s / 51.712s | 27.142s / 47.729s / 53.148s |

The factory-specific mechanism worked: request-created `webhook_session_factory_creation` telemetry fell from a 6,100 owner counter in the reference run to zero. Nevertheless, all major performance gates still failed and one of 796 accepted jobs remained queued after the drain window.

Durable audit checks still passed for single winner, winner alignment, event idempotency, provenance, version chains, and outbox convergence. The audit currently does not include unfinished-job state; the separate snapshot exposed the queued job.

## Remaining boundary

The next measured request-side amplification is API-key authentication cache failure/fallback:

- cache hits: 280;
- cache misses: 2,783;
- cache timeouts: 70;
- database fallback/authentication executions: 2,853;
- database fallback p50/p95/p99: 1.793s / 7.489s / 12.589s;
- bcrypt verification p50/p95/p99: 333ms / 516ms / 611ms;
- Redis circuit-open fallback messages: 30,167.

API CPU was approximately 102% while the scale worker was below 1%, the broker queue was zero, PostgreSQL about 21%, Redis about 2%, and Qdrant about 7%. This localizes the remaining failure to request-side authentication/cache fallback rather than worker capacity.

## Decision

The repair was reverted because the predeclared MODERATE acceptance criteria failed. Post-revert quota/webhook tests passed 10/10. The first FAST attempt encountered the known Windows pytest-temp ACL harness error; a workspace-local-temp retry passed 8/8 with no product failures.

The next isolated step should be **diagnosis only** of why API-key auth cache misses/circuit-open fallback occur under MODERATE traffic. Do not change authentication semantics or cache behavior until that trace is reviewed and approved.

All valid/invalid run-scoped fixtures were removed and the disposable stack and volumes were destroyed.
