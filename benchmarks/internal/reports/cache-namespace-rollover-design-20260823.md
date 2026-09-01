# Cache namespace rollover and offline purge design

## Status and scope

Design only. This proposal makes no production, configuration, test, or benchmark-runtime
change. It follows the failed live per-user registry experiment and keeps the accepted
PostgreSQL session reuse, Redis timeout/deadline defaults, enum correction, and TCP
preflight behavior unchanged.

The objective is to remove wildcard Redis scans from live memory request paths without
weakening mutation correctness, privacy deletion, tenant isolation, or rolling-deployment
safety.

## Current architecture and confirmed problem

Dynamic retrieval and hot-tier entries use identity-prefixed keys. A memory mutation or
hard deletion calls the same cache invalidation boundary, which scans the dynamic prefixes
and deletes matches. The frozen MODERATE run showed that these scans dominate Redis command
timeouts. The attempted live registry replacement did not help: concurrent first requests
stampeded its lazy legacy migration, and transactional registry writes increased Redis
connection pressure.

Current responsibilities are therefore coupled:

1. Routine mutation needs immediate logical invalidation.
2. Privacy/hard deletion needs eventual, verified physical erasure.
3. Namespace migration needs removal of obsolete derived cache entries.

Only the first belongs on the synchronous request path.

## Proposed architecture

### 1. Versioned cache namespace

Introduce one configured dynamic-cache namespace version, for example
`MEMORY_CACHE_NAMESPACE=v2`. Retrieval and hot-tier keys include that version. Reads and
writes use only the active version and never fall back to an older namespace.

Cache values are derived state, so old values are not copied into the new namespace. A
rollover invalidates them by making them unreachable; normal traffic warms the new namespace
on demand.

### 2. Deterministic per-identity generation

Within the active namespace, keep an integer generation per correctly scoped identity
(tenant plus proxy/user identity). Dynamic keys include that generation. Routine mutation
increments the generation atomically, making all older entries unreachable in bounded time
without `SCAN` or a per-key registry.

Conceptual keys:

- generation: `memory-cache:v2:g:{tenant}:{identity}`
- retrieval: `memory-cache:v2:retrieve:{tenant}:{identity}:g{n}:{query_hash}`
- hot memory: `memory-cache:v2:hot:{tenant}:{identity}:g{n}:{memory_id}`

The exact encoding must reuse existing canonical tenant/user identifiers and hashing rules;
it must not expose external user identifiers. Fixed keys that can be addressed directly may
still be deleted directly.

Generation acquisition adds Redis work and must be measured before activation. No local
generation cache or pipelining optimization is part of the first experiment.

### 3. Separate routine invalidation from governed purge

Routine invalidation is synchronous and O(1): increment the active generation and clear any
directly addressable fixed keys. Previous-generation values remain unreadable and expire by
TTL.

Privacy/hard deletion invokes a durable purge workflow after authoritative database and
vector deletion is recorded. That workflow scans only in a dedicated background execution
context, across every supported namespace version, deletes all keys for the exact
tenant/identity, records counts and completion evidence, and retries idempotently. Privacy
completion must not be reported until the governed purge reaches its defined terminal state.

Namespace retirement uses the same offline mechanism but operates on an entire retired
version in rate-limited batches. It must use a separate Redis client/pool and queue from API
request traffic so cleanup cannot consume request-path connection capacity.

### 4. Deployment protocol

Use a two-release capability/activation protocol:

1. **Capability release:** deploy binaries that understand both the current and proposed
   namespace configurations, while every API and worker continues using the current active
   namespace. No key behavior changes.
2. **Readiness verification:** confirm all API/worker instances advertise support for the
   target namespace and the offline purge worker is available.
3. **Coordinated activation:** change the centrally managed active namespace for all
   instances. Reads and writes immediately use only the new namespace.
4. **Safety guard:** readiness fails when an instance's configured active namespace differs
   from the deployment control value. Load testing and production activation refuse to run
   while active namespaces are mixed.
5. **Offline retirement:** after the fleet is consistent and correctness gates pass, purge
   the retired namespace in controlled batches.

Mixed binary versions are supported during the capability release because all instances use
the same old namespace. Mixed active namespace versions are not supported and are blocked;
allowing them would let old instances serve stale cache values.

### 5. Rollback

Do not restore reads to a previously retired namespace after writes have occurred in the new
one. It may contain stale derived state. A safe rollback either:

- keeps the new namespace while rolling back to a binary already proven compatible with it;
  or
- rolls forward to a fresh namespace after quiescing traffic and verifying fleet agreement.

Rollback to an old namespace is allowed only after its full purge and an explicit empty-state
verification. This rule must be automated in the activation command.

## Deletion and governance semantics

- **Routine update/correction/supersession:** generation bump; old cache values become
  unreachable. PostgreSQL remains authoritative and Qdrant/outbox semantics are unchanged.
- **Memory hard deletion:** database/vector deletion plus durable identity-scoped cache purge;
  completion evidence includes namespace versions and deleted-key counts.
- **Proxy-user deletion:** preserve the existing database and vector cleanup, then purge all
  cache namespaces for that tenant/proxy identity.
- **User/UUI deletion:** retain its distinct ownership and permission-cache cleanup paths;
  the namespace mechanism must not broaden one identity's deletion to another cache domain.
- **Namespace retirement:** infrastructure cleanup only; never interpreted as a user privacy
  deletion record.

All scans are forbidden from API, retrieval, extraction, mutation, and conflict request
paths. They are permitted only in the governed purge/retirement worker with rate and batch
caps.

## Required implementation slices

Each slice should be reviewed separately:

1. Key contract and configuration: versioned builders, scoped generation key, fleet
   capability/readiness reporting, and holdout-independent tests.
2. Routine invalidation experiment: generation-based reads/writes and O(1) invalidation in
   the disposable benchmark stack only.
3. Durable privacy purge: dedicated task, idempotent progress record, bounded offline scan,
   retries, audit evidence, and isolation tests.
4. Deployment tooling: capability check, coordinated activation guard, namespace retirement,
   and rollback protection.
5. Frozen LOW then MODERATE validation before any production activation.

No slice should change Redis retry semantics, circuit behavior, cache value semantics,
extraction, claims, retrieval ranking, Qdrant behavior, or authoritative deletion semantics.

## Verification matrix

### Deterministic and integration checks

- Key isolation across tenant, user/proxy user, namespace, and generation.
- Mutation makes all prior-generation retrieval and hot-tier entries unreadable.
- Concurrent invalidations cannot reactivate an older generation.
- Seeded legacy keys are never read after namespace activation.
- Capability rollout permits mixed binaries while the active namespace remains identical.
- Readiness rejects mixed active namespace configuration.
- Privacy purge removes matching keys from all supported versions and leaves unrelated
  tenant/user keys untouched.
- Purge is idempotent and resumes after partial Redis/task failure.
- Hard deletion does not report successful cache erasure before purge completion.
- Namespace retirement is bounded, observable, and isolated from the API Redis pool.
- Rollback drill cannot reactivate stale keys.

Run the existing privacy, retrieval, mutation, conflict, lifecycle, coordination, FAST, and
required INTEGRATION regression gates before load testing.

### Load experiment acceptance

Use the frozen disposable workload and compare with the accepted reference; do not tune the
workload or thresholds around this design.

- Request-path Redis `SCAN` count: **0**.
- Stale cache leakage after mutation, correction, supersession, and deletion: **0**.
- Privacy purge coverage across supported namespaces: **100%**.
- Cross-tenant/user/agent leakage: **0**.
- Durable correctness, provenance, winner, version, idempotency, and outbox invariants:
  **100%**.
- Redis command timeouts and connection/pool failures: materially reduced; target **at least
  80%** versus the frozen failing MODERATE reference.
- API error rate: **at most 0.5%**.
- Redis-related HTTP 500s: **0**.
- Unfinished jobs after drain: **0**.
- PostgreSQL connection exhaustion and enum/database telemetry errors: **0**.
- Cache effectiveness must be reported; a result that merely disables useful caching does
  not pass.
- Add, retrieval, queue-wait, and job p95/p99 must not materially regress from the accepted
  pre-saturation baseline.

## Required telemetry and artifacts

Record active namespace, supported namespace versions, generation increments, cache
hit/miss rate, request-path scan count, purge queue depth, keys scanned/deleted, purge retry
and failure counts, oldest unfinished purge age, Redis pool/connection latency, command
timeouts by command, and API/job latency percentiles.

Every activation and retirement produces a machine-readable artifact containing fleet
capability results, old/new namespace, timestamps, operator identity, purge totals,
correctness-gate results, and rollback eligibility.

## Safety and cleanup

- Develop and load-test only in the disposable benchmark stack with its marker and endpoint
  isolation checks.
- Do not access holdout or paid providers.
- Cap purge batch size, command rate, concurrency, and maximum runtime.
- Refuse wildcard purge without an exact known namespace prefix and environment marker.
- Verify benchmark containers, volumes, cache namespaces, database fixtures, and Qdrant
  collections are removed after testing.

## Decision

The next approved experiment should implement only slices 1 and 2 in the disposable
benchmark environment: versioned key contracts plus deterministic generation invalidation.
It should not yet implement the governed privacy purge or activate production behavior.
This isolates whether request-path scans can be eliminated without repeating the failed
registry migration and connection-pressure behavior.
