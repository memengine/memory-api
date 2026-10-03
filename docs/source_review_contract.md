# Non-activating source reviews

Related local contract: [explicit preference applicability](explicit_preference_contract.md).
It is opt-in and disabled by default, not an enabled SDK/assistant capability.

Release status: backend commit `15da090` verified live on 2026-10-02;
Python SDK `0.1.1` published on main PyPI and TypeScript `0.1.1` published on npm.
Live verification used the checkout's Python client,
not an installed published release.
This is a review-delivery fallback, not a new semantic classifier or an extraction
accuracy certificate. Existing two-memory clarification contracts are unchanged.
The temporary merge containment described below was deployed as `4bd0fbc`: API health
and one fresh stored memory's worker provenance match that commit. Its specific live
containment branch remains unexercised; the project candidate in the one approved smoke
was rejected by extraction evidence validation before the source classifier ran.

## Boundary

A verified user quote establishes attribution, not a committed, correctly scoped
property value. When the existing source decision recognizes uncertainty or
ambiguity but cannot produce a usable option, it may attach a backend-only
`restate_source` outcome. The worker persists that outcome with the existing
pending candidate. No fake Memory, embedding, active claim or selectable model
value is created for this fallback.

Only recognized semantic outcomes qualify. Invalid source roles/hashes, stale
targets, malformed provider responses, unavailable providers and incomplete
context remain processing failures, not questions attributed to the user.
The model still controls semantic interpretation; misclassified uncertainty and
incorrect interpretations that pass the existing option checks remain separate
limitations. This change does not fix or weaken those semantic checks.

## Retrieval contract

### Temporary source-backed merge containment

Source-backed `MERGE` decisions now preserve the existing active memory and buffer the
original verified incoming candidate with decision reason `source_merge_containment`. The backend
creates a target/version-bound source review through the existing pending store. This gate
runs after owned-target and staleness validation, before authority resolution or any archive
or replacement write. Higher writer priority cannot bypass it. It adds no provider call,
new SDK schema, or customer UI requirement.

This applies to every automatic merge on the `verified_source_spans` path, including
apparently legitimate merges. Generated merge wording cannot yet prove preservation of
source qualifiers and applicability scope. The generated merge is not stored or given the
incoming evidence's authority. Existing ungrounded/legacy merge behavior is unchanged.

This is containment, not an automatic scope repair: general/project coexistence is not
guaranteed; source-backed `UPDATE`, extraction omissions, and semantic classification
errors remain separate limitations. Existing review answers never activate this candidate.
`restate` still requests normal ingestion and may remain pending again. Customers must
use the actual job/retrieval outcome, not claim successful storage from an acknowledgement.

Current `POST /v1/memories/retrieve` responses add `source_reviews`, defaulting to
`[]`. Historical `as_of` requests do not surface current reviews. A review contains:

- `id`, opaque `version`, `kind: "restate_source"`, `question`, `expires_at`;
- optional `target_memory_id` and `current_memory_content` (a preview capped at
  1,000 characters, with an ellipsis when truncated—not a user-confirmed claim);
- `actions`: `restate`, `dismiss`, and, for a still-valid target, `keep_current`.

Responses expose at most three recent candidates and hydrate targets in one
additional bounded query. Reviews remain pending across reads; merely seeing a
question does not consume it. Foreign, expired and changed targets are excluded.
TTL is fixed at seven days from the first generation's server-side ingestion time;
reinforcement cannot extend it indefinitely. No processing metadata,
raw source transcript, model reasoning or new authority appears in this view.

The response is a bounded recent view, not a complete review inbox. Stale rows
are conditionally expired without overwriting concurrent worker refreshes, so
subsequent reads advance past them. The service inspects at most three selected candidate rows per call;
older reviews may need a subsequent read and are not paginated in this slice.
Local SQL-plan checks cover 1K/10K/100K synthetic rows; a targeted partial expression
index supports scoped and legacy lookup, including empty matches. Loaded production
p99 and throughput have not been measured. Do not
advertise guaranteed delivery of every pending review or a load-tested SLA.

Existing retrieval context remains the last admitted memory state. This slice
does not add a global pending-property suppression system or promise immediate
read-your-writes while asynchronous ingestion is still queued.

## Answer contract

`POST /v1/memories/source-reviews/{id}/answer` takes:

```json
{
  "external_user_id": "customer-owned-user-id",
  "version": "<opaque version returned with the review>",
  "action": "restate"
}
```

The normal authenticated tenant/API-key write policy applies. The backend looks
up an existing customer identity, locks the owned pending record and target,
and rechecks the source version, target content/authority/scope/temporal validity
and expiry. Unknown/foreign reviews return 404; stale or closed reviews return
409; unavailable actions return 422. No new customer identity is created here.

- `keep_current`: dismiss the interpretation while preserving the stored target
  unchanged for ordinary reviews. For the opt-in backend unresolved-choice
  extension, a matching valid review can additionally restore the existing
  preference's usability and rotate its version binding. It does not promote
  authority, activate candidate text, reactivate an archived/expired row or
  create a memory. See [the explicit preference contract](explicit_preference_contract.md#durable-unresolved-current-choice--backend-only-extension).
- `dismiss`: dismiss the interpretation without changing any memory.
- `restate`: return `resolved: false`, `next_step: "add_memory"`. The review stays
  pending. Ask the user in the customer's existing chat, then submit their actual
  new statement through normal `add` ingestion. Do not submit a model paraphrase
  as direct user input, or treat this answer as completed storage.

Review storage and a durable preference-choice barrier are separate. Dismissing,
restating, expiring, purging or rejecting storage of a review cannot restore a
withheld default. Reads continue to report an unresolved choice without requiring
an available review card. This backend policy is disabled by default and does
not provide automatic natural-language admission or typed SDK exposure yet.

The existing job lifecycle reports what ingestion actually did. If it remains
pending, do not report successful resolution. A changed target invalidates the
old review; source-review resolution is not yet linked atomically to a new job.

## Review lifecycle hardening (local, not deployed)

Open identical interpretations reinforce one generation, preserving its original
seven-day deadline. Server-issued extraction job identity and ingestion time
bind retries to their generation. Dismissed/expired generations remain terminal;
jobs ingested before closure cannot reopen them or join a newer generation.
New evidence ingested after closure can create a new generation, not reactivate
an old memory. Exact pre-hardening fingerprints are recognized for their original
job, so a retry does not reopen an already closed legacy review.

The existing review-answer route locks the candidate and obtains its target lock
with `NOWAIT`; a busy target yields `409 source_review_target_busy_retry`, avoiding
a lock-order deadlock with a worker. No review answer increases authority.

Storage admission is serialized by a short tenant-scoped PostgreSQL advisory
transaction lock, with no provider work inside that lock. Retained source-review
rows (open and closed) are capped by `SOURCE_REVIEW_PROFILE_ROW_LIMIT` (default
256) and `SOURCE_REVIEW_TENANT_ROW_LIMIT` (default 10,000). Counts stop at the
configured cap rather than counting an unlimited backlog. Existing over-cap data
is not silently deleted or grandfathered into new admission. At capacity the job
reports `extraction_metadata.source_reviews_capacity_rejected`; that candidate
is not retained or activated. Other admissible work in the job can still succeed.
Duplicate/replayed closed work reports `source_reviews_closed_replays`.

Celery's background maintenance runs every five minutes, handling at most 1,000
expirations and 1,000 deletions per run (maximum caller batch size 5,000), with
row locks and `SKIP LOCKED`. Reads/answers enforce the deadline even if maintenance
is delayed. Dismissed/expired rows are purged after their original seven-day
window plus `SOURCE_REVIEW_CLOSED_RETENTION_DAYS` (default 30), including their
candidate text and evidence. This does not delete active memories or their audit
history, and it does not change raw extraction-job payload retention.
Old jobs beyond the review deadline cannot recreate purged reviews.

Limits are operational safety defaults, not measured product capacity or a
promise to deliver every review. Closure timestamps conservatively fence
already queued evidence; it may require a genuinely new ingestion after closure.

## SDK contract and release boundary

Python sync/async clients expose `result.source_reviews` and
`answer_source_review(id, external_user_id=..., version=..., action=...)`.
TypeScript exposes `result.sourceReviews` (absent on older/frozen clients;
default `[]` on the normal client) and `answerSourceReview({...})`.
Existing clarification fields and `answer_clarification`/`answerClarification`
remain compatible. No assistant-specific phrase matching or UI was added.

Model calls, holdouts, MCP deployment and production configuration are unchanged.
Publish versioned SDK releases and update public docs only after the backend
deployment is verified; do not claim these new methods exist in older releases.

## Verification

Unit tests exercise semantic-fallback eligibility, non-activation, ownership,
write permission, expiry, target and source-version changes, repeat delivery,
bounded preview, and target-aware pending-candidate deduplication.
Real PostgreSQL tests exercise the existing parser/worker, persisted pending
record, review projection and locked answer. Existing canonical clarification
selection tests verify later retrieval returns its winner.
Model outputs and embeddings in these lifecycle tests are controlled. They are
not fresh real-model evaluations, independent holdouts, or load tests.

The merge-containment regressions cover English/Hindi/Hinglish source text, higher/equal/lower
writer priority, both apparently faithful and scope-erasing generated merges, and stale targets.
Real PostgreSQL worker tests verify repeated input reinforces one pending candidate without
archiving the current memory, creating a successor, changing its claim winner/revisions, or
adding version/vector-outbox churn. Existing source review projection and all three review
answers are exercised without activation. These tests establish containment, not model scope accuracy.

### Live release smoke (2026-10-02)

One fresh synthetic user was exercised through `https://api.memoryo.dev` with
normal authenticated add, job-status, retrieve and review-answer calls. Health
reported `15da090` and all three dependencies healthy; OpenAPI exposed the new
field and answer route. Two ingestion jobs completed without retries:

- A C++ default created one memory.
- A natural uncertain C++/Python statement created zero memories, buffered one
  candidate and surfaced a target-bound review on retrieval.
- Repeated reads returned the same review/version. `restate` left it pending
  and requested normal ingestion. `keep_current` closed it; content, archive
  state and authority remained unchanged. Closed reviews disappeared on retrieval.
- Unknown-user answers returned 404; stale versions and closed replays returned 409.

Four warm retrieval calls took 157-187 ms end-to-end from this machine. This is
not a p99, added-latency estimate, or load result. Extraction remained async;
job processing took about 20.3 s initially and 10.4 s for the uncertain update.
The journey used real backend model calls, but one result does not establish
semantic accuracy. No holdout, prompt tuning or direct provider experiment ran.
Actual billing is not returned by job status: primary extraction reported 9,572
input and 269 output tokens; source decisions reported 2,819 total tokens with
no input/output split. Embedding charges and exact total cost are not measured.

### Merge-containment deployment smoke (2026-10-02)

One approved check used a fresh synthetic user and the unchanged general/project-only
C++ statements. Both jobs completed without retries. Setup created one general memory;
the project job reported one model-returned candidate, one `evidence_validation` rejection,
zero created memories, zero pending candidates, and zero source-classifier calls. No
source review was returned. The original memory ID, content, lineage, authority and history
remained unchanged; foreign-user retrieval returned no memory, review, or clarification.

This verifies API/one worker deployment and observed preservation, not execution of
`source_merge_containment` or successful scoped ingestion. The public rejection count
does not reveal the exact invalid candidate field, so do not attribute it to a specific
quote/content/role error. No retry, prompt edit, gate relaxation, review answer or holdout
run followed. Raw evidence is ignored at
`artifacts/internal-benchmarks/source-merge-containment-live-20261002-01.json`.
Recorded model tokens: 11,180; exact billing and embeddings cost are not returned.

### Evidence rejection diagnostics (2026-10-03)

Job status adds `extraction_metadata.candidate_validation.evidence_rejection_counts`.
This subdivides the existing `rejection_counts.evidence_validation` bucket; the
original counts and acceptance rules are unchanged. Each generic evidence rejection
contributes one first-failing backend check, not every possible problem in a candidate.
Proposal-policy failures already have their own existing reasons and are not counted
again here. Successful direct-user fallback and accepted/pending claims add no entry.
Counts aggregate across the existing primary, empty-response repair and correction
recovery passes. This change introduces no additional pass or model call.

Reasons include `missing_evidence`, `invalid_evidence_turn`,
`evidence_not_visible_to_model`, `no_user_evidence`, `unsupported_relation`,
`missing_source_spans`, `invalid_source_spans`, `invalid_source_span_shape`,
`invalid_source_span_turn`, `invalid_source_quote`, `quote_not_found`,
`quote_not_unique`, `duplicate_source_span`, `candidate_source_mismatch`, and
`unsupported_user_evidence`. Keys come from backend checks, never model wording.
Diagnostics contain only reason codes and integer counts: no source text, quotes,
candidate content, raw model output, or caller-provided authority. Existing evidence
validation and span-only callers share the same validation implementation.

Older jobs need not contain this field; absence means diagnostic detail was not
recorded, not that evidence passed. The failed 2026-10-02 project job cannot be
retrospectively diagnosed from these new counts. No database migration, SDK release,
assistant change, or authority relaxation is needed. Local controlled tests verify
the diagnostic path; deployed model behavior still needs a separately approved smoke.

Local verification: 1,568 unit tests passed (25 existing warnings), 58 real PostgreSQL
integration tests passed, and all 9 FAST suites passed without product failures or
harness errors. A 480-case controlled comparison against the pre-change committed
parser found identical outcomes and coarse counts. The SQL regression verifies job
completion persistence, owned job-status serialization, foreign-tenant denial, zero
source-classifier calls after rejection, and preservation of the current memory.
These checks made no paid provider calls and did not rerun any holdout.

### SDK release verification (2026-10-02)

The Python wheel and sdist passed `twine check`, uploaded to main PyPI, and their
registry SHA-256 digests matched the local artifacts. A fresh registry install
imported `Memory`, `AsyncMemory` and the new review types/methods successfully.
Python SDK contract tests: 27 passed. TypeScript typecheck, ESM/CJS build and
10 contract tests passed. The user completed npm's authenticator requirement;
registry latest is `0.1.1`, and its SHA-512 integrity matched the tested seven-file
tarball. A fresh npm install passed all 10 contract tests against the downloaded
ESM bundle; CJS exports and review methods were verified too. No login retry
loop ran. These package checks made no production or provider calls.
