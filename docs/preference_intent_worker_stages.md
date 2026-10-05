# Runtime preference integration: staged implementation

## Current state

The closed contract and v3 transaction now have a **default-off queue adapter**
in `api/services/runtime_preference_worker.py`. This completes the controlled
worker integration, not production activation or a semantic release gate.
Existing v1/v2 records are not upgraded. SDK, MCP, assistant, UI and production
configuration were not changed by this stage.

`api/services/preference_intent_transaction.py` now supplies the disconnected,
provider-free transaction stage, reused through its v3 subclass. It stores a
separately versioned reply/receipt in the existing bounded
job columns, using the existing ownership, encryption and retention checks.
Version-one and version-two readers reject one another's records; existing v1
records are not migrated or reinterpreted. No schema migration is needed.

Its `capture`, `admit` and `apply` operations preserve the original snapshot and
first validated decision. `apply` accepts a plain mapping of already prepared
embeddings, never a provider callback. Preference identities are locked in
stable order and absence/target versions and authority are rechecked before
the existing writers run. Memories, audits, vector outbox, reviews and the
terminal receipt commit or roll back together. A retry after commit returns the
same created IDs without replaying writes, even if job completion overwrote the
ordinary result. The external conversation reference is frozen at capture and
rechecked, with the same 255-character limit as the public ingestion contract.

**Version-two fact limitation:** that stage does not apply fact corrections. Nonpreferences
are retained in the fixed reply and submitted as non-activating source reviews.
The receipt separately reports deferred facts, buffered reviews, capacity
rejections and closed-review replays. Capacity rejection is not successful fact
storage. Tentative, contextual and unrepresentable preferences also remain
non-activating reviews. `state=applied` means this limited transaction finished,
not that all findings became current memories. Relation validation and fact
writes still require the next stage before any worker can use this service.

`api/services/preference_fact_transaction.py` supplies the **version-three
transaction stage** used by the default-off adapter. It captures at most 50 owned, temporally current memory
bindings with a +1 completeness check. Protected preference rows are veto
inputs, never fact-write targets, even if their category is mislabeled. The
same evidence validator, relation parser, resolver mutation policy, claim
ledger, audit, version, pending-review and vector-outbox writers are reused.
No separate fact mutation engine or new public input schema was introduced.
The v1/v2 wire records and terminal outcomes are not reinterpreted as v3.

An injected relation service classifies detached rows after the read session
closes; application cannot call it. The first checked reply and backend-parsed
fact plans are persisted together, with a digest covering both lanes and the
captured fact context. Fact corrections and accepted preferences share the
existing single write transaction and terminal receipt. Source conversation
and external conversation references survive both writes. Fact provenance
explicitly supplies the existing enforced authority-rule format at priority
20; higher stored provenance or active-ledger authority cannot be overwritten.
Unknown legacy authority defaults remain conservative, not silently downgraded.

Application uses PostgreSQL SERIALIZABLE isolation and rechecks context and
selected versions; concurrent changes can produce SQLSTATE 40001 and require
retrying the **same fixed record**, never recapturing or reclassifying it. This
stage deliberately does not implement the worker's retry scheduling. Changed
context on retry becomes pending, not an unintended replacement. All memory,
audit, outbox, review and receipt writes roll back together on failure.

Prior incoming facts are nomination-only transient candidates during planning.
Selecting one, or reusing the same existing target in a batch, defers the
involved findings rather than independently activating contradictory values.
Transient IDs never become durable targets. Unrelated new facts can be stored
together. This is conservative dependency handling, not a semantic proof that
a classifier correctly identified every relationship.

Ambiguous, unrepresentable, failed, stale, automatic-merge, protected-target and
incomplete-context decisions stay non-activating source reviews. Pending plans
need no embedding. The receipt distinguishes `facts_created`, `facts_rejected`
and `deferred_facts`, alongside review-capacity outcomes and exact created IDs.
`deferred_facts` does not mean every review was retained: existing capacity and
closed-generation fences still apply. This fact stage does not construct new
selectable fact alternatives or activate generated merge text.

**Current limits:** a profile with more than 50 current rows
does not get an automatic fact write, even if the intended target is in the
first 50. The existing full-source relation prompt limits (8,000 supporting
source characters, 32,000 prompt characters) remain enforced without qualifier
truncation. Planning makes at most one relation call per nonpreference finding
(combined batch limit 16). Provider work before durable admission can repeat
after crashes or concurrent admission; exactly-once provider billing is not
claimed. Large-profile candidate selection, live provider/model accuracy,
latency/cost still need their own gates. Retry/cache coordination is now covered
by controlled worker tests, not by deployed service or provider measurements.
Controlled SQL tests do **not** establish those release properties.

The closed reply requires both `preferences` and `nonpreferences` arrays,
including empty arrays. The combined batch limit is 16. Each finding cites an
exact user quote and a turn index in 0..63. Preference values are verbatim and
bounded to 256 characters. Quotes and fact content are bounded to 2,000
characters. Property/scope references must come from the backend catalog.
Unrepresentable intent uses `abstain`; it does not invent an identity or value.
No model-supplied authority, confidence, target IDs, applicability objects,
versions, or arbitrary metadata are accepted.

Preparation uses the existing evidence validator, declaration builder, and
value-free choice binder. Definite adoption/correction binds `set_current` to an
offered target/version; an `assert` does not automatically replace an existing
preference. Withhold/reaffirm need no alternative value. Tentative, contextual,
abstained, or absent-target choice findings remain pending preparation, never
current defaults. Unrelated facts are prepared separately, not sent through the
preference writer or dropped. Conflicting findings for the same preference
identity and detectable preference/fact double-processing are rejected.

These checks establish attribution and shape, **not semantic correctness**.
A wrong model category or scope can still be syntactically valid. A future
semantic evaluation must measure that risk independently. Controlled multilingual
fixtures verify that binding has no phrase-matching dependency; they do not
prove that a classifier understands those languages.

## Implemented default-off worker stage

The queue entry point checks the durable job before dispatching legacy extraction.
The implementation follows these constraints; activation remains separately gated.

1. Select only eligible, durable tenant-owned jobs using a default-off backend
   flag and a tenant-owned catalog. Do not trust broker/public opt-in fields.
   Keep registered-source events, agent jobs, manual declarations, and domain
   overlays on their existing paths until their coordination is designed. Do
   not quietly skip their processing. A captured job must never fall back to
   legacy extraction after flag/configuration changes.
2. Use the implemented v3 capture for the transcript hash, source conversation,
   preference snapshot and bounded fact-target context. Close the session before
   classification or embedding. Do not replace this with another capture store
   or reinterpret v1/v2 records. Retain existing size, encryption and retention
   checks when adding worker selection.
3. Classify into this reply contract through the existing model router.
   Reuse the existing fact relation parser/decision mapper outside SQL
   transactions. Do not copy its mutation policy or hard-code a provider.
   Invalid, oversized, or unavailable responses must not activate preference
   text through legacy extraction. Fix the first validated decision durably.
4. Prepare embeddings outside SQL transactions. Inside one short transaction,
   recheck job ownership, profile state, transcript/catalog identity, and all
   selected target versions. Acquire preference identities in deterministic
   order, including absence reservations. Recheck captured absence rather than
   turning a first-writer race into an unintended replacement. Reuse the
   existing writers, audits, pending reviews and vector outbox.
5. Use the implemented v3 adapter to apply nonpreferences with prepared,
   backend-validated relation decisions. Do not make provider/vector calls from
   the write transaction.
   Recheck selected targets and context before applying them. Never let that
   lane mutate governed preferences. Incomplete/stale context must become a
   pending result, not proof of absence. Explicitly test interactions between
   multiple new facts within one batch; independent plans cannot safely assume
   they are unrelated.
6. Commit all accepted preferences, unrelated facts, reviews and a terminal
   receipt together. A retry returns that receipt without replaying writes.
   Cache invalidation follows the commit and is retried from the receipt if it
   fails. Job completion must preserve the receipt. Provider requests before
   durable admission can repeat after crashes; do not claim exactly-once
   provider billing.

## Controlled coverage and activation checks

- Real disposable PostgreSQL: mixed preference/fact correction, uncertainty,
  reaffirmation, captured absence race and protected-authority rejection.
- Failure injection before commit, after commit, during encryption/review/outbox
  writes and cache invalidation. Retry must preserve exact created IDs and
  produce no duplicate memories, conversations, audits or reviews.
- Source/profile/catalog changes and flag rollback cannot cause refresh,
  rebinding, cross-user access or fallback to the legacy preference lane.
- One-slot pool and provider spies prove all provider work occurs with no
  retained SQL connection/transaction. Large context and response limits are
  explicit failure/review conditions, never silent truncation.
- Existing extraction, preference choice/admission, public job-status privacy,
  retention and cache regressions remain green.

No paid evaluation, production configuration changes, or SDK/MCP/assistant
changes are part of this staged work. Synthetic transaction tests do not replace
an independent semantic evaluation before any future activation.

The local transaction gate uses synthetic owners in a disposable PostgreSQL
instance, with provider methods forbidden. Set `DATABASE_URL` explicitly to
that isolated database (never the application or production database), migrate
it, then run:

```powershell
python -m pytest tests/unit/test_preference_intent_transaction_contract.py tests/integration/test_preference_intent_transaction.py tests/unit/test_preference_intent_admission_service.py tests/integration/test_preference_intent_admission.py -q
```

The integration suite is also part of the existing governance CI gate.

For the v3 fact transaction gate, add
`tests/unit/test_preference_fact_transaction_contract.py` and
`tests/integration/test_preference_fact_transaction.py` to the same isolated
database run. That integration suite is also included in the governance CI gate.

## Rollout contract and remaining activation gates

Fresh selection requires all three operator settings:

- `RUNTIME_PREFERENCE_ENABLED=true` (default `false`).
- `RUNTIME_PREFERENCE_TENANT_IDS` contains the job's tenant UUID (default empty;
  normalized, unique UUIDs, maximum 64).
- `EXPLICIT_PREFERENCE_CLAIMS_ENABLED=true` for the existing read/write contract.

The active owned tenant must have `metadata.runtime_preference_catalog`, a closed
object with only `properties` and `scopes`, matching `PreferenceSnapshotCatalog`
without its `tenant_id` field. The backend derives that ID from the durable job.
The catalog is server configuration, not a new API input or a model-generated
property registry. There is no catalog-management UI/API in this stage. Its
serialized limit is 32 KiB, with at most 16 properties, 32 scopes and 32 offered
property/scope identities. It does not grant authority or permission.

Before capture, registered events, MCP/client assertions, explicit declarations and
agent jobs retain their existing writers. Normal conversation ingestion also
creates a provenance event; it is eligible only with the backend-issued
`conversation_ingestion_sha256` binding in that event's processing metadata.
This separately binds prepared turn IDs, source, policy and ownership without
changing the original source-envelope hash contract. Public metadata cannot issue
the binding. A registered writer bound to the API key remains excluded even when
the request omits `source`. Historical unmarked events remain on their existing
path; there is no automatic backfill or authority promotion. Changed marked jobs
fail closed. Capture fixes the source-event binding; each transactional step and
receipt delivery rechecks it. Source events are share-locked during writes.
Created preferences, facts and pending reviews retain the source-event ID;
attestation remains `client_asserted` and authority stays capped at 20.

An allowlisted normal job with missing/invalid catalog
or a domain overlay fails as a configuration error; it does not silently skip an
overlay or run the legacy preference lane. Do not allowlist domain-overlay tenants
until that coordination is designed. Broker and public payload fields cannot
enable this route, replace its catalog or select its owner.

After capture, the frozen catalog and source are reused even if live catalog
configuration changes. Rollback flags suspend unfinished records through the
existing bounded retry/dead-letter machinery, never recapture or legacy fallback.
An already-applied receipt may finish cache delivery/job completion under rollback
flags, but source retention/hash, ownership, profile blocking and tenant activity
are still rechecked. Unknown v1/v2 or malformed records fail closed, not conversion.

One primary structured classification call runs through `LLMService`, retaining
configured provider/model selection and its existing provider limits. It is not
a regex prefilter or a call added to synchronous retrieval. It runs per selected
queued ingestion; repeated provider attempts before admission remain possible.
The primary prompt is bounded to 128 KiB UTF-8 and its reply to 64 KiB, with an
8,192-token output cap. Oversized/invalid/unavailable responses fail without
legacy activation. Evidence validation is independent of provider schema support.
Relation planning retains the existing limits described above. Embedding model
configuration is read in a short SQL session, detached, and supplied to the
existing embedder after that session closes; an unavailable Redis model cache
cannot silently pick an unrelated DB model.

The SERIALIZABLE apply transaction and receipt remain atomic. SQLSTATE 40001 is
reported separately and uses the existing queue retry schedule with the same fixed
decision. Primary/relation classification is not repeated after validated admission.
Precomputed embeddings are not durable and may be requested again after a crash.
The adapter adds no second retry scheduler or memory-write engine.

Cache invalidation runs after commit in strict mode: Redis errors/open circuits
are not swallowed. Existing request-side cache callers retain best-effort behavior.
Deletes are batched at 100 keys, but SCAN still has Redis-keyspace work; this is
not a constant-time invalidation claim. SQL read hydration/currentness remains
the safety boundary, not cache deletion alone. Local worker caches are cleared;
other processes' local caches are not broadcast-cleared. A cache outage can leave
the job failed/dead after its memories committed; the private receipt records that
outcome and retries do not write again. No exactly-once delivery/billing claim.
Public job results contain created IDs and bounded counters, not private evidence,
catalogs, decisions or raw transcripts.

The controlled worker suite uses disposable PostgreSQL and isolated Redis:

```powershell
python -m pytest tests/unit/test_runtime_preference_classifier.py tests/integration/test_runtime_preference_worker.py tests/integration/test_runtime_preference_ingestion.py -q
```

Do not enable writes for an integration that only uses generic retrieval:
declared preferences require the already-existing SDK/API `preference_context`
and `preference_decisions` read contract. Generic vector retrieval deliberately
does not promote them. That integration contract must be exercised before an
assistant canary, without preference-specific assistant prompt workarounds.
### Stage 1 visibility foundation (not a launch gate)

Eligible normal ingestion now reserves `runtime_route=preference_v1` in the same
commit as the job and source event. Catalog/routing configuration is checked
before acceptance. A reserved job cannot fall back to legacy extraction when
rollout flags are disabled, its source binding changes or its catalog disappears.
Existing exclusions and the client-asserted authority cap of 20 are unchanged;
legacy conversational authority 50 is neither rewritten nor promoted.

Current tenant/proxy reads use indexed ownership-scoped EXISTS checks. Pending
updates return no learned memory data and additive `memory_visibility` with
`state=processing` or `state=unavailable`, `scope=user_learned_context`. The fast
preflight does not call embeddings, vector search or a read-time classifier.
Historical `as_of` reads remain historical and do not use this currentness guard.
Processing is not a clarification: old review cards, clarification cards and
domain context overlays are suppressed while it is pending. The prompt-ready
notice preserves bullets/JSON/XML formatting and respects the context budget;
the typed status remains even when the prompt budget cannot accommodate it.

When candidate ranking occurs, final processing state, refreshed SQL rows and
explicit preference ledger selection share one short REPEATABLE READ snapshot.
There is no provider/vector call inside that snapshot. The read releases the
request's read-only transaction before opening it and refuses to commit pending
caller mutations. SQL is authoritative even if cached candidate content is old.
Query embeddings now also detach their selected model configuration and release
the read transaction before provider work; vector search follows with no held
SQL connection. Existing model-ID selection and provider routing are unchanged.
An applied timestamp is only an indexed projection of the private receipt: the
runtime writer stores it in the same transaction as memory changes and receipt.
Cache delivery/job completion is not required to release the guard. Empty/partial
results are possible until replacement facts reach vector ranking; no instant
vector publication or complete causal fact read is claimed.

Pre-reservation runtime captures remain guarded through their private marker.
Only fully validated receipt delivery projects their applied timestamp. Retention
preserves their route before erasing private evidence; it does not treat an old
`state=applied` string as a proof. Failed/dead/redacted unfinished jobs report
unavailable. Expired evidence cannot be retried, and retention marks it unavailable;
expiry never makes an old value current. They require operational recovery; this
slice does not add an unaudited cancellation/reset endpoint.

This initial guard is conservative across the user's learned context. The later
target-withholding stage below adds an independently gated narrowing path.
Continuous writes or failed jobs can therefore reduce context availability;
bounded lookup cost does not imply a latency/availability SLO. Evidence-backed
hold discharge, measured sustained-load behavior and Stage 2
catalog-driven generic reads remain required before a customer canary. Generic
reads still do not activate declared preference winners without `preference_context`.

Deploy both additive migrations and all API/worker instances while runtime flags
remain disabled, then drain/reconcile old captured jobs before considering a
separately approved canary. Old workers do not understand reservations. The
concurrent indexes are a separate revision so an interrupted build can be retried
without replaying column additions; only these exact invalid indexes are repaired.
No automatic enrollment/backfill of historical uncaptured jobs occurs.

Controlled guard checks:

```powershell
python -m pytest tests/integration/test_runtime_pending_visibility.py tests/unit/test_preference_choice_contract.py -q
```

These tests cover immediate reads, acceptance/commit races, rollback, cache lag,
legacy authority, retention, old captures, owner isolation and a synthetic
3,000-job backlog. The index plan check proves index compatibility, not production
p99 or sustained-throughput performance. Backend withholding does not force a
customer model to ignore contradictory conversation history; that is outside the
returned-context guarantee.
Independent semantic evaluation, provider availability/fallback evidence,
latency/cost measurements and a separately approved tenant canary remain required.

### Stage 1 recovery hardening

Internal dead-letter discard refuses runtime reservations and historical runtime
captures with `409 runtime_job_discard_forbidden`, including applied jobs. Deleting
the job would erase both the pending guard and the routed-owner read boundary.
Retention can erase customer text but preserves the minimal guard. Ordinary
legacy-job discard still uses its existing audit/deletion path.

Manual runtime retry uses the existing admin-only endpoint:

```text
POST /v1/internal/dead-letter-jobs/{job_id}/retry?reason=<operator explanation>
```

`reason` must be nonblank and at most 512 characters; do not include credentials
or customer transcripts. Legacy retries do not require it. A nonblocking row lock
prevents concurrent retry/discard transitions (`409 dead_letter_job_busy`). Before
queue admission, the runtime adapter checks ownership, active tenant/owner,
rollout gates, retained source binding, snapshot and fixed private record. Changed,
expired, redacted or inconsistent evidence returns 409 without dispatch or a fresh
capture. Unapplied jobs need enabled gates; fully validated applied receipts may
finish delivery under rollback. A fixed decision is never refreshed/reclassified.

Retry preserves route, applied timestamp, private record, original source and
transcript. It resets the existing operational attempt cycle, not memory authority
or decision state. Queue reservation remains the existing Redis admission, not a
second scheduler. An `AuditAction.updated` record with
`event=runtime_job_retry_requested` commits atomically with the queued transition.
It records credential class `admin_secret`, request ID, operator explanation,
principal/job IDs, state transition and decision digest. Shared admin authentication
does not establish a named human reviewer. No transcript/catalog enters this audit.
Database/audit failure rolls back retry and releases its queue reservation.

Runtime broker messages contain only the durable job reference. SQL commits before
dispatch, and the blocking broker operation runs off the API event loop. A
successful acknowledgement conditionally installs its task lease,
without replacing a worker/watchdog lease. If dispatch fails, the API returns 503
with `runtime_retry_dispatch_unavailable`, `job_id`, and `status=queued`. The guard
and audit remain; the existing watchdog can recover the unleased queued row after
its dispatch grace. Do not interpret that 503 as permission to create a new job.
This is recoverable dispatch, not exactly-once delivery or a latency guarantee.

Evidence loss is not resolved by clearing a guard, deleting a job, changing rollout
flags, or submitting an unrelated successful job. Such jobs remain unavailable;
evidence-backed hold discharge is still a separate release requirement. The later
quarantine stage records terminal operational diagnosis without discharging holds.
There is no new cancellation/reset endpoint, UI, trust tier or authority promotion.
Catalog-driven generic reads remain unimplemented.

Controlled recovery checks (disposable PostgreSQL and Redis; providers forbidden):

```powershell
python -m pytest tests/integration/test_runtime_recovery.py tests/integration/test_runtime_pending_visibility.py tests/unit/test_internal_endpoints.py -q
```

### Frozen target footprint foundation

Before raw evidence expires, the runtime adapter now stores a minimal
`runtime_withholding_footprint` on the extraction job in the same transaction as
the first validated private decision. It contains job/tenant/proxy/capture IDs,
transcript/source/decision digests, and either backend-selected preference
fingerprints and fact target IDs/versions or an explicit user-wide coverage
result. It contains no preference values, quoted turns, property/scope labels,
catalog descriptions, model confidence or authority. The projection is closed,
bounded to 16 combined targets and 8 KiB, and fixed across application/retry.
Its digest detects inconsistent representations; it is not a signature or an
independent semantic correctness certificate.

Uncaptured jobs have no footprint. Empty decisions, abstention/unrepresented
preferences, missing fact targets, or incomplete fact context retain user-wide
coverage. A mixed decision with any unknown coverage widens the whole footprint;
do not truncate it into apparent completeness. Known targets come from captured
backend bindings, never model-supplied memory IDs or public payload fields.
Application/recovery compares a retained footprint against its validated private
decision. Corruption or rebinding fails closed. Retention erases the private
transcript/decision but preserves this minimal projection and the existing route;
it neither sets `runtime_applied_at` nor unblocks reads. Ordinary owner deletion
continues deleting the job and its projection together.

Deploy the additive `runtime_withholding_footprint` migration before API/worker
code. There is no table backfill, new index, public schema or rollout flag.
The nullable column has no default. Its size/type constraint is added NOT VALID
to avoid a deployment-time scan; PostgreSQL still enforces it on inserted/updated
rows. Historical NULL projections remain unknown, not implicitly reconciled.
Validated/applied historical records may acquire a projection only through the
existing evidence-checked transactional writer, never a standalone metadata read.

The foundation stage did **not** implement target-level read withholding or terminal
reconciliation, and does not improve current context availability yet. Retrieval
used the indexed user-wide guard, including cached and generic reads.
Next, target-bound reads must handle scoped/global fallback, legacy preferences,
fact versions, mixed/unknown jobs and concurrent writes before returning unrelated
context. Only then can a separate audited reconciliation record safely retain
holds on proved targets; unknown coverage must remain user-wide. No admin
acknowledgment, footprint digest, timeout or newer successful job proves that old
context is current. Runtime flags remain default-off; no canary is authorized.

Controlled checks (no paid providers):

```powershell
python -m pytest tests/unit/test_runtime_preference_footprint.py tests/integration/test_runtime_footprint_persistence.py tests/integration/test_runtime_recovery.py tests/integration/test_runtime_pending_visibility.py -q
```

### Gated target withholding and audited quarantine

`RUNTIME_TARGET_WITHHOLDING_ENABLED=false` is a new default-off read gate. Narrowing
also requires membership in the existing `RUNTIME_PREFERENCE_TENANT_IDS` allowlist.
Write rollout rollback does not clear holds. Disabling narrowing or removing
membership restores the conservative user-wide hold; it does not restore old
context. No tenant is enrolled or production configuration changed by this stage.

Current reads keep the indexed pending/routed-owner checks. When narrowing is
enabled, they load at most nine minimal job projections to prove that at most
eight pending jobs exist. Each must have a supported route, a closed valid
server-owned footprint and matching job/tenant/proxy binding. Any unknown coverage,
missing projection, overflow or invalid binding retains user-wide withholding.
Digests detect representation inconsistencies, not independent semantic accuracy.
No transcript, private classifier snapshot or read-time model call is needed.

Up to 128 combined targets can be examined across the eight jobs. Fact targets
must still exist under the same owner, remain unarchived and match their frozen
versions. Missing/changed targets widen withholding instead of guessing a successor.
Hydrated fact targets and directly linked previous-version results are withheld.
For explicit preference reads, held requested identities and held global identities
are checked **before** scoped/global winner fallback. A held project cannot silently
reactivate a global default for that project; a held global default conservatively
blocks its scoped reads too. An unrelated explicit global/project read can remain
available. Legacy generic preferences cannot prove typed identity, so **all generic
preference candidates** are withheld when any preference identity is held. Generic
reads still do not activate declared preference winners without `preference_context`.

Final hold checks, SQL hydration and preference selection share the existing short
REPEATABLE READ snapshot. Ranking/provider work is outside it. Candidate caches
are not authority; partial results are SQL-rechecked and not labeled cache hits.
Unknown coverage can still suppress all learned context during continuous writes.
These fixed query/row bounds are not a measured production latency or throughput SLO.

The additive `memory_visibility.scope=affected_context` means that affected
learned context was withheld while unrelated eligible results can be returned.
`user_learned_context` remains the broad/default scope. Both use the existing
`processing`/`unavailable` states. Neither is a clarification or authorization.
Bullets/JSON/XML notices and safe context share the requested prompt budget; if
the notice cannot fit, the prompt is empty but typed visibility survives.
Pending states suppress domain overlays, old review cards and preference decisions.
MemoryOS cannot enforce how a customer's model treats separate conversation history.

Deploy `runtime_job_reconciliation` **before** this API/worker code. It creates a
small internal `runtime_job_reconciliations` table keyed uniquely by job ID, with
owned tenant/proxy references, a closed record capped at 4 KiB, and an audit link.
Ordinary job/owner deletion cascades this record. Audit retention may null the
audit link but cannot remove the job's hold. No data backfill is required. Do not
downgrade this table while new API/worker instances are running.

The existing admin authentication protects the new endpoint:

```text
POST /v1/internal/dead-letter-jobs/{job_id}/reconcile?reason=<operator explanation>
```

It accepts only unapplied dead runtime jobs with an active, matching owner/tenant.
The operator explanation must be nonblank and at most 512 characters; never put
credentials or transcripts there. It records credential class `admin_secret`,
not an independently verified human identity. A nonblocking job-row lock prevents
concurrent retry/quarantine/discard transitions. Missing/busy/conflicting requests
return 404/409, invalid explanations 422, and failed admin authentication 403.

The backend checks permanent evidence failure without providers or recapture:
expired/redacted source, invalid retained record, or changed source binding.
Provider outages, rollout flags or missing current catalog alone are **not** proof
of evidence loss (`409 runtime_job_recoverable`). Expired/redacted evidence may
retain an already valid bound target hold. Invalid/changed evidence and unknown
coverage require user-wide withholding. No footprint is synthesized or repaired.

An `AuditAction.updated` record with `event=runtime_job_quarantined` commits
atomically with the quarantine record. The same normalized reason returns the
original record/audit; a different reason cannot rewrite it. Audit/database failure
rolls back both. A quarantined job stays dead and keeps its source, private record,
footprint and unapplied timestamp unchanged. Manual retry returns
`409 runtime_job_quarantined`; discard still refuses to delete the guard. There is
no new scheduler, provider call, trust tier, cancellation/reset or authority change.

**Quarantine is an operational diagnosis, not recovery of current context.** It
does not clear the guard, set an applied receipt, activate old values, or treat an
unrelated successful job as covering lost evidence. Evidence-backed hold discharge
using new authoritative covering input is not implemented in this stage. Unknown
coverage can therefore remain user-wide indefinitely; do not enable a customer
canary until that availability policy is explicitly settled and tested. Independent
semantic evaluation and measured latency/load/provider resilience gates also remain.

Controlled checks (disposable PostgreSQL/Redis; no paid providers):

```powershell
python -m pytest tests/integration/test_runtime_target_withholding.py tests/integration/test_runtime_quarantine.py tests/unit/test_runtime_guarded_context.py tests/unit/test_runtime_reconciliation_schema.py tests/unit/test_preference_choice_contract.py -q
```

Local verification on 2026-10-06: 225 focused runtime/read/recovery/contract tests
passed, followed by 19 target/preflight checks after a nullable-branch type fix.
The complete local unit suite passed 2,603 tests (four dependency/runtime warnings).
This count includes existing local exploratory tests; it is not a GitHub CI run.
Fresh PostgreSQL migration and downgrade/re-upgrade passed. New modules and
focused tests passed Ruff. Comparing scoped Mypy results against HEAD confirmed
the existing 35 router errors and one context-builder annotation error, rather
than hiding them or claiming the whole repository type-checks cleanly. All
providers were controlled/forbidden; no live traffic, paid semantic evaluation,
holdout, production activation or load/SLO validation was performed.
