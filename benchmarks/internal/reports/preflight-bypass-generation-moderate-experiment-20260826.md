# Preflight-bypass plus generation invalidation under MODERATE load

Date: 2026-08-26  
Run: `preflight-bypass-generation-moderate-20260826`  
Decision: **failed performance acceptance; correctness passed; stop Redis tuning**

## Result

The approved isolated experiment disabled only the benchmark TCP preflight while retaining the
benchmark-only generation-v1/v2 invalidation candidate. The frozen workload remained 8 arrivals/s
for 20 minutes with 20 preallocated and 40 maximum VUs. Provider cost was zero and holdout was not
loaded.

| Metric | Preflight enabled reference | Preflight disabled | Acceptance |
|---|---:|---:|---|
| Completed | 1,921 | 2,591 | improved, capacity still failed |
| Dropped | 7,649 | 7,010 | improved, still failed |
| API error | 5.15% | 11.96% | worse; failed <=0.5% |
| HTTP failure | 22.38% | 20.62% | improved slightly; failed <=0.5% |
| Add p50/p95/p99 | 19.366/28.478/30.001s | 16.717/28.796/30.006s | mixed; failed |
| Retrieval p50/p95/p99 | 16.796/25.064/30.000s | 15.374/28.212/30.003s | mixed; failed |
| Job p50/p95/p99 | 30.159/48.860/54.220s | 25.653/48.392/55.650s | mixed; failed |
| Auth cache hit rate | 4.62% | 32.04% | improved; failed >=95% |
| DB/bcrypt fallback | 3,199 | 2,762 | improved; failed <=1% |
| TCP preflight failures | 590 | 0 | mechanism passed |
| Connection/pool failures | 10/10 | 652/652 | decisively worse |

Disabling preflight removed false probe failures, but it did not restore MODERATE capacity. Under
unrestricted command attempts, Redis connection/command deadlines became the dominant symptom.
This confirms broader API event-loop/resource starvation rather than one remaining Redis setting.

## Correctness and gates

- 878/878 accepted jobs completed; zero retries or unfinished jobs.
- 916/916 outbox rows converged.
- Winner, alignment, idempotency, provenance and version chains passed with zero violations.
- FAST before/after: 8/8 and 8/8.
- INTEGRATION before/after: 5/5 and 5/5.
- No paid provider call and no holdout access.

## Final decision

Do not activate either benchmark-only cache generation or preflight bypass in production based on
these scale experiments. Do not run another Redis-tuning experiment now. The system has strong
functional and durability correctness but does not sustain the artificial frozen MODERATE target
on this single-node disposable configuration.

This capacity limitation should not indefinitely block public memory-quality benchmarks or a
controlled beta launch. It must block claims of MODERATE/high-scale readiness and requires launch
guardrails: conservative rate limits, queue/backpressure monitoring, staged tenant admission and a
documented degraded-service policy.

The disposable `memoryos-scale` containers, network and temporary PostgreSQL/Redis/Qdrant storage
were destroyed. The RecoveryOS container using port 18000 was not modified; this experiment used
port 18001.
