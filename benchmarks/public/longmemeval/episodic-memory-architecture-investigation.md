# Episodic Memory Architecture Investigation

Status: design only  
Scope: core backend and LongMemEval development pilot  
Production behavior changed: no

## Why this investigation exists

The six-case LongMemEval pilot exposed information that MemoryOS intentionally does not
model well as durable profile memory:

- completed personal experiences, such as attending a named play;
- facts stated by the assistant in an earlier conversation;
- event ordering that depends on session dates;
- corrections whose durable proposition is obscured by conversational wording.

Adding a broad rule to the durable extractor recovered the named experience in all three
development runs, but reduced mean precision by 3.19 percentage points, reduced F1 by
2.78 points, and increased the false-memory rate by 3.19 points. That experiment was
reverted. Episodic recall should therefore not be added by weakening durable extraction.

## Current production architecture

### Ingestion and temporary transcript retention

`POST /v1/memories/add` queues the original messages in an `ExtractionJob`. The job is
tenant/user/agent scoped and can be linked to a durable `MemorySourceEvent`. Raw messages
are retained only in the job payload. `EXTRACTION_PAYLOAD_RETENTION_DAYS` defaults to 30,
after which the provenance task removes messages from the job, result, and dead-letter
payload.

### Conversation records

The `Conversation` model stores an identifier, backing user, optional agent, message
count, timestamps, and processing status. It does not store turns or a retrievable session
summary. A stored durable `Memory` points to its source conversation, but the conversation
cannot independently answer an episodic query.

### Durable governed memory

The `Memory` table is the sole general retrievable memory representation. It contains one
of six profile categories, importance/confidence, temporal validity, version linkage,
source event, provenance, and archive state. These rows participate in:

- conflict and claim reconciliation;
- winner/version transitions;
- importance and decay;
- transactional vector outbox processing;
- current and historical retrieval;
- context construction.

This is appropriate for governed beliefs and user state, but not for every attributable
conversation episode.

### Retrieval and context

Current retrieval searches Qdrant-backed durable memories, applies tenant/user/agent and
lifecycle filters, enforces a semantic floor, hydrates from PostgreSQL, and ranks using
semantic relevance plus importance/recency. Context construction drops memories below
importance 3 and renders only the six durable categories.

There is no separate episodic collection, session search, speaker-role distinction, or
result label that tells a consumer whether content is a governed belief or recalled
conversation evidence.

### Privacy and deletion

Raw turns currently have bounded retention. User deletion cascades through user-scoped
data, and memory hard deletion reconciles claims and vectors. Any episodic design must
preserve these guarantees and must not turn temporary extraction payloads into an
unbounded transcript archive by accident.

## Confirmed architectural gap

The missing capability is not another durable-memory category. It is a separately governed
episodic evidence plane with explicit provenance, speaker role, occurrence time, retention,
and deletion semantics.

Profile memory and episodic evidence have different contracts:

| Concern | Governed profile memory | Episodic evidence |
|---|---|---|
| Meaning | Current or durable belief about the user | What occurred or was said in a session |
| Conflict handling | Claim winner and version chain | Preserve separate attributable episodes |
| Speaker | Normally user assertion | User, assistant, or registered service, explicitly labelled |
| Retention | Lifecycle/decay policy | Explicit transcript/event retention policy |
| Ranking | Semantic score plus importance | Semantic score plus event time/session relevance |
| Correction | Supersede governed claim | Keep history; corrected belief lives in profile memory |
| Privacy deletion | Claim/vector reconciliation | Delete episode, chunks, and vectors together |

## Rejected approaches

1. **Store all named experiences as durable facts.** The three-run experiment demonstrated
   unacceptable precision and false-memory regressions.
2. **Keep extraction-job payloads indefinitely.** This bypasses retrieval contracts and
   violates the existing bounded-retention design.
3. **Treat assistant statements as user facts.** This destroys provenance and can convert
   generated content into asserted user state.
4. **Put episodic rows directly into the existing claim ledger.** Independent sessions are
   evidence records, not necessarily competing claim revisions.
5. **Tune directly on LongMemEval answers.** That would overfit a public benchmark and blur
   the internal/public benchmark boundary.

## Proposed benchmark-only shadow experiment

Before adding a schema or production path, build an isolated development evaluator that:

1. Uses the already frozen six LongMemEval pilot cases.
2. Converts every session into evidence-preserving turn chunks without using the durable
   extraction prompt.
3. Records, for each chunk, only benchmark-local metadata: case/user ID, session ID,
   session date, turn range, speaker roles, and a content hash.
4. Indexes chunks in a disposable, separately named Qdrant collection, never the production
   memory collection.
5. Retrieves episodic chunks using the unchanged production embedding model and query.
6. Keeps durable-memory retrieval unchanged and reports durable-only, episodic-only, and
   fused diagnostic results independently.
7. Deletes the disposable collection and local fixture metadata after results are saved.

This experiment must not create `Memory`, `MemoryClaim`, `MemoryClaimRevision`,
`MemoryVersion`, or production outbox rows. It must not expose episodic results through the
production API.

## Metrics

- session evidence Recall@K;
- session evidence Precision@K;
- MRR and nDCG;
- answerable-case empty-result rate;
- abstention filler rate;
- user/assistant evidence coverage by speaker;
- temporal two-session coverage;
- correction-chain evidence coverage;
- fused retrieval provenance completeness;
- cross-case/user leakage;
- indexing and retrieval latency;
- chunk/vector count and estimated storage growth;
- embedding calls, tokens where available, and cost;
- preview answer accuracy only after separate provider approval.

## Acceptance criteria for the shadow experiment

- evidence-session recall improves materially over the durable-only pilot;
- both previously empty answerable cases return relevant episodic evidence;
- the temporal case retrieves both required sessions;
- the knowledge-update case retrieves both old and new evidence with dates/provenance;
- the abstention case does not become answerable from irrelevant chunks;
- cross-case/user leakage remains zero;
- every result carries session ID, timestamp, speaker role, and content hash;
- durable extraction outputs and production database/Qdrant collections remain unchanged;
- no holdout access;
- no answer/judge calls without separate approval.

## Production architecture only if shadow evidence passes

The smallest credible production design would add a separate `episodic_records` table and
Qdrant collection. Required fields would include tenant, proxy user, optional agent,
conversation/session identity, turn range, speaker roles, occurred/observed time, content
or governed summary, source event, provenance hash, retention expiry, deletion state, and
embedding model identity.

Episodic records should not enter conflict resolution or the claim ledger. Retrieval should
query profile and episodic planes independently, enforce the same authorization boundary,
and return typed results so consumers cannot confuse quoted session evidence with governed
beliefs. Activation would require explicit tenant retention policy, complete hard-deletion
coverage, outbox idempotency, and lifecycle benchmarks.

## Recommended next action

Implement only the benchmark-local disposable episodic retrieval shadow described above.
Do not add production tables, APIs, workers, or retention settings until the shadow results
show that episodic semantic retrieval materially solves the confirmed public-benchmark gap.
