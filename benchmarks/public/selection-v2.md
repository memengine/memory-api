# Public benchmark selection v2

Date: 2026-08-29

Status: external benchmark expansion paused after contract-fit review

## Product scope decision

The current MemoryOS release is a governed durable state/context layer. It stores
reusable user/customer facts, preferences, goals, procedures, relationships, and
expertise, then manages corrections, conflicts, provenance, authority, lifecycle,
and access boundaries.

Transcript-complete episodic recall is explicitly deferred to a later version.
Public evaluation must not silently broaden that release scope.

## Completed public diagnostics

### LongMemEval

The frozen development pilot demonstrated that LongMemEval requires detailed past
conversation evidence that the durable extractor intentionally does not retain.
It remains useful architecture evidence, but no current-release official score is
claimed and no further paid run is planned.

### LoCoMo

One frozen question was exercised through API, worker, PostgreSQL, outbox, Qdrant,
and retrieval. The infrastructure path passed, but the question failed because
speaker identity and one-off visits were not represented by the durable memory
plane. This independently confirmed the LongMemEval boundary. The remaining pilot
is not scheduled for the current release.

## MemoryAgentBench FactConsolidation review

MemoryAgentBench is an established benchmark that evaluates accurate retrieval,
test-time learning, long-range understanding, and selective forgetting. Its
FactConsolidation conflict split initially appeared aligned with MemoryOS conflict
and supersession behavior.

Upstreams:

- Paper: https://arxiv.org/abs/2507.05257
- Code: https://github.com/HUST-AI-HYZ/MemoryAgentBench
- Dataset: https://huggingface.co/datasets/ai-hyz/MemoryAgentBench

Official dataset inspection at Hugging Face revision
`00d1946269e29b41eed74511997afa8171b91e08` showed a different information model:

- each sample is a long stream of arbitrary world-knowledge facts;
- facts concern many unrelated entities, organizations, locations, and people;
- deliberate conflicting objects test consolidation across single-hop and
  multi-hop questions;
- the official split is not a stream of user/customer state updates.

The current production extractor is instructed to extract durable facts about the
user/customer. Registered service-event mode also canonicalizes authoritative
service assertions as customer facts. Feeding the official world-fact corpus
through that path would lose entity identity. Mapping every entity to a synthetic
`external_user_id`, parsing benchmark predicates, or rewriting statements as
customer properties would add benchmark-specific structure and would no longer be
a clean official comparison.

Decision: do not build or run a MemoryAgentBench adapter for this release. Revisit
only if MemoryOS introduces an explicitly general, entity-addressed claim API.

## Why no replacement benchmark is selected immediately

The established public benchmarks reviewed so far primarily measure one or more of:

- transcript/episodic recall;
- arbitrary document or world-knowledge retrieval;
- long-context reasoning;
- test-time task learning.

None has yet been verified to measure the complete current MemoryOS control-plane
contract: source authority, claim revisions, provenance, tenant/user/agent
isolation, governed supersession, lifecycle, and idempotent event processing.

Running a mismatched benchmark and presenting a low or adapter-enhanced score would
be less credible than declaring the boundary precisely.

## Launch evidence for the current release

Use the frozen internal evidence for engineering confidence, but do not present it
as a third-party leaderboard result:

- extraction golden and live-provider baselines;
- conflict/winner/version-chain correctness;
- durable provenance and evidence preservation;
- temporal current-state behavior within the supported contract;
- multi-agent coordination, revocation, and isolation;
- idempotency, concurrency, outbox/Qdrant convergence, and scale reliability.

Public claims must remain bounded to those measured capabilities. Avoid “complete
memory,” “remembers everything,” or transcript-recall claims.

## Re-entry criteria for external benchmarks

Select another external benchmark only when all are true:

1. Its official input model maps to a production API without question-, answer-, or
   label-aware transformation.
2. The evaluated information type is in the current product scope.
3. The official metric can be retained without a custom favorable reinterpretation.
4. Dataset/evaluator revisions and licenses can be pinned.
5. A one-case zero/low-cost contract smoke passes before any full paid run.

Until then, external benchmark expansion is paused—not failed—and launch readiness
should proceed using accurate positioning and the completed internal regression
evidence.
