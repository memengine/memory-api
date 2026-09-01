# Multi-agent isolation and coordination baseline v1

## Result

Frozen development-only live baseline: **11/12 passed (91.67%)**. Production behavior was not
changed and holdout was not accessed.

| Metric | Result |
|---|---:|
| Cross-tenant leakage | 0 |
| Cross-user leakage | 0 |
| Tenant agent-filter accuracy | 100% |
| Expected shared-memory visibility | 100% |
| Category grant / revocation / write authorization | 100% |
| Source-agent preservation in storage | 100% |
| Source-agent provenance in universal API readback | 0% |
| Mean live API latency | 1060.64 ms |

The run exercised real PostgreSQL, Qdrant, the configured embedding provider, tenant API auth,
Passport agent/UUI auth and API retrieval. All disposable PostgreSQL and Qdrant fixtures were
cleaned afterward.

## Confirmed product failure

`UniversalMemory.source_agent_id` and metadata provenance survive PostgreSQL/Qdrant storage,
but `/v1/universal/memories/retrieve` maps results into a response that omits both source agent
and provenance. Authorized agents receive correct memory content but cannot attribute which
agent produced it. The failing boundary is **universal retrieval response mapping**, not storage,
Qdrant filtering, permission resolution or source propagation.

## Harness drift, separate from product correctness

The pre-existing cross-agent security suite initially produced five failures: four stale fakes
return the old two-value universal-search contract, and one expects direct Qdrant writes after
the transactional-outbox migration. Stable relevant suites plus the frozen dataset contract
pass **22/22**. The stale test file was not used to lower product metrics and production logic
was not changed.

## Coverage boundary

This baseline establishes isolation, authorized sharing, category grants, revocation, read-only
enforcement and source attribution. Cross-agent conflicting claims, duplicate delivery,
concurrent updates and agent deletion are the next coordination slice; they are not claimed as
passing here.

## One isolated proposed improvement

Expose stored `source_agent_id` and provenance in universal retrieval results without changing
permissions, ranking, sharing semantics, conflict logic or stored memory. Acceptance: API
source-agent/provenance readback 100%, all isolation/grant metrics remain 100%, zero leakage,
and existing retrieval/security suites remain green after repairing their stale fakes.
