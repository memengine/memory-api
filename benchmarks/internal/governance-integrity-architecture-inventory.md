# Governance integrity architecture inventory

## Durable source and evidence

Tenant writes create `MemorySourceEvent` records with tenant/user/writer identity, logical
event ID, payload hash, observed time, scope, authority rules and evidence references.
Extraction builds a provenance snapshot and stores both `Memory.source_event_id` and the
snapshot in memory metadata. Claim revisions separately retain source-event, writer,
evidence, decision evidence, validity interval and processor/schema versions.

## Lifecycle propagation

Conflict updates link predecessor memories, archive prior winners, change claim revisions,
record versions and enqueue vector lifecycle payloads. Temporal transitions lock memory and
claim rows, change winner/revision state, record versions and use the transactional outbox.
Superseded vectors are retained with lifecycle metadata; privacy/hard deletion remains a
separate physical-delete operation. Agent retirement tombstones the source agent rather
than deleting provenance identity.

## Readback and governance surfaces

Current/historical retrieval returns source-event and provenance fields from PostgreSQL,
cache or Qdrant payloads. Memory APIs expose provenance; history and user export expose
version chains. Internal provenance-health/issues/version endpoints measure coverage and
backfill state. Raw extraction payload retention redacts messages while preserving durable
governance context.

## Frozen v2 benchmark decision

The existing 25-case lifecycle-provenance pack remains unchanged. The v2 extension adds 14
independently scoped scenarios for correction evidence, temporal expiration, retained-vector
metadata, agent retirement, writer attribution, API history/export, provenance health and a
real PostgreSQL Passport governance flow. Holdout is not read or referenced.

Known limitation before execution: several scenarios validate real components individually;
only the PostgreSQL broad-flow scenario crosses authorization, claims, retrieval and
provenance together. Qdrant live-state inspection and tenant hard-deletion completeness are
not yet represented by a single full-path scenario.
