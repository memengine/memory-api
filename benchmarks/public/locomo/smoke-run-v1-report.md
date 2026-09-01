# LoCoMo Smoke Run v1

Status: completed, non-official diagnostic smoke run

## Frozen inputs

- Dataset SHA-256: `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`
- Sample: `conv-43`
- Question: `conv-43:qa-21` (category 1)
- Question: Which US cities does John mention visiting to Tim?
- Gold answer: Seattle, Chicago, New York
- Gold evidence: `D3:19`, `D6:3`, `D9:6`
- Run ID: `locomo-smoke-v1-20260829`

This run is not an official LoCoMo score. It exercised one frozen question through
the normal local MemoryOS API, extraction-worker, PostgreSQL, outbox, Qdrant, and
retrieval path. It made no answer-model or judge calls.

## Execution results

- Sessions ingested: 29/29
- Conversation turns: 680
- Extraction jobs completed: 29/29
- Extraction retries: 0
- Memories reported created: 72
- Sessions reporting zero memories: 6
- Ingestion elapsed time (sum of sequential session latency): 195.195 seconds
- Extraction-job latency p50/p95/p99: 6.599 / 14.596 / 17.272 seconds
- Retrieval latency: 1.070 seconds
- Retrieval results: 1
- Vector outbox state after the run: 75 done, 0 pending/failed for this sample
- Holdout used: no
- Production behavior changed: no

Provider cost was not emitted by the production path, so it is not estimated in
this report. The run used the configured OpenAI extraction provider and configured
embedding path.

## Correctness result

The retrieved memory was "User is planning a team trip next month to explore a
new city." Its session-level candidate provenance was session 11, which contains
none of the three gold evidence dialogs. Candidate evidence recall was therefore
0%. Exact attribution precision was not scored because production provenance
identifies the source session, not the exact supporting dialog for each extracted
memory.

Database inspection localized two independent failures:

1. Extraction coverage loss: the Seattle fact was stored, but no active or
   archived memory mentioning Chicago or New York was created.
2. Retrieval miss: the surviving active Seattle memory was indexed successfully,
   but the query returned an unrelated trip memory instead.

The vector outbox fully converged, so temporary Qdrant indexing lag does not
explain the retrieval miss.

## Interpretation and next experiment

This smoke run validates that the adapter and full infrastructure path work, but
the single question fails correctness. It is insufficient for any public score or
marketing claim.

Before expanding the pilot, the next zero/low-cost diagnostic should replay the
same stored state with controlled retrieval inspection (candidate scores and
cutoff effects) and audit the three evidence turns against extraction outputs.
Extraction and retrieval must remain separate failure labels. No prompt, ranking,
threshold, or production-memory change should be made from this one case.
