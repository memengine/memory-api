# Originating-agent deletion architecture inventory

Development baseline only; holdout is excluded.

## Supported lifecycle surfaces

- `GlobalAgent` is the Passport source/application identity. `Agent` and
  `Agent.memory_scope` belong to the separate tenant-local memory plane.
- No public, tenant, operator, service, or worker operation deletes/deactivates a global agent.
  Current production supports key revocation, grant revocation and verification updates only.
- Consequently, “agent deletion” currently means a direct database row deletion, not a governed
  product lifecycle operation.

## Database effects

- `PermissionGrant.agent_id` and `AgentApiKey.global_agent_id`: `ON DELETE CASCADE`.
- `UniversalMemory.source_agent_id`: `ON DELETE SET NULL`.
- `UniversalMemoryClaimRevision.source_agent_id`: `ON DELETE SET NULL`.
- `UniversalMemoryVersion.changed_by_agent_id`: `ON DELETE SET NULL`.
- Memory, claim, revision and version rows otherwise survive. Active/winner state is not
  recalculated when the source agent disappears.

The schema intentionally stopped cascading source-agent deletion to universal memories when
organisation sources were added. This preserves shared knowledge, but no explicit private/shared
ownership field replaced the old cascade behavior.

## Provenance and vectors

- Recent worker writes snapshot `source_agent_id` and durable event identity into memory metadata.
  Older/other write paths may not have that snapshot.
- API readback can return the metadata provenance snapshot, but relational source IDs become null.
- Universal Qdrant payloads retain `source_agent_id`. Database agent deletion creates no outbox
  operation, so vector payloads remain stale and vectors remain searchable by user/category.
- Internal provenance diagnostics explicitly classify Passport rows with null source-agent IDs as
  orphaned provenance.

## Privacy distinction

- Grant revocation removes access for one agent and invalidates permission cache; it does not
  delete shared memories.
- Universal-user privacy erasure deletes every vector for the UUI, then deletes the user and its
  memories/grants/claims by cascade.
- Source-agent deletion is neither of those operations and currently has no vector cleanup or
  retention policy.
