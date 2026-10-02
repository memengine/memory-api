# Scoped review write comparison - 2026-10-02

## Scope and outcome

One explicitly approved separate-write development replay, using a fresh synthetic user against
`https://api.memoryo.dev`, deployed version `15da090`. Health reported all storage services OK.
No production prompts, extractor, classifier, governance policy, SDK, assistant or deployment
were changed. No frozen holdout was read. No live evaluation was retried.

Both the earlier batched run and this separate-write run failed their unchanged scope assertions.
The separate case sends the same user sentences, under one stable external user/conversation,
but waits for the general default's job before submitting the project-only default.

## Direct evidence

All three ingestion jobs completed without attempts/retries. Primary extraction used
`gpt-4o-mini`; source-decision calls also completed.

1. Setup created the general C++ default.
2. The project-only write created a successor whose public metadata says `resolution=MERGE`,
   `relation=mergeable`, and `commitment_status=committed_current`.
3. Its decision explanation explicitly recognizes the particular project context. Despite that,
   the successor's stored content is only "My default language for every programming example is C++."
4. Public GET/history confirms that the general predecessor is archived, with a `conflict_update`
   history entry, and the successor's `previous_version_id` points to that general predecessor.
5. Warm and settled retrieval return that general-worded successor, with no project-only memory.
6. The undecided Python statement creates no memory and one pending candidate. A source review
   is delivered against the successor. No review action or final replacement write was attempted,
   because the distinct general/project precondition failed.
7. Public provenance retains the external conversation ID and `client_assertion` authority 20;
   foreign-user retrieval returns no memories, source reviews, or clarification.

Private artifacts (ignored, not committed):

- `artifacts/internal-benchmarks/scoped-review-live-20261002-01.json` - earlier batch run.
- `artifacts/internal-benchmarks/scoped-review-separate-live-20261002-01.json` - separate run.
- `artifacts/internal-benchmarks/scoped-review-separate-followup-20261002-01.json` - bounded
  supplemental public memory/history observations, without another ingestion or model call.

## What this establishes, and what it does not

Batching is not the sole explanation: the separate path independently demonstrates an unsafe
scope-erasing merge. Retrieval is returning what was stored; the assistant is not the source of
this particular missing scope. Recognizing scope in reasoning is insufficient when a subsequent
write still erases it. Verifying source IDs/spans authenticates evidence references but does not
prove that transformed memory content preserves every source qualifier.

The exact pre-merge extraction content and raw classifier response are not exposed by these
public endpoints. Do not claim that the initial extraction was fully faithful, or that a single
comparison proves model-wide reliability. The batched omission and separate merge may be
different failures. Later review/restatement/lineage steps remain unverified live.

## Next repair boundary (not implemented)

Inspect and reproduce the existing source classifier -> decision mapper -> merge builder path.
First preserve this specific destructive transition in a backend regression, not only a runner
assertion. Scope compatibility and preservation must constrain destructive UPDATE/MERGE;
model reasoning, similarity, same value, and equal authority cannot authorize deleting a
different applicability context. If scope cannot be established safely, retain the existing
active memory and route the incoming candidate to a pending/review outcome. Do not introduce
fixed Release Check/C++ regexes, require customer assistant prompts, or recommend unbatching
as the production fix. Any representation/schema change needs a separate inspected design and
approval, plus historical, scoped correction, concurrency and authority regressions.

## Timing and usage

Three add acknowledgements: 845-1,149 ms. Three job-poll observations: 5,931-8,364 ms.
Four retrieval requests: 163-982 ms. These are development samples, not p99 estimates or browser
first-token latency. No extraction wait was added to the assistant.

Primary pass tokens: 4,881 + 4,959 + 4,967 = 14,807.
Source-decision tokens: 1,338 + 1,547 + 1,502 = 4,387.
Total recorded model tokens: **19,194**. Exact dollar spend is unavailable from this telemetry;
embedding usage and source-model billing details are not fully returned. Do not report a made-up
exact cost or zero provider spend.
