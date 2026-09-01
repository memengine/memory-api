# Entity-Preserving Episodic Memory Plane — Design v1

Status: design only; implementation requires separate approval

## Decision this document supports

MemoryOS currently provides governed, durable semantic memory: reusable facts,
preferences, goals, procedures, relationships, and expertise with provenance,
conflict handling, lifecycle state, and tenant/agent controls.

The first LoCoMo smoke run exposed a different requirement: exact conversational
episodes involving multiple named participants. The current pipeline cannot retain
that information faithfully because it reduces messages to `user`, `assistant`, or
`system`, stores only a conversation row with a message count, and deliberately
extracts durable facts instead of retaining every event.

The proposed episodic plane complements the durable plane. It does not replace the
claim ledger, semantic memories, conflict resolution, or current retrieval path.

## Goals

1. Preserve who said what, when, in which session, and from which source.
2. Retrieve relevant historical episodes without rewriting them as generic user
   facts.
3. Link derived durable memories to exact supporting turns.
4. Apply the existing MemoryOS governance principles to episodic data: tenant/user/
   agent isolation, provenance, revocation, retention, deletion, and auditability.
5. Introduce the capability in shadow mode before it can affect agent context.

## Non-goals

- Tuning specifically for LoCoMo questions or evidence strings.
- Storing every conversation forever by default.
- Treating arbitrary speaker labels as verified real-world identities.
- Replacing PostgreSQL authority with Qdrant.
- Changing current extraction, importance, conflict, or retrieval behavior in the
  first implementation slice.
- Claiming an official public benchmark score from the current adapter.

## Current architecture boundary

The existing `Conversation` row stores user, optional agent, message count,
processing status, and creation time. Raw messages live in the extraction job
payload and are governed by payload retention. A `MemorySourceEvent` preserves the
source envelope and session-level evidence, while each durable `Memory` points to a
source conversation and optional source event.

This is sufficient for durable-memory lineage but not for conversational recall:

- individual turns are not durable first-class records;
- speaker identity is reduced to transport role;
- a memory has no first-class semantic subject;
- evidence currently identifies a source event/session rather than exact turns;
- Qdrant contains derived memories, not the complete governed episode.

## Proposed logical model

Names are illustrative and must be reviewed against existing naming conventions
before a migration is written.

### `episodic_sessions`

- `id`
- `tenant_id`
- `proxy_user_id`
- `agent_id` (nullable)
- `source_event_id` (nullable but required for registered service ingestion)
- `external_session_id`
- `observed_from`, `observed_until`
- `visibility_scope` / policy snapshot
- `retention_class`, `expires_at`
- `status`: active, revoked, expired, privacy_deleted
- `content_hash`
- timestamps

Durable idempotency boundary: tenant + source service + source event/session ID.

### `episodic_participants`

- `id`
- `session_id`
- `source_participant_key`
- `display_label` (unverified metadata)
- `participant_kind`: focal_user, agent, external_person, service, unknown
- `linked_proxy_user_id` or `linked_agent_id` only when verified
- `identity_confidence` and verification source

Participant keys are source/session scoped by default. The system must not silently
merge two people because they share a display name.

### `episodic_turns`

- `id`
- `session_id`
- `sequence_number`
- `source_turn_id`
- `participant_id`
- transport `role`
- immutable original content or governed encrypted content reference
- `observed_at`
- `content_hash`
- `status`: active, revoked, expired, privacy_deleted
- source metadata required for multimodal references, without downloading remote
  media by default

Unique boundary: session + source turn ID, with a sequence fallback when the source
does not provide turn IDs.

### `episodic_chunks`

Derived, rebuildable retrieval units spanning one or more contiguous turns:

- `id`, `session_id`
- first/last turn and sequence boundaries
- speaker-preserving rendered text
- embedding model/collection identity
- lifecycle status and policy snapshot
- deterministic derivation version and content hash

Chunking must not erase names, speaker keys, timestamps, or source turn IDs.

### `memory_evidence_links`

Many-to-many links from a durable memory/claim revision to exact episodic turns:

- memory or claim-revision ID
- episodic turn ID
- attribution method and confidence
- derivation version

These links supplement immutable provenance snapshots. They must never permit an
attribution worker to rewrite a memory.

## Ingestion contract

Add a versioned episodic input contract instead of overloading role semantics:

```json
{
  "session_id": "source-session-123",
  "participants": [
    {"key": "p1", "label": "Tim", "kind": "focal_user"},
    {"key": "p2", "label": "John", "kind": "external_person"}
  ],
  "turns": [
    {
      "turn_id": "D6:3",
      "participant_key": "p2",
      "role": "assistant",
      "content": "I was in Chicago...",
      "observed_at": "2023-07-..."
    }
  ]
}
```

`role` remains transport metadata; `participant_key` identifies the speaker. The
API must reject unknown participant keys, duplicate turn IDs with different
payloads, naive timestamps, and source-event payload mismatches.

The existing `/v1/memories/add` behavior remains unchanged. Episodic ingestion
should initially be a separate internal service boundary or a new explicitly
versioned endpoint so existing SDK clients cannot activate it accidentally.

## Retrieval design

### Episodic retrieval

1. Embed the query using the active versioned embedding model.
2. Search speaker-preserving chunks in Qdrant with mandatory tenant, proxy-user,
   agent/audience, lifecycle, and time filters.
3. Hydrate candidate sessions/turns from PostgreSQL.
4. Treat PostgreSQL status and policy as authoritative during Qdrant lag.
5. Return the relevant excerpt with exact turn IDs, participant identity, event
   time, source event, and provenance.

### Combined context

Durable and episodic retrieval remain separate result sets until a deterministic
fusion contract is evaluated. Initial shadow telemetry should compare:

- durable only;
- episodic only;
- combined candidates before context rendering.

No episodic result enters agent context during shadow mode. A later fusion policy
must prevent recent episodic chatter from displacing high-value durable memory and
must avoid treating an unverified third-party statement as a user fact.

## Governance and lifecycle

- Tenant/user/agent filters are mandatory at both vector search and PostgreSQL
  hydration boundaries.
- Sharing a derived durable memory does not automatically share its source episode.
- Source-agent deletion, grant revocation, and privacy deletion use explicit policy;
  they are not inferred from Qdrant state.
- Expiry or privacy deletion tombstones the PostgreSQL row and drives an idempotent
  outbox deletion from Qdrant.
- Retention defaults should be shorter than durable-memory retention and configurable
  by tenant/purpose.
- Exact original content should be encrypted or referenced through an approved
  content store if database-level encryption is not already available.
- Audit readback records which episodic turns entered a generated context.
- A deleted/revoked turn invalidates or flags derived evidence links; it does not
  silently rewrite claim history.

## Cost and storage controls

- Explicit tenant opt-in and retention class.
- Maximum turns, bytes, participants, session duration, and chunk count per event.
- Batch embeddings per session where supported.
- Content-hash idempotency prevents duplicate embeddings and vectors.
- Expiry cleanup covers PostgreSQL, outbox, Qdrant, caches, and content storage.
- Per-tenant ingestion, storage, embedding, and retrieval telemetry.
- No real provider calls in deterministic infrastructure/load benchmarks.

## Phased implementation and gates

### Phase 0 — contract and threat model

Deliver schema/API design, data classification, retention policy, authorization
matrix, and migration/rollback plan. No runtime activation.

Gate: security/privacy review agrees on third-party conversational data handling.

### Phase 1 — shadow persistence

Persist entity-preserving sessions/turns and exact provenance from development
traffic. Do not embed, retrieve, or alter durable extraction.

Gates:

- participant/turn preservation: 100%
- idempotent duplicate delivery: 100%
- cross-tenant/user/agent leakage: 0
- durable extraction outputs unchanged: 100%
- deletion/revocation database behavior: 100%

### Phase 2 — shadow indexing and episodic retrieval

Add transactional outbox operations and a separate Qdrant collection. Run
retrieval in shadow only.

Gates:

- PostgreSQL/Qdrant convergence: 100%
- expired/revoked leakage: 0
- exact turn provenance on results: 100%
- speaker identity accuracy: 100%
- no impact on active durable retrieval latency or results

### Phase 3 — frozen external diagnostic

Rebuild the LoCoMo adapter against the new neutral participant/session contract.
Ingestion must remain independent of questions, answers, categories, and evidence.
Run the existing frozen pilot before expanding it.

Measure answer correctness separately from episode retrieval recall, speaker
correctness, temporal correctness, evidence recall, latency, tokens, and cost.

### Phase 4 — controlled product activation

Offer episodic retrieval only to opted-in development tenants behind a capability
flag. Evaluate context fusion and human usefulness before general availability.

## Acceptance criteria for proceeding beyond design

- Clear customer need beyond benchmark performance.
- Explicit third-party conversation retention and deletion policy.
- No ambiguity about participant identity versus transport role.
- Existing durable-memory behavior remains backward compatible.
- PostgreSQL remains authoritative and Qdrant remains rebuildable.
- Exact evidence links survive conflict, supersession, agent deletion, and retries.
- Security tests cover tenant, user, agent, audience, and revoked-source boundaries.
- A storage/cost model is approved before retaining normal customer transcripts.

## Recommendation

Do not implement this entire design merely to improve a public benchmark. First
validate with prospective voice/chat-agent customers whether governed episodic
recall is a launch requirement. Until then, position MemoryOS accurately as a
governed durable context and state-management layer, not complete transcript recall.

If customer evidence supports the capability, implement only Phase 0 and Phase 1
first. The LoCoMo smoke has already supplied the regression case: John, Tim, and
the Seattle/Chicago/New York evidence must remain distinct without changing the
current durable memories.
