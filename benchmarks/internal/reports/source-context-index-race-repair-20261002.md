# Source context and asynchronous indexing repair

Date: 2026-10-02. Implementation baseline: `85d6655`.

## Scope and observed failure

The retained four-journey development smoke completed only two journeys. In the
Hinglish uncertainty journey, primary extraction already had the earlier SQL
memory, but source classification received zero targets. Read-only job, SQL,
outbox and CloudWatch inspection showed that vector search completed about
248 ms before the earlier memory's vector upsert completed. Adding a wait to the
assistant would hide that backend context loss rather than repair it.

A separate English journey had a target but returned an unavailable candidate
representation. This repair does not change that model result, instructions,
model, or source schema. Pending is safe abstention, not a successful resolution
journey.

## Implemented boundary

- Reuse the worker's SQL context in source classification. Validate the job's
  tenant/proxy binding before creating a conversation. SQL load errors propagate
  through the existing worker failure/retry path; they are not an empty profile.
- SQL context contains only unarchived, currently effective, unexpired memories.
  Fetch at most 51 rows to distinguish a complete profile of at most 50 memories
  from a bounded partial snapshot. Complete snapshots need no vector search.
- For larger profiles, add at most 20 unsynced current memories outside the
  first 50. Fetch one additional sentinel row. An overflow stays pending without
  invoking source classification. This uses the existing memory/outbox indexes;
  it adds no scan of the full transcript, new service, schema, or index-wait loop.
- Merge partial SQL context with at most 20 vector nominations, deduplicating
  IDs and checking SQL ownership/currentness. Claims stored earlier in the same
  batch are also available before indexing. More than 90 nominees or the existing
  32,000-character source-input cap causes pending, not silent truncation.
- Native governance searches require a completed vector response. Healthy empty
  search and timeout/open circuit are distinct; unavailable search on a partial
  profile leaves the claim pending. Existing ordinary retrieval fallback behavior
  is unchanged. Complete small SQL profiles can proceed without the vector index.
- Revalidate the selected target after classification, including temporal fields.
  Changed, expired, or foreign targets cannot authorize an update. Model output
  never grants authority or supplies tenant identity.
- Record only bounded counts/availability flags in job source-decision telemetry;
  no raw source text, model payload, or credential is added.

The candidate bounds are internal safeguards, not a public memory quota. Large
profile nominations are not an exhaustive conflict search across every historical
claim. The loaded context is not a global serialization fence against simultaneous
new claims. No calibrated model accuracy, p99 latency, or high-load claim follows
from these local tests.

## Verification

The initial four new regressions failed against the baseline before implementation.
Final verification used the bind-mounted checkout inside local Docker, a named
disposable PostgreSQL database, Redis DB 15, and disabled provider credentials.

| Gate | Result |
| --- | --- |
| Full unit suite | 1,415 passed, 22 existing environment/deprecation warnings |
| Six PostgreSQL/Redis integration files | 82 passed, 1 pytest configuration warning |
| Deterministic FAST tier | 9 registered, 9 executed, 9 passed, 0 skipped |
| Critical Ruff checks and whitespace diff check | Passed |
| Paid provider calls / blind holdout use | None / none |

Integration coverage includes the real worker, SQL persistence, pending review,
chat-choice endpoint, claim ledger, provenance and current retrieval. The model
responses and vector nominations are controlled. Clarification and correction
pass with an empty vector result while earlier writes remain in the outbox.
Additional checks cover bounded large-profile sync gaps, overlap with SQL context,
gap overflow, unavailable search, foreign scope, temporal exclusions, and target
changes committed by a separate PostgreSQL connection. Unit checks include
same-batch context, deduplication, SQL failure, strict native search and oversized
source input.

Two failed gate attempts were retained and investigated rather than ignored:

1. A watchdog concurrency test logged dispatch but missed its mock. A no-network
   diagnostic confirmed Celery's shared-task proxy resolved to different concrete
   task objects in the main and worker threads after the application was initialized.
   The test now replaces its module dependency with a stable dispatcher mock.
   Production watchdog code is unchanged; all 82 integration checks pass.
2. FAST capture files failed to truncate on the Windows-mounted output folder.
   Running the unchanged gate with container-native temporary storage passed all
   nine suites. The failed aggregate remains at
   `artifacts/internal-benchmarks/aggregate/20261002T052115Z-fast-v1/aggregate.json`.
   The successful aggregate is inside the local container at
   `/tmp/memoryos-source-context-fast-20261002/20261002T052308Z-fast-v1/aggregate.json`.

## Rollout and remaining work

No deployment, AWS mutation, SDK/MCP/assistant change, prompt change, or holdout
evaluation was performed. Public API methods and response models are unchanged;
the extra job extraction telemetry is additive. Public quickstart guidance still
correctly describes asynchronous writes and nonblocking application responses.
Unrelated public documentation changes were left untouched.

Next, push/deploy this backend commit, verify its live version, and obtain approval
for one bounded development verification using fresh synthetic identities. Check
context telemetry and actual clarification/selection/currentness, retaining failures.
Keep the English representation failure separate. Do not tune prompts, add assistant
rescue rules, or rerun blind holdouts as part of this index repair.
