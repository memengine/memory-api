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

**Fact limitation:** this stage does not apply fact corrections. Nonpreferences
are retained in the fixed reply and submitted as non-activating source reviews.
The receipt separately reports deferred facts, buffered reviews, capacity
rejections and closed-review replays. Capacity rejection is not successful fact
storage. Tentative, contextual and unrepresentable preferences also remain
non-activating reviews. `state=applied` means this limited transaction finished,
not that all findings became current memories. Relation validation and fact
writes still require the next stage before any worker can use this service.

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
Live selection, provider execution, fact planning/application and cache/job
coordination below are still unimplemented. They need separate, narrow review
before connecting anything to the extraction pipeline.

1. Select only eligible, durable tenant-owned jobs using a default-off backend
   flag and a tenant-owned catalog. Do not trust broker/public opt-in fields.
   Keep registered-source events, agent jobs, manual declarations, and domain
   overlays on their existing paths until their coordination is designed. Do
   not quietly skip their processing. A captured job must never fall back to
   legacy extraction after flag/configuration changes.
2. Capture the transcript hash, source conversation, preference snapshot and
   bounded fact-target context once using the existing job storage. Close the
   session before classification or embedding. Add a separately versioned
   record; do not reinterpret existing version-one receipts. Retain the
   existing admission size bound, encryption and retention behavior.
3. Classify once into this reply contract through the existing model router.
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
5. Apply nonpreferences separately using prepared, backend-validated relation
   decisions. Do not make provider/vector calls from the write transaction.
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
