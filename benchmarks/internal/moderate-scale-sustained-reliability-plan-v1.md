# Moderate Scale and Sustained Reliability Benchmark Plan v1

Status: design only. No load execution or production optimization is authorized by this document.

## 1. Purpose and boundaries

Measure performance and correctness of the complete MemoryOS backend under realistic moderate
traffic, then locate saturation and recovery boundaries before any production tuning.

- Dedicated disposable development/test infrastructure only.
- No production databases, customer data, credentials, tenants, collections, or queues.
- Frozen development scenarios provide traffic templates, but extraction holdout is never loaded.
- Public/marketing benchmarks remain separate.
- The consolidated FAST and INTEGRATION gates must pass before load starts.
- Load generation, telemetry collection, invariant audit, and cleanup are separate processes so a
  load-generator failure cannot be mistaken for a product failure.

## 2. Production-path inventory and likely bottlenecks

| Boundary | Current architecture | Load risk to measure |
|---|---|---|
| API | One Uvicorn process in local Compose | Event-loop saturation, auth/quota latency, request acceptance p99 |
| PostgreSQL | Async pool 20, overflow 30, 30s timeout per process | Pool wait, transaction duration, lock waits, connection exhaustion |
| Redis | Queue broker, result backend, plan/queue/cache state | Command latency, eviction, queue-depth drift, outage degradation |
| Celery | starter 3, growth 3, scale 4, background 2; prefetch 1 | Queue wait, worker saturation, unfairness, retry amplification |
| Provider extraction | Multi-provider LLM path with token/provider recording | Dominant completion latency, rate limits, nondeterminism, cost |
| Claims/conflicts | Claim-row `FOR UPDATE`; universal advisory transaction locks | Hot-claim serialization, deadlock/timeout risk, winner correctness |
| Outbox | 5s schedule, batch 100, Qdrant chunks 10 | Backlog age, eventual consistency, retry throughput |
| Qdrant | Async pool default 100; live upsert/search | Search/write p99, indexing lag, collection growth |
| Retrieval | Qdrant candidates plus PostgreSQL hydration/reranking | N+1/hydration cost, cache miss amplification, stale vector handling |
| Caches | L1 1s, hot 2s, count 30s, model 60s, Redis result 60s | Hit ratio, invalidation correctness, stampede behavior |
| Plan routing | Per-plan queue limits 10/50/200/500/1000 | Admission rejection, tenant fairness, cached-plan drift |
| Recovery | Task retry/dead letter, watchdog, outbox retries | Duplicate state, retry storms, recovery time and data loss |

The benchmark must report API/service saturation separately from provider latency. Provider-backed
writes cannot by themselves establish backend capacity.

## 3. Workload model

Use seeded, reproducible weighted selection. Content, users, agents, event IDs, categories, temporal
phrasing, and conflict slots vary within each class; requests are not identical repetitions.

| Traffic class | Share | Notes |
|---|---:|---|
| Current retrieval | 45% | 60% warm/cacheable, 40% varied cache-miss queries |
| Normal memory writes | 20% | Durable facts/preferences/goals/procedures; some multi-memory turns |
| Corrections/updates | 8% | Existing-memory explicit correction and version-chain transition |
| Conflicting writes | 5% | Same semantic slot, controlled authority/recency winner |
| Multi-agent writes/readback | 5% | Private/shared authorized flows across two or three agents |
| Duplicate delivery | 4% | Same `(tenant, service, event_id)` and identical payload, sequential/concurrent |
| Pending memories | 3% | Tentative preferences/goals that must remain pending |
| Historical retrieval | 5% | `as_of` current/previous/date queries over temporal chains |
| Feedback/readback/status | 5% | Job polling, API readback and retrieval feedback |

Tenant distribution: 70% ordinary tenants, 20% moderately active tenants, and 10% hot tenants.
At least one hot tenant targets the same claim slot to exercise lock contention, but no tenant may
consume more than 25% of total offered traffic. Use at least 200 users and 40 agents for moderate
stages. One percent of writes are deliberate same-event duplicates; another one percent use the
same text with distinct event IDs and must remain legitimate observations.

Two execution modes are required:

1. **Backend-capacity mode:** deterministic local extraction/embedding fixtures with realistic
   latency distributions. This is zero-cost and isolates API/database/queue/outbox/Qdrant limits.
2. **Provider-calibration mode:** a capped sample through the configured real production provider
   and model. It estimates true latency/tokens/cost but is not used to generate maximum throughput.

## 4. Staged workload

Concurrency means active virtual users with one in-flight operation each. Arrival rate is capped;
the generator must not create an unbounded local queue.

| Stage | Virtual users | Target rate | Duration | Purpose |
|---|---:|---:|---:|---|
| Preflight | 1–2 | ≤1 request/s | 3 min | Validate fixtures, telemetry and cleanup identity |
| LOW | 5 | 2 requests/s | 10 min | Stable latency/correctness reference |
| MODERATE | 20 | 8 requests/s | 20 min | Expected near-term development traffic |
| HIGHER controlled | 40 | 15 requests/s | 15 min | Find approaching saturation without stress-to-failure |
| Recovery cooldown | 5 | 2 requests/s | 10 min | Confirm queues/outbox converge after higher stage |
| SUSTAINED moderate | 20 | 8 requests/s | 2 hours | Detect leaks, backlog growth and retry accumulation |

Initial safety ceiling: 40 VUs, 15 requests/s, 50 in-flight requests, 2-hour sustained window,
and 100,000 total HTTP requests per run. Do not automatically increase a later stage if the prior
stage breaches a stop condition. After one clean baseline, an approved follow-up may test 60 VUs
or 20 requests/s, never both in the same experiment.

Because production plan admission limits are intentional product behavior, load tenants use
explicit benchmark plans and separate queues. Queue-full responses are measured, not bypassed.

## 5. Infrastructure and observability

Minimum dedicated stack:

- Load generator: k6, extended from `scripts/staging_load_k6.js`.
- FastAPI: one production-shaped instance initially; resource limits pinned and recorded.
- PostgreSQL 16 with `pg_stat_statements`; dedicated database and role.
- Redis 7 with persistence policy and memory cap recorded.
- Celery workers for starter/free, growth, scale/enterprise, background, plus beat.
- Qdrant in a dedicated collection namespace per run.
- Provider proxy/stub for deterministic capacity mode; configured real provider for capped mode.
- Metrics: Prometheus plus process/container metrics where available; structured request logs are a
  fallback, not the primary percentile source.

Required instrumentation snapshots:

- API request count/latency/status by route; event-loop and CPU/memory utilization.
- SQLAlchemy pool checked-out/overflow/wait/timeout; PostgreSQL transaction/query/lock wait,
  deadlocks, connections, database size and table/index growth.
- Redis command latency, memory, connections, errors, evictions and queue lengths.
- Celery active/reserved/completed/failed/retried counts, runtime, worker utilization and queue age.
- Extraction-job timestamps: queued, processing, completed/dead.
- Provider calls, input/output/total tokens, model, retries and estimated cost.
- Outbox pending/processing/failed counts, oldest pending age and creation-to-done latency.
- Qdrant upsert/search latency, point count, collection size and errors.
- Retrieval cache hit/miss, Qdrant search, PostgreSQL hydration and total context-building latency.

All metrics include run ID, stage, traffic class and synthetic tenant cohort. Never label by raw
external user ID or memory content.

## 6. Performance measurements and initial thresholds

Thresholds are deliberately moderate launch-readiness floors, not marketing claims. Compare every
stage with LOW as well as absolute limits.

| Metric | LOW/MODERATE pass | HIGHER/SUSTAINED pass |
|---|---:|---:|
| API add acknowledgement p95 / p99 | <500ms / <1,000ms | <750ms / <1,500ms |
| Cached retrieval p95 / p99 | <300ms / <600ms | <450ms / <900ms |
| Uncached retrieval p95 / p99 | <750ms / <1,500ms | <1,000ms / <2,000ms |
| Historical retrieval p95 / p99 | <1,000ms / <2,000ms | <1,500ms / <3,000ms |
| Backend-capacity job completion p95 | <10s | <20s |
| Provider-backed job completion | Report distribution; p95 < provider timeout plus 15s |
| Queue wait p95 | <5s | <15s; sustained slope must return to zero |
| PostgreSQL transaction p95 / p99 | <250ms / <750ms | <500ms / <1,500ms |
| Claim-lock wait p99 | <1,000ms | <2,000ms; zero lock timeouts/deadlocks |
| Qdrant search p95 / write p95 | <250ms / <500ms | <400ms / <750ms |
| Outbox creation-to-done p95 | <15s | <30s; oldest age <60s after cooldown |
| HTTP error rate | <0.5% | <1%; excludes expected queue-full/validation responses |
| Unexpected retry rate | <1% | <2%; no upward trend during sustained window |
| Throughput | ≥95% target arrival rate | ≥90%, with no unbounded backlog |

Sustained stability requirements: API/worker memory growth after warm-up <20%; PostgreSQL and Redis
connections remain within configured limits; no monotonic queue/outbox backlog; no dead jobs;
provider rate-limit responses <2% and recover without manual intervention.

## 7. Correctness audit under load

Correctness is evaluated from an immutable expected-state ledger generated before traffic. Audit
100% of security/idempotency/conflict scenarios and a reproducible sample of ordinary retrievals.

Hard gates:

- Cross-tenant leakage: 0.
- Cross-user leakage: 0.
- Unauthorized cross-agent leakage: 0.
- Multiple activated revisions for a single-winner claim: 0.
- Duplicate durable writes/revisions/outbox points for identical logical events: 0.
- Provenance/source-event/evidence preservation: 100%.
- Version-chain integrity: 100%.
- Revocation enforcement: 100%.
- Conflict winner correctness: 100% for deterministic authority scenarios.
- PostgreSQL/Qdrant eventual consistency: 100% by the post-stage convergence deadline.
- Superseded/current temporal leakage: 0 for audited current/historical queries.
- PostgreSQL authority during Qdrant lag: 100%; stale/unauthorized vector candidates must not
  survive hydration filters.

Soft retrieval gates for the seeded relevance sample: Recall@K ≥ accepted retrieval baseline,
MRR/nDCG regression ≤2 percentage points, provenance 100%, and empty-result accuracy no lower than
the accepted baseline.

Any security leakage, duplicate winner, provenance loss, corruption, deadlock, or unrecoverable
data mismatch aborts the run regardless of latency performance.

## 8. Sustained failure/recovery schedule

Faults run only after a clean no-fault MODERATE stage. Inject one fault at a time during the
2-hour sustained stage, with at least 20 minutes of normal traffic between faults.

| Offset | Fault | Injection window | Recovery requirement |
|---:|---|---:|---|
| 20 min | Restart one extraction worker | one SIGTERM; separate approved SIGKILL case | No loss/duplicates; stale job recovered within watchdog SLA |
| 45 min | Duplicate selected task/event deliveries | 5 min | One logical write per event; distinct events unaffected |
| 70 min | Qdrant unavailable | 2 min | PostgreSQL writes continue, retrieval filters safely, outbox converges ≤5 min after restore |
| 95 min | Delay background outbox worker | 5 min | Backlog bounded; no stale-state leakage; full convergence ≤5 min |
| 115 min | Redis interruption | ≤30s, local dedicated stack only | Defined degraded behavior, no cross-tenant/cache leakage, queues recover |

Transient provider failure is a separate optional run using a controlled proxy that returns
retryable failures for 5% of calls for five minutes. Never induce failures against the real
provider. Require bounded retries, accurate terminal status, no partial commits and no retry storm.

## 9. Provider cost model and caps

Before any paid run, compute the estimate from the configured model's then-current price:

`estimated_cost = calls × (mean_input_tokens × input_price + mean_output_tokens × output_price) / 1,000,000`

Use the latest accepted development provider artifact to seed mean token counts. Add a 25% safety
margin for retries and variance. The orchestrator must print estimated calls/tokens/cost and require
confirmation before dispatch.

Initial paid calibration caps:

- LOW: at most 100 provider-backed extraction jobs.
- MODERATE sample: at most 300 jobs.
- HIGHER and sustained stages: deterministic provider proxy by default; no paid calls.
- Per-run hard budget: USD 5 and 500,000 total provider tokens, whichever is reached first.
- Abort at 80% of either cap; require new approval to continue.

Therefore expected paid cost is **$0 for the capacity/sustained run** and **no more than $5 for the
separately approved provider calibration**. Exact expected cost is calculated immediately before
execution because provider/model pricing is not frozen in this plan.

## 10. Resource limits and stop conditions

Pin and record CPU/memory for each service. Initial local/cloud envelope:

- API: 2 vCPU, 2 GiB.
- PostgreSQL: 2 vCPU, 4 GiB, connection ceiling compatible with all process pools.
- Redis: 1 vCPU, 1 GiB, no eviction for correctness run.
- Qdrant: 2 vCPU, 4 GiB.
- Celery total: 4 vCPU, 4 GiB across workers.
- Load generator: separate 2 vCPU, 2 GiB host/container.

Immediate stop conditions:

- Any isolation/security violation, data corruption or multiple active winners.
- Error rate >5% for two consecutive minutes.
- PostgreSQL connections >90% ceiling, pool timeouts, deadlocks, or disk >80%.
- Redis/Qdrant/API/worker memory >85% limit for two minutes.
- Queue depth >80% plan limit or oldest job >2× expected completion SLA.
- Outbox oldest pending age >5 minutes outside an intentional fault window.
- Provider spend/token cap reaches 80%.
- Cleanup identity or telemetry becomes unreliable.

## 11. Correctness gates before and after load

Before every run:

1. Consolidated FAST tier — must pass 100%.
2. Consolidated INTEGRATION tier — must pass with zero product or harness failures.
3. Retrieval live-vector smoke on the dedicated stack when provider/embedding is enabled.
4. Service health, migrations, empty benchmark namespace and telemetry preflight.

After every stage:

1. Run the load invariant auditor before cleanup.
2. Re-run FAST.
3. Re-run affected integration suites: fault-injection reliability, integration reliability,
   governance integrity, lifecycle activation and temporal memory.
4. Re-run retrieval correctness/live-vector and historical retrieval only when Qdrant or temporal
   traffic participated.
5. Compare all outputs with the accepted pre-load aggregate; do not accept a performance pass if
   correctness gates regress.

Provider extraction evaluation is not a routine post-load gate; use it only in the separately
approved provider-calibration run. Holdout is never a pre/post scale gate.

## 12. Results and analysis

Each run emits one immutable directory containing:

- workload seed, mix, stage schedule, commit/version and service resource limits;
- sanitized configuration fingerprints and provider/model names, never secret values;
- per-stage k6 summary and raw time-series references;
- API/job/DB/Redis/Celery/outbox/Qdrant/provider metrics;
- correctness audit with entity IDs and boundary-localized failures;
- resource and storage growth snapshots;
- fault timeline, detection time, recovery time and convergence time;
- provider calls/tokens/cost;
- pre/post consolidated gate artifacts;
- machine-readable `aggregate.json` and concise `aggregate.md`.

Product failures, harness/configuration errors, expected admission responses and intentional fault
errors are separate classifications. Stage comparison uses LOW and the previous accepted run.

## 13. Cleanup strategy

All fixtures carry a unique run ID in tenant, source service/event, metadata, queue markers and
Qdrant collection namespace. Cleanup is idempotent and runs only after the invariant audit.

1. Stop load and fault injectors; restore all paused/restarted services.
2. Drain or explicitly account for benchmark queues, retries and outbox rows.
3. Delete the run-scoped Qdrant collection/points.
4. Delete run-scoped PostgreSQL tenants/users/agents/memories/source events/claims/revisions/jobs,
   relying on verified governed deletion order rather than broad table truncation.
5. Delete run-scoped Redis cache, idempotency, queue-depth and result keys.
6. Verify zero rows/points/keys/jobs remain for the run ID and all services are healthy.
7. Drop the disposable database/stack after artifacts are copied.

Cleanup never targets a workspace-wide, shared, production, or ambiguously resolved resource.

## 14. Implementation slices after approval

1. Telemetry contract and read-only collectors.
2. Deterministic mixed-traffic fixture generator and expected-state ledger.
3. k6 staged workload plus bounded job poller.
4. Correctness invariant auditor and run-scoped cleanup tool.
5. LOW baseline only; review results and thresholds.
6. MODERATE and HIGHER controlled stages after LOW approval.
7. Sustained no-fault run.
8. One-at-a-time recovery experiments.
9. Separately approved provider-calibration sample.

No production optimization should begin until a repeatable baseline identifies a confirmed
bottleneck and an isolated change is approved.
