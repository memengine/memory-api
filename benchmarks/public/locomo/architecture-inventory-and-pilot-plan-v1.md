# LoCoMo architecture inventory and pilot plan v1

Date: 2026-08-28

Status: design approved for inventory and pilot planning; no LoCoMo provider run has been performed.

## Purpose and interpretation boundary

LoCoMo is the second independent public benchmark in the MemoryOS public-benchmark roadmap.
It evaluates recall and reasoning over long, timestamped conversations. It does **not** directly
measure MemoryOS governance capabilities such as tenant isolation, authority-aware conflict
resolution, claim revision integrity, revocation, or deletion semantics.

The LongMemEval audit established that MemoryOS currently persists governed durable semantic
state, while detailed episodic conversation evidence is not a complete production memory plane.
LoCoMo therefore serves as an independent check of that same architectural boundary. A weak
LoCoMo score must not be presented as proof that the governed memory engine is generally weak;
conversely, a strong score must not be presented as proof of governance correctness.

## Official benchmark contract

Upstream repository: https://github.com/snap-research/locomo

Paper: *Evaluating Very Long-Term Conversational Memory of LLM Agents*, ACL 2024.

The released corpus contains ten conversations. Each conversation contains:

- a stable sample identifier;
- two named speakers;
- chronologically numbered sessions and session timestamps;
- turns with speaker, dialog ID, and text;
- optional image metadata and generated captions;
- generated observations and session summaries;
- annotated event summaries;
- QA items with question, answer, category, and evidence dialog IDs when available.

The QA task has five categories:

1. single-hop;
2. multi-hop;
3. temporal reasoning;
4. open-domain/common-knowledge reasoning;
5. adversarial/unanswerable.

The paper reports normalized token-level partial-match F1 by category. It also reports whether
RAG retrieves the annotated answer-bearing dialog context. The first MemoryOS implementation
will target QA only. Event summarization and multimodal dialog generation remain separate future
tracks.

The upstream dataset is licensed CC BY-NC 4.0. The downloaded dataset must remain outside Git,
and any public/commercial use of results must be reviewed against that non-commercial license.

## MemoryOS production-path mapping

| LoCoMo object | MemoryOS mapping | Required preservation |
| --- | --- | --- |
| conversation | one isolated external user namespace per run and sample | no cross-sample state |
| speaker | turn role plus original speaker name in source metadata | speaker attribution |
| session | one `/v1/memories/add` event | chronological observed time |
| session timestamp | `source.observed_at` | original timestamp and ordering |
| dialog ID | evidence reference in provenance | retrieval-grounding measurement |
| question | `/v1/memories/retrieve` query | no gold answer/evidence sent to MemoryOS |
| retrieved memory | bounded evidence supplied to answer model | memory ID, score, provenance |
| adversarial QA | expected abstention evaluation | no empty-context shortcut |

The real path is:

`official dataset -> adapter -> add API -> extraction job/Celery -> PostgreSQL -> outbox/Qdrant -> retrieve API -> fixed answer model -> official-compatible scorer`

No benchmark-only episodic index may be included in the primary MemoryOS result. If an episodic
oracle or shadow comparison is later useful, it must be reported as a separate architecture
diagnostic and never blended with production-path metrics.

## Reuse and required new components

Reuse from LongMemEval:

- API-key authentication and local API configuration;
- add/job-poll/retrieve transport behavior;
- deterministic event IDs and idempotency headers;
- provider-call approval flags;
- latency, token, cost, and failure classification;
- public-path/holdout guard;
- ignored local dataset and result directories.

Implement separately for LoCoMo:

- strict dataset models for conversations, sessions, turns, and QA records;
- timestamp parser for the upstream format;
- speaker-preserving turn conversion;
- dialog-ID-level provenance references;
- normalized token F1 matching the upstream evaluation contract;
- evidence-dialog retrieval metrics;
- adversarial/unanswerable scoring;
- deterministic pilot selection and manifest.

Do not subclass the LongMemEval dataset contract. Only transport utilities should be shared;
the benchmark schemas and scoring semantics must remain independent.

## Frozen pilot design

The pilot is a wiring and architectural diagnostic, not a publishable LoCoMo score.

- Dataset: official `data/locomo10.json`, pinned by upstream commit and SHA-256.
- Conversations: two selected deterministically by hashing `sample_id`, without inspecting QA
  answers or evidence.
- Questions: 25 total, selected deterministically within the selected conversations.
- Stratification: five questions from each official category where availability permits.
- Ingestion: complete sessions for each selected conversation through the normal MemoryOS API.
- Retrieval: one frozen `limit` and context-token cap inherited from the accepted production
  retrieval path; no tuning on pilot labels.
- Answering: one pinned model, temperature zero, with the prompt recorded verbatim.
- Scoring: normalized token F1 overall and per category; evidence recall at 1, 5, and K;
  evidence MRR; adversarial abstention accuracy.
- Operations: ingestion/job/retrieval latency, failures by boundary, provider calls, tokens, and
  estimated cost.
- Isolation: a unique run ID and external-user namespace; fixtures cleaned after artifact capture.

Selection must be generated once into a manifest containing only sample IDs and QA indices/IDs.
After freezing, the selection code must verify the manifest and must not silently regenerate it.

## Pilot acceptance criteria

These criteria validate the adapter, not product quality:

- dataset validates and its hash is recorded;
- selected cases remain stable across repeated validation;
- no internal holdout path can be loaded;
- no gold answer, category, or evidence label crosses the MemoryOS API boundary;
- every accepted session has a deterministic event ID and preserved timestamp;
- evidence dialog IDs can be recovered from provenance when extracted content is returned;
- retries do not create duplicate logical ingestion events;
- product, provider, adapter, and evaluator failures are reported separately;
- machine-readable artifacts contain complete question accounting;
- provider calls require explicit approval and a declared maximum call/cost cap.

There is deliberately no minimum product F1 threshold for the first pilot. Its purpose is to
determine whether LoCoMo confirms the LongMemEval episodic-memory gap and to localize failures.

## Cost and safety caps

Before any paid run, the runner must print and require approval for:

- selected conversation/session/turn/question counts;
- maximum extraction calls implied by ingestion;
- maximum answer-model calls;
- whether a model judge is used;
- estimated input/output tokens and upper-bound cost;
- API URL, tenant identity, and service key without secret values.

The first execution sequence is:

1. offline dataset validation only;
2. offline contract/unit tests;
3. one-question local API smoke without answer evaluation;
4. inspect ingestion, provenance, retrieval, and cleanup;
5. request explicit approval for the frozen 25-question pilot.

## Stop conditions

Stop before tuning if any of the following occurs:

- dataset/evaluator ambiguity changes question accounting;
- speaker or dialog-ID attribution is lost at ingestion;
- the adapter requires production behavior changes;
- results primarily reproduce the already-established missing episodic-plane limitation;
- provider cost exceeds the approved cap;
- any internal holdout path is accessed.

After the pilot, classify failures as production memory-plane mismatch, extraction loss,
retrieval failure, temporal/multi-hop reasoning failure, answer-model failure, evaluator issue,
or benchmark annotation ambiguity. Do not modify production behavior until that diagnosis is
reviewed.
