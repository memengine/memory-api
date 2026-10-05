# Runtime preference integration: staged implementation

## Current state

`api/services/preference_intent_contract.py` is inactive preparation only. No
worker, router, SDK, MCP tool, or assistant imports it. No provider calls or SQL
writes were added. Existing version-one admissions and production flags are
unchanged. This is **not** a completed worker integration or a release gate.

`api/services/preference_intent_transaction.py` now supplies the disconnected,
provider-free transaction stage. It is not imported by the live worker or any
router. It stores a separately versioned reply/receipt in the existing bounded
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

`api/services/preference_fact_transaction.py` supplies that next, **disconnected
version-three stage**. It captures at most 50 owned, temporally current memory
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

**Limits before worker integration:** a profile with more than 50 current rows
does not get an automatic fact write, even if the intended target is in the
first 50. The existing full-source relation prompt limits (8,000 supporting
source characters, 32,000 prompt characters) remain enforced without qualifier
truncation. Planning makes at most one relation call per nonpreference finding
(combined batch limit 16). Provider work before durable admission can repeat
after crashes or concurrent admission; exactly-once provider billing is not
claimed. Large-profile candidate selection, live provider/model accuracy,
latency/cost and worker retry/cache coordination still need their own gates.
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

## Remaining transactional worker stage

The provider-free preference transaction is implemented and locally tested.
Live selection, primary intent-classifier execution and cache/job
coordination below are still unimplemented. They need separate, narrow review
before connecting anything to the extraction pipeline.

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

## Required tests before wiring or activation

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
