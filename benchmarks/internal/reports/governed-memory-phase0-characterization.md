# Governed-memory public API journey characterization

Date: 2026-09-30

## Scope

This development-only matrix verifies that governance capabilities compose
through the public tenant API. It uses a fresh synthetic external user for each
journey and does not read or reuse any blind holdout data.

The four journeys are:

1. English C++ to undecided Python preference, followed by an in-chat Python selection;
2. the same uncertainty and selection flow expressed in Hinglish;
3. a durable project-name correction from Atlas to Nova, including idempotent replay and foreign-user isolation;
4. an assistant tool-output claim attempting to replace a user-confirmed C++ preference.

The runner uses only these public endpoints:

- `POST /v1/memories/add`
- `GET /v1/memories/jobs/{job_id}`
- `POST /v1/memories/retrieve`
- `POST /v1/memories/clarifications/{clarification_id}/answer`

## Governance invariants

- An undecided competing value remains inactive until the user chooses it.
- Clarification is returned through the customer's existing chat API flow.
- Selecting an option activates one canonical current claim and removes the superseded memory from current retrieval.
- Durable corrections replace the previous memory without requiring clarification.
- Repeating an ingestion with the same idempotency key returns the same job rather than creating a duplicate.
- A fresh external user cannot retrieve another user's memories or clarification.
- Assistant or tool-output text cannot become trusted user evidence or override a user preference.
- Every returned memory retains a source event, matching external conversation ID, authority, and user-turn evidence.
- Retriable extraction-job failures are polled until completion or a truly terminal state.

## Final live result

Deployment health was `ok` for PostgreSQL, Redis, and Qdrant. Version:
`90dbb35`.

Artifact (local and ignored):
`artifacts/internal-benchmarks/governed-memory-phase0-v3-90dbb35-final.json`

All release checks passed:

- scenarios: 4/4
- complete journeys: 4/4
- immediate safe state: 4/4
- settled state: 4/4
- provenance: 4/4
- clarification resolution: 2/2
- correction idempotency: 1/1
- foreign-user isolation: 1/1
- hostile tool-output boundary: 1/1

After selection, the English journey returned only:
`My default language for every programming example is Python.`

After selection, the Hinglish journey returned only:
`User's default programming language is Python.`

In both cases the selected memory ID was present, the original C++ memory ID
was absent, no clarification remained, and tentative phrases such as
`considering` or `not decided` were absent.

The project correction returned Nova as the only current value. Replaying the
same update was idempotent, and a fresh external user retrieved neither memory
nor clarification. The hostile tool-output journey preserved the user's C++
preference and did not activate Python.

## Descriptive timing

This is a four-user functional sample, not a latency benchmark and not evidence
for percentile claims.

- add acknowledgement: 309.78-2242.69 ms; mean 948.80 ms
- completed job polling: 2404.38-20332.32 ms; mean 6770.82 ms
- retrievals: 162.01-9514.11 ms; mean 982.83 ms across 14 calls
- clarification answers: 809.96-813.30 ms; mean 811.63 ms across 2 calls

The long job and retrieval observations should be investigated with a larger
operational sample before setting an SLO. They do not justify p95 or p99 claims.

## Repairs validated by the matrix

- Semantic conflict decisions now separate relationship, current commitment,
  and whether user choice is required.
- Tentative or unclear competing values are routed to user clarification by the
  backend rather than activated as current memories.
- The classifier returns a structured attribute and candidate value. The
  backend verifies that the value is present in the supported candidate and is
  not the existing value, then constructs the canonical current claim.
- The canonical candidate remains archived until the user selects it.
- The replay runner no longer treats the retriable `failed` job state as
  terminal; it waits for automatic completion or a dead/error state.

These changes add no assistant-specific prompt requirement, new UI, database
migration, or additional model call. Authority, evidence verification,
clarification state, and activation remain backend responsibilities.

## Verification

- focused resolver and replay suites: passed
- full unit suite: 1106 passed, 26 warnings
- critical Python lint: passed
- strengthened live public-API matrix: 4/4 complete journeys passed

## Status

This development matrix is complete for the four covered journeys. It is not a
claim of universal governance correctness, multilingual accuracy, or production
latency percentiles. Broader independent evaluation and operational monitoring
remain separate gates.
