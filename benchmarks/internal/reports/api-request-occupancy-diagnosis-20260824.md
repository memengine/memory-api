# API request-occupancy diagnosis — 2026-08-24

## Decision

Diagnosis complete from the frozen `redis-failure-ownership-moderate-20260824` artifacts. No production behavior was changed and another load run was not required. The primary request-occupancy cascade is low authentication-cache availability followed by repeated PostgreSQL plus synchronous bcrypt fallback. Database-backed route work then amplifies the tail across add, retrieval, feedback, clarification, proxy resolution, and job polling.

Do not attribute the MODERATE failure to Qdrant alone, Celery alone, or PostgreSQL connection exhaustion. Do not tune Redis deadlines again based on this run.

## Frozen workload result

- 1,939 completed; 7,658 dropped; 3 interrupted.
- API error rate: 16.01%.
- HTTP failure rate: 27.31%.
- Add p50/p95/p99: 20.507/30.001/30.002 seconds.
- Retrieval p50/p95/p99: 18.874/30.001/30.002 seconds.
- Job completion p50/p95/p99: 30.538/52.411/55.296 seconds.
- All 727 accepted jobs and all 753 outbox rows converged.
- Durable correctness audit passed.

## Shared request occupancy

| Boundary | Count | p50 | p95 | p99 | Maximum |
|---|---:|---:|---:|---:|---:|
| API-key authentication, all API paths | 3,403 | 1.775s | 6.895s | 12.900s | 38.475s |
| Add authentication | 892 | 1.966s | 7.086s | 12.755s | 24.117s |
| Retrieval authentication | 1,048 | 1.873s | 7.756s | 12.754s | 20.788s |
| Job-poll authentication | 1,463 | 1.626s | 6.125s | 12.900s | 38.475s |
| Quota envelope, add | 837 | 0.982s | 3.647s | 5.309s | 9.336s |
| Quota envelope, retrieval | 1,010 | 0.901s | 3.254s | 5.423s | 9.428s |
| Quota envelope, job poll | 1,463 | 0.735s | 3.284s | 5.127s | 14.752s |

Region resolution stayed below 0.1ms at p99 and is not material. Webhook session-factory construction was 3.58ms at p95 and is also not the dominant wait.

## Authentication decomposition

- Cache hits: 717; misses: 2,540; explicit timeouts: 146.
- Cache hit rate: 21.07%.
- Database/bcrypt fallbacks: 2,686, or 78.93% of authenticated requests.
- Cache-hit p50/p95/p99: 77.83/159.65/200.35ms.
- Bcrypt p50/p95/p99: 305.02/454.97/523.66ms.
- Full database fallback p50/p95/p99: 1.821/6.960/12.861s.

The direct bcrypt cost is material, but most fallback latency is accumulated database/event-loop wait rather than bcrypt computation alone. The accepted failure-ownership repair stopped duplicate/forced opens, yet the shared circuit still gated 1,947 Redis operations after genuine owned failures. Once the cache becomes unavailable, repeated database fallback and inline bcrypt increase request occupancy and make Redis/database progress slower.

## Add route decomposition

| Route phase | Count | p50 | p95 | p99 | Maximum |
|---|---:|---:|---:|---:|---:|
| Quality gate | 725 | 1.184s | 3.662s | 7.160s | 14.826s |
| Proxy resolution | 725 | 0.918s | 3.424s | 5.290s | 9.966s |
| Queue/persistence | 725 | 3.706s | 7.619s | 12.035s | 22.207s |
| Complete route body | 725 | 6.853s | 12.946s | 18.742s | 31.360s |

Queue/persistence is the largest add-route component. However, it occurs after the shared authentication wait and coincides with long API PostgreSQL transactions, so it is an amplification boundary rather than evidence for changing Celery dispatch or extraction.

## Retrieval route decomposition

| Route phase | Count | p50 | p95 | p99 | Maximum |
|---|---:|---:|---:|---:|---:|
| Proxy resolution | 1,010 | 1.046s | 4.191s | 7.548s | 15.037s |
| Retrieval core | 1,010 | 1.798s | 5.670s | 8.728s | 12.817s |
| Feedback persistence | 1,010 | 1.320s | 4.137s | 6.937s | 11.726s |
| Clarification lookup | 1,010 | 0.411s | 1.950s | 3.078s | 6.483s |
| Domain context | 1,010 | 0.070s | 0.868s | 1.451s | 3.671s |
| Context construction | 1,010 | 0.000s | 0.001s | 0.002s | 7.864s wall-time outlier |
| Complete route body | 1,010 | 5.633s | 14.225s | 19.450s | 25.036s |

Context CPU time is negligible at normal percentiles. Retrieval-core time is important, but proxy and feedback persistence together are of comparable magnitude. The evidence does not support a Qdrant-specific change yet.

## PostgreSQL amplification

| Process/outcome | Count | p50 | p95 | p99 | Maximum | >=5s |
|---|---:|---:|---:|---:|---:|---:|
| API commit | 7,771 | 0.815s | 4.577s | 8.800s | 37.119s | 323 |
| API rollback | 4,170 | 0.626s | 2.967s | 7.872s | 15.093s | 82 |
| Extraction-worker commit | 859 | 0.614s | 11.062s | 22.437s | 26.465s | 140 |
| Background-worker commit | 95 | 0.019s | 0.091s | 0.166s | 0.166s | 0 |

No PostgreSQL connection error was present in the service-log analysis, so this is transaction/wait amplification rather than confirmed server exhaustion. The independent observer did not sample because its host launch omitted the benchmark environment variables; detailed connection-count percentiles remain unavailable.

## Boundary classification

1. **Initiating/shared boundary:** authentication-cache availability collapses to 21.07% under sustained concurrency.
2. **Amplifier:** 2,686 database fallbacks each include synchronous bcrypt and database work.
3. **API persistence amplification:** add queue persistence, proxy resolution, retrieval feedback, and clarification operations wait behind shared database/event-loop pressure.
4. **Worker amplification:** extraction-worker commits reach 11.06s p95, explaining job completion tail after API acceptance.
5. **Not primary:** region resolution, context CPU, webhook factory construction, durable outbox processing, or correctness logic.
6. **Harness drift:** PostgreSQL observer launch environment, not product behavior.

## One isolated experiment proposed

Re-evaluate **per-key authentication fallback single-flight with one bounded offloaded leader**, now that caller-forced Redis opens and recovery-clock resets have been removed.

This is intentionally not the earlier experiment's environment: that experiment was rejected while the 200ms caller timeout force-opened the shared circuit repeatedly. The accepted failure-ownership repair removed that confounder. The candidate should only coalesce simultaneous fallback work for the same hashed API-key identity; different API keys remain independent. The leader performs the unchanged database lookup, bcrypt verification, and cache fill; followers await the same result. No result cache beyond the existing Redis cache and no raw key in the flight identity.

Keep unchanged: Redis timeouts/circuit/retries, cache TTL and payload, key selection, bcrypt verification semantics, permissions, last-used persistence, quota, extraction, retrieval, claims, workers, and workload.

Acceptance:

- cache hit rate >=95% after warm-up;
- database/bcrypt fallback leaders <=1% of authenticated requests after warm-up;
- peak simultaneous bcrypt leaders for the same key exactly one;
- API errors <=0.5% and HTTP failures <=0.5%;
- add p95 <500ms and p99 <1s;
- retrieval p95 <750ms and p99 <1.5s;
- job p95 <10s;
- zero unfinished jobs and complete outbox convergence;
- all authorization and durable correctness invariants pass;
- no raw API key exposure, no holdout, and zero provider cost;
- candidate must be reverted if full frozen acceptance fails.

Wait for approval before implementation.
