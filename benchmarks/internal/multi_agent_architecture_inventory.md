# Multi-agent isolation and coordination inventory

Development baseline scope only. Holdout is not used.

## Two independent memory planes

1. Tenant memory uses `Agent`, `Memory.agent_id`, tenant API authentication, `ProxyUser`,
   PostgreSQL, the `memories` Qdrant collection and `RetrieverService`. Supplying `agent_id`
   applies an exact agent filter; omitting it intentionally retrieves the tenant/user memory pool.
2. Passport memory uses `GlobalAgent`, `AgentApiKey`, `UniversalUser`, `PermissionGrant`,
   `UniversalMemory`, the universal claim ledger and the `universal_memories` collection.
   Grants are user/agent/category scoped. Memories are shared across authorized agents by
   category, regardless of which agent originally wrote them.

These IDs are not interchangeable: tenant `Agent` is owned through legacy `User`, while
`GlobalAgent` is owned by `Tenant`. Domain projection passes global-agent provenance into the
universal plane.

## Enforcement boundaries

- Tenant API middleware establishes `request.state.tenant_id`; proxy-user identity is hashed
  with tenant ID. PostgreSQL and Qdrant retrieval require tenant plus proxy-user.
- Tenant `agent_id` filtering exists in hot cache, Qdrant, PostgreSQL fallback and historical
  retrieval. `Agent.memory_scope` exists in schema but is not consulted by retrieval.
- Universal middleware resolves both agent API key and UUI token. Reads require an active,
  unexpired grant and use only its allowed categories. Writes additionally require
  `read_write`; the worker rechecks the grant before persistence.
- Universal Qdrant search filters user, category and archive state. PostgreSQL revalidates user
  and archive state. Source agent is provenance, not a read-isolation filter.
- Grant revocation invalidates the permission cache. User erasure removes the universal user's
  vectors and cascades PostgreSQL data.
- Universal writes create versions, claim revisions and transactional outbox rows. Claim
  identity/winner logic is handled by `UniversalClaimLedgerService`.

## Existing coverage and observed harness drift

Existing security/unit suites cover authentication, user isolation, category grants,
read-only writes, model constraints, universal claims and outbox writes. Initial run: 26 passed,
5 failed. Four failures are stale `_search_universal_memories` fakes returning two values after
the production contract changed to three. One expects a direct Qdrant write after production
moved to transactional outbox. These are harness failures, not baseline product failures.

## Known gaps to measure

- `Agent.memory_scope` has no evident enforcement in tenant retrieval.
- Universal retrieval responses do not expose source-agent/provenance even though PostgreSQL
  and Qdrant retain `source_agent_id`.
- Cross-agent concurrent updates and duplicate delivery have service-level coverage but no
  frozen full API/worker/readback regression.
- Tenant `Agent` and Passport `GlobalAgent` identity mapping remains an integration boundary.
