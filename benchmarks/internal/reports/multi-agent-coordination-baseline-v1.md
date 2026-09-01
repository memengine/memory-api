# Multi-agent coordination correctness baseline v1

## Result

Frozen development baseline: **9/17 passed (52.94%)**. Production behavior was unchanged and
holdout was not accessed. Real PostgreSQL, production universal claim code and worker grant
checks were exercised with disposable fixtures.

| Metric | Result |
|---|---:|
| Conflict detection accuracy | 100% |
| Resolution/winner correctness | 100% |
| Source-authority correctness | 50% |
| Duplicate/exactly-once correctness | 0% |
| Concurrent single-winner correctness | 100% |
| Revocation enforcement | 50% |
| Agent-deletion correctness | 50% |
| Provenance after agent deletion | 0% |
| Duplicate active revisions observed | 0 |
| Unauthorized cross-agent leakage | 0 |
| Cross-user leakage | 0 |
| Cross-tenant leakage | 0 |

Regression suites: **35 passed**. One initial PostgreSQL test failure was harness configuration
(`DATABASE_URL` was not exported); it passed when loaded from the configured `.env`. All
temporary users and agents were removed.

## Confirmed weaknesses and boundaries

1. **High security — revocation during worker execution:** authorization is checked once before
   extraction. Revocation after that check can race with persistence because there is no final
   grant revalidation. Boundary: worker authorization-to-commit TOCTOU.
2. **High correctness — durable duplicate delivery:** repeating the same worker event creates a
   second memory and claim revision. It does not create a second active winner, but execution is
   not exactly once. Boundary: API Redis idempotency is not propagated as durable worker/source
   event identity.
3. **High auditability — originating-agent deletion:** foreign keys set source-agent IDs to null;
   worker-created memory metadata does not guarantee an immutable source-agent snapshot.
   Boundary: deletion FK policy plus provenance snapshot creation.
4. **Medium correctness — authority and event time:** universal claims have no authority or
   observed/event-time fields. Older authoritative evidence cannot outrank newer low-authority
   evidence, and delayed events are resolved by arrival order. Boundary: universal claim schema
   and resolver contract.
5. **Medium correctness — cross-agent correction/shared update:** a different Passport agent's
   changed value is disputed and archived; only `user_correction` can replace a winner. There is
   no explicit governed cross-agent update operation. Boundary: universal claim transition rules.
6. **Medium security/product semantics — private memory on agent deletion:** Passport memories
   have grants/categories but no private/shared lifecycle classification. The requested private
   deletion behavior is therefore undefined. Boundary: Passport memory schema/lifecycle policy.
7. **Defense-in-depth:** advisory locking produced one winner under concurrent writes, but the
   universal revision table lacks the database partial unique activated-revision constraint used
   by tenant claims. No duplicate active revision occurred in this run.

## Strengths

Direct conflicts are disputed with the existing winner preserved; compatible claims coexist;
equal-authority conflicts keep a single winner; concurrent same-claim writes serialize correctly;
revocation before worker execution blocks persistence; transactions roll back atomically; shared
memory remains available under surviving grants; isolation remained intact.

## One isolated proposed repair

Repair only the highest-risk confirmed issue: **revalidate the exact active `read_write` grant
immediately before persistence/commit in the universal extraction worker**. If it was revoked or
expired after initial authorization, roll back the staged memory/version/claim/outbox work and
return `write_not_permitted`.

Acceptance: both pre-execution and mid-flight revocation block all memory, version, claim,
revision and outbox persistence; normal authorized writes remain unchanged; retries create no
partial rows; isolation and all existing coordination/security tests remain green. Do not mix
durable event idempotency or authority changes into this repair.
