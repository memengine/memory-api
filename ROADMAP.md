# MemoryOS Roadmap

The roadmap is organized around governed context reliability, not the number of storage backends
or domain templates shipped.

## Current Foundation

- General extraction and retrieval engine.
- Source events, claim ledger, revisions, and memory version chains.
- Conflict resolution, clarification, and tenant-review routing.
- Temporal validity, lifecycle transitions, and historical read support.
- Provenance and evidence propagation.
- Durable duplicate-event handling and concurrent single-winner protection.
- Tenant, user, agent, consent, and category isolation.
- Transactional Qdrant outbox with PostgreSQL authority during index lag.
- Internal correctness, integration, provider, and scale benchmark tiers.
- EdTech structured overlay and domain-extension framework.

## Launch Track

1. Run reproducible external baselines, beginning with LongMemEval and then LoCoMo.
2. Complete controlled-beta operational gates: migrations, backup/restore, monitoring, alerting,
   bounded queue admission, rate limits, and rollback procedures.
3. Onboard a small number of multi-agent or long-lived AI products where governance failures are
   observable and consequential.
4. Publish benchmark methodology and limitations alongside results.

## Control-Plane Improvements

- Semantic claim identity and merge diagnostics that are easier for operators to inspect.
- Dependency-aware invalidation when a supporting premise is corrected or deleted.
- Human or supervisor review policies for sensitive memory transitions.
- Better audit tooling for tracing when and why agent context changed.
- Explicit policy configuration for source authority, retention, and audience.
- Historical semantic retrieval without weakening current-state isolation.

## Reliability and Capacity

- Address the known single-node MODERATE-load API/resource-contention limitation.
- Establish bounded capacity targets per deployment topology.
- Improve queue admission, backpressure, and tenant fairness.
- Add sustained recovery exercises for Redis, Qdrant, workers, and provider degradation.
- Produce supported on-premise and private-cloud deployment guidance.

## Ecosystem

- Stabilize Python and TypeScript SDK contracts.
- Add framework adapters only after the core API contract is stable.
- Expand domain overlays where typed state has a demonstrated product need.
- Provide export, portability, and operator tooling for governed context histories.

## Not a Goal

MemoryOS is not trying to replace every file, skill, note system, vector database, or agent
framework. It focuses on the governance boundary those tools do not automatically provide when
context changes, conflicts, crosses audiences, or must be audited.
