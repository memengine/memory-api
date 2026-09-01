# API-key authentication deadline experiment

Date: 2026-08-22  
Run: `moderate-auth-deadline-repair-rerun-20260822`  
Decision: **failed acceptance and reverted**

The experiment removed only the authentication-local 200 ms Redis GET/SET wrappers and delegated timeout control to the existing 500 ms Redis command/socket limits and 750 ms circuit deadline. The frozen MODERATE workload ran for 20 minutes in the disposable deterministic-provider stack. Holdout and paid providers were not used.

## Results

| Metric | Result |
|---|---:|
| Completed / dropped iterations | 2,532 / 7,063 |
| API error rate | 26.12% |
| HTTP failure rate | 24.43% |
| Add p50 / p95 / p99 | 9.218s / 30.004s / 30.007s |
| Retrieval p50 / p95 / p99 | 13.439s / 30.005s / 30.010s |
| Job p50 / p95 / p99 | 13.279s / 44.524s / 50.833s |
| Cache hit rate | 61.00% |
| Database fallbacks | 2,112 |
| DB fallback p50 / p95 / p99 | 3.157s / 22.793s / 39.491s |
| bcrypt verifications | 2,119 |
| Redis circuit-open fallbacks | 24,066 |
| Accepted jobs completed | 891 / 891 |
| Outbox | 947 done / 5 failed |

Cache behavior improved relative to the diagnosed 8.94% hit rate, but remained far below the 95% warm-cache acceptance threshold. API errors and tail latency remained unacceptable. Five outbox rows failed to converge after drain, so correctness acceptance also failed.

Single-winner correctness, winner alignment, durable event idempotency, provenance preservation, and version-chain integrity passed.

## Conclusion

The 200 ms wrapper is not the sole cause. Once removed, Redis operations still experience multi-second observed delays under request-side event-loop pressure, trip the shared circuit, and recreate DB+bcrypt amplification. Logs concurrently show large request-phase delays and request-created synchronous webhook session factories. The isolated deadline removal cannot be retained safely on its own.

The production behavior and temporary test were reverted. Focused auth/circuit/Redis tests passed 24/24 and the consolidated FAST tier passed 8/8. Run-scoped fixtures and all disposable containers were removed.

## Next investigation

Do not tune another Redis timeout. Measure event-loop blocking and request-phase ownership together, specifically synchronous bcrypt execution, database `last_used_at` commits, quota/feedback response work, and request-created synchronous session factories. The next step should remain diagnosis-only and identify one dominant blocking boundary before another repair.
