# MemoryOS Architecture

## System Model

MemoryOS is a governed state and context service for AI applications. A stored memory is not just
text plus an embedding. It participates in a lifecycle that includes source attribution, claim
identity, revisions, conflict decisions, temporal validity, access policy, indexing, and retrieval.

The architecture separates four responsibilities:

1. **Ingestion** turns conversations or events into candidate state.
2. **Governance** decides what the system may accept and which revision is current.
3. **Delivery** retrieves relevant, valid, authorized context.
4. **Operations** makes asynchronous processing observable, retryable, and idempotent.

## Authoritative and Derived State

- **PostgreSQL** is authoritative for tenants, users, agents, source events, memories, claims,
  revisions, validity ranges, grants, jobs, audit history, and the transactional outbox.
- **Qdrant** is a derived semantic index. Outbox processing synchronizes it with PostgreSQL.
- **Redis** provides cache, rate-limit, coordination, hot-tier, and circuit state. Redis loss must
  not redefine durable memory truth.
- **Celery** executes extraction, lifecycle, vector synchronization, retry, and maintenance work.

This boundary allows PostgreSQL readback to remain correct during temporary vector-index lag.

## Write Path

```text
API/event
  -> authentication and tenant/user/agent resolution
  -> quality, quota, and idempotency checks
  -> durable source event and extraction job
  -> Celery extraction
  -> candidate memories and evidence
  -> claim reconciliation and conflict policy
  -> memory/version/claim revision transition
  -> transactional outbox
  -> Qdrant synchronization
```

Key guarantees exercised by the regression suites include scoped duplicate-event handling,
single-winner claim revisions, provenance preservation, version-chain integrity, and eventual
outbox convergence.

## Correction and Conflict Model

A correction is not treated as an unrelated new note. The engine reconciles the candidate with an
underlying claim, records a new revision, applies the resolution decision, and updates active or
superseded state. Claim evidence and source-event identity remain attached to the revision chain.

Depending on scope and ambiguity, a conflict can be resolved automatically, queued for user
clarification, or routed for tenant review. Domain routers specialize classification while the
generic policy remains the fallback.

Database constraints and row locking enforce at most one activated revision for claims that expect
one winner, including concurrent updates.

## Temporal and Lifecycle Model

Memories and claim revisions may carry `effective_from` and `effective_until`. Current retrieval
excludes state that is not yet effective or has expired. Historical retrieval can evaluate state as
of a supplied timestamp. Supersession, expiration, archival, decay, and future activation are
separate transitions rather than synonyms.

Lifecycle tasks move state through these transitions while retaining PostgreSQL history and
coordinating the derived vector representation through the outbox.

## Read Path

```text
retrieve request
  -> authorization and scope resolution
  -> current or as-of temporal boundary
  -> cache/hot-tier lookup where valid
  -> Qdrant candidate search for indexed paths
  -> PostgreSQL hydration and authoritative filtering
  -> semantic, importance, and recency ranking
  -> provenance-aware response and prompt context
```

Filtering covers tenant, user, agent, category, permission, lifecycle, temporal validity, and
superseded state. A minimum semantic floor prevents irrelevant filler for current vector retrieval.

## Cross-Agent Context

Tenant memories and universal cross-agent memories are separate governed paths. Universal access
requires user identity linkage, agent identity, and an active permission grant. Category and access
mode restrictions are applied before retrieval or writes. Agent retirement is distinct from user
privacy deletion: access may end while durable provenance remains attributable through retained
source snapshots.

## Important Services

| Area | Primary implementation |
| --- | --- |
| Extraction | `api/services/extraction_service.py` |
| Claims and revisions | `api/services/claim_ledger_service.py` |
| Conflict policy | `api/services/conflict_resolver.py` |
| Temporal validity | `api/services/temporal_validity.py` |
| Lifecycle | `api/services/lifecycle_manager.py` |
| Retrieval | `api/services/retriever.py` |
| Context formatting | `api/services/context_builder.py` |
| Vector outbox | `api/services/vector_outbox.py` |
| Cross-agent retirement | `api/services/global_agent_retirement_service.py` |
| Provider fallback | `api/services/llm_service.py` |

## Failure Boundaries

- Extraction provider failures are separated from job and persistence failures.
- Durable event identity makes worker redelivery converge rather than duplicate state.
- PostgreSQL transactions protect memory, claim, and outbox transitions.
- Failed asynchronous work is retryable and may move to dead-letter handling.
- Cache and Qdrant degradation must not authorize broader access or override PostgreSQL truth.
- Benchmark-only provider and telemetry modes require explicit isolated-environment markers.

## Benchmark Boundaries

`benchmarks/internal` contains private development correctness and regression suites. Internal
holdout data is sealed from routine runs. `benchmarks/public` contains adapters for established
external benchmarks and must not reuse internal cases or tune against public test labels.
