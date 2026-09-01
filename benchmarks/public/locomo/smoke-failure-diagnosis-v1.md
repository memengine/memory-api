# LoCoMo Smoke Failure Diagnosis v1

Status: architecture diagnosis; no product changes

## Scope

This diagnosis uses the completed `conv-43:qa-21` smoke run, the official LoCoMo
source turns, persisted MemoryOS rows, and the current extraction/retrieval code.
It does not rerun ingestion, call an answer model or judge, access holdout data, or
change production behavior. The raw retrieval inspection made one query-embedding
call using the same model as the stored vectors.

## Primary conclusion

The failure is not principally a prompt-quality or cutoff-tuning issue. The current
adapter is projecting a two-person episodic conversation into a user-centric,
durable semantic-memory contract. That projection loses both subject identity and
episodic coverage required by this LoCoMo question.

## Confirmed boundaries

### 1. Adapter identity mismatch

For `conv-43`, LoCoMo identifies Tim as `speaker_a` and John as `speaker_b`. The
adapter maps `speaker_a` to the API `user` role and `speaker_b` to `assistant`.
The selected question asks what **John** said he visited, not what Tim visited.

The adapter also submits a registered `source` envelope. In authenticated service
event mode, the production extraction prompt treats declarative assistant messages
as authoritative observations and canonicalizes them as facts about the
user/customer. Consequently, statements made by John can become memories whose
subject is merely "User." The persisted Seattle memory demonstrates that loss:
"User loves the energy, diversity, and food of Seattle..."

This is a harness-to-product contract mismatch, backed by a product-model gap:
ordinary memories do not have a first-class semantic subject/entity field capable
of distinguishing Tim from John.

### 2. Durable-semantic versus episodic-memory mismatch

The production extractor is explicitly designed to retain reusable, durable facts
and ignore one-off operational or temporary chatter. LoCoMo's gold evidence here
contains three historical visit mentions:

- `D3:19`: John is going to Seattle and calls it a favorite city.
- `D6:3`: John says he was in Chicago.
- `D9:6`: John refers to a trip to New York City.

Seattle produced a durable preference memory. Chicago and New York produced no
memory mentioning those cities. This is consistent with the present extraction
contract rather than evidence of a transient worker/index failure.

### 3. Retrieval behavior compounds the loss

All vector outbox operations for the sample converged. Raw Qdrant inspection using
the production embedding model returned 69 active candidates. The unrelated
session-11 trip memory ranked first at `0.354844`, above the configured semantic
floor (`0.315`). It is therefore the exact vector candidate returned by the API,
not a PostgreSQL fallback or an indexing-lag artifact.

The Seattle memory ranked eighth at `0.251280`. It was inside the production
30-candidate overfetch window but was removed by the semantic floor. Only one of
the 69 candidates passed that floor. Lowering the floor to `0.25` would admit eight
candidates, including several unrelated travel goals, so a threshold change is not
an evidence-supported repair.

The score ordering is consistent with the identity loss: the question names John
and asks for completed city visits, while the stored memories refer to a generic
"User" and the surviving Seattle memory is phrased as a preference. This is a
representation/query-alignment failure before it is a ranking-weight problem.

The retriever does also prepend unscored hot-tier entries when present. That is a
separate architecture risk, but this run does not show that path caused the result;
the returned memory and score exactly match raw Qdrant rank one.

## Failure classification

| Finding | Classification | Risk |
|---|---|---|
| John/Tim collapsed into generic "User" | Adapter contract mismatch plus product identity-model gap | High |
| Chicago/New York not retained | Expected consequence of durable-only extraction for an episodic task | High for general conversational memory; not necessarily a regression in the governed durable-memory contract |
| Irrelevant vector candidate outranked Seattle | Consequence of subject/episodic representation loss; retrieval accepted the only candidate above its floor | High for this task |
| Vector synchronization | Passed | None observed |
| Worker/API/persistence path | Passed | None observed |

## What not to do

- Do not lower extraction thresholds or add city/travel examples to the prompt.
- Do not lower the semantic retrieval floor from this single question.
- Do not select a speaker mapping per question or use gold answers/evidence during
  ingestion.
- Do not claim an official LoCoMo score from the current adapter.

Those changes could improve this case while concealing the contract mismatch.

## Recommended next decision

Before expanding the LoCoMo pilot, decide whether MemoryOS intends to support a
first-class episodic conversation-memory plane in addition to its governed durable
semantic-memory plane.

If yes, the smallest architecture-design slice is an entity-preserving episodic
record contract with speaker identity, session/turn identity, event time,
provenance, lifecycle/privacy governance, and semantic retrieval. Durable memories
can still be derived from that record, but must not replace it.

If no, LoCoMo should remain a documented scope-mismatch diagnostic rather than a
public product score. MemoryOS can continue to benchmark its intended strengths
with governance, correction, provenance, isolation, and lifecycle evaluations.

The retrieval sub-boundary is now closed. Further cutoff tuning cannot recover the
two cities that were never represented, and lowering the floor would add irrelevant
travel memories. The next decision must therefore address product scope and memory
representation rather than retrieval constants.
