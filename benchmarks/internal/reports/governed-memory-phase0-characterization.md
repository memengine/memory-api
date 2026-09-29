# Governed-memory Phase 0 characterization

Date: 2026-09-29

## Scope

This phase records current behavior without changing production extraction,
conflict resolution, claim activation, caching, retrieval, or clarification
logic. It covers two synthetic user journeys through the public tenant API:

1. an unresolved C++ versus Python programming-language preference;
2. an exam-date correction from October 10 to October 18.

The runner uses fresh external user IDs and only these public endpoints:

- `POST /v1/memories/add`
- `GET /v1/memories/jobs/{job_id}`
- `POST /v1/memories/retrieve`

## Expected governance invariants

- An uncertain Python change does not replace the current C++ preference and
  produces a structured clarification.
- A direct exam-date correction supersedes October 10, leaving only October 18
  in current trusted retrieval.
- Results remain correct both immediately after the completed extraction job
  and after the vector-index settling interval.
- The diagnostic captures request, job, retrieval, and created-memory IDs plus
  latency and provenance for synthetic data only.

## Local characterization

- The exact natural uncertainty sentence is not recognized by the deterministic
  clarification-intent fallback. This is recorded as a strict expected failure,
  not repaired in Phase 0.
- Fixture and evaluator contract tests verify the intended invariants without
  changing backend behavior.

## Live run

Deployment health was `ok` for PostgreSQL, Redis, and Qdrant. Version:
`cd30c86`.

The first dry configuration attempted the stale hostname `api.memoryos.io` and
failed DNS resolution. The checked-in SDK defaults and the deployed environment
use `https://api.memoryo.dev`; the runner now uses that same default. Historical
backend documents still contain both hostnames and require a separate docs
consistency review after behavior is corrected.

Artifact (local and ignored):
`artifacts/internal-benchmarks/phase0/governed-memory-20260929T051521Z.json`

Four ingestion jobs and six retrievals were executed for two fresh synthetic
users. This sample is a characterization, not a statistically meaningful
latency benchmark.

- add acknowledgement: 384.13-1739.89 ms; mean 1048.66 ms
- completed job polling: 2369.85-5861.76 ms; mean 4099.04 ms
- retrieval: 123.88-493.96 ms; mean 338.39 ms

Each one-turn extraction sent roughly 3.8K-4.0K tokens. The system prompt alone
was 3,767 tokens; provider latency ranged from 1,071 ms to 2,827 ms. This is a
confirmed cost and governance-job latency optimization opportunity, although it
does not sit on the assistant's answer-streaming path. Four writes and six reads
are too small a sample for percentile claims.

### Programming-language uncertainty

- Both ingestion jobs completed and created one memory each.
- The update extraction did not produce a structured clarification request; its
  extraction metadata reported `clarification_requires_two_memories`.
- The conflict path nevertheless queued a clarification.
- The first post-update retrieval returned the clarification and only the C++
  memory. Python was not returned as trusted current context.
- The later retrieval still returned only C++, but the clarification was no
  longer available because retrieval had already marked it triggered.

This run therefore passed the core claim-state expectation, but confirmed that
clarification delivery is one-shot rather than acknowledged/retryable. The
deterministic fixed-phrase fallback also remains unable to recognize the exact
natural uncertainty sentence on its own.

### Exam-date correction

- Both jobs completed successfully but created zero memories.
- Extraction marked both the original date and the correction as
  `nothing_to_extract`.
- All three retrievals were empty, so the October 18 correction was unavailable
  in a fresh session.
- The extraction prompt contains conflicting product policy: dated events are
  described as temporary and assigned an expiry, while another instruction says
  temporary statements should be omitted. The live model chose omission.

This is an extraction/lifecycle policy failure before conflict resolution or
cache invalidation can be evaluated for the exam claim.

## Verification

- focused runner tests: 4 passed, 1 expected failure
- related extraction and replay regression tests: 57 passed, 1 expected failure
- Ruff: passed for the new Python files
- mypy: passed for the live runner
- the expected failure is the exact natural uncertainty sentence against the
  existing fixed-phrase fallback; it is intentionally not repaired in Phase 0

## Phase 0 exit gate

Phase 0 is complete only after:

- focused tests pass with the known semantic gap reported as `xfail`;
- a live run produces a sanitized artifact (complete);
- the active deployment version and latency baseline are recorded (complete);
- no production behavior change is included in the Phase 0 commit.
