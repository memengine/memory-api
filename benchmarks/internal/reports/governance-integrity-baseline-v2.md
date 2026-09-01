# Governance integrity baseline v2

## Scope

Development-only internal correctness baseline. It combines the unchanged 25-case
lifecycle-provenance pack with a frozen 14-case extension covering correction evidence,
temporal expiration, retained supersession vectors, originating-agent retirement, writer
attribution, API history/export, provenance health and PostgreSQL Passport governance.
Production behavior was not changed and holdout was not accessed.

## Results

- Scenarios: 39
- Product-evaluable: 39
- Passed: 39
- Product failures: 0
- Harness errors: 0
- Governance success: 100%
- Total isolated-run time: 209,674.71 ms
- Mean per scenario: 5,376.27 ms
- Minimum: 885.27 ms
- Maximum: 15,586.38 ms

Every measured area and metric passed, including source/evidence preservation, claim and
version alignment, current winner correctness, duplicate/idempotency behavior, temporal
chain provenance, agent/source preservation, governed revocation, tenant isolation,
PostgreSQL/Qdrant payload contracts, API/export readback, retention redaction, transaction
rollback and governance observability.

## Confirmed strengths

1. Extraction source identity and evidence reach memories and claim revisions.
2. Explicit correction carries predecessor and decision evidence into the reconciled claim.
3. Conflict, manual resolution and temporal expiration preserve single-winner state and
   transactional vector synchronization metadata.
4. Supersession retention remains distinct from privacy deletion.
5. Originating-agent retirement revokes access while retaining the source identity.
6. Retrieval payloads and API history/export return the expected provenance fields.
7. PostgreSQL duplicate, concurrency, attribution and broad Passport governance flows pass.

## No confirmed production repair

No covered scenario exposed a production correctness failure, so changing production logic
would not be evidence-based.

## Remaining benchmark coverage gap

The suite does not yet have one scenario that performs the entire tenant flow through a real
Celery worker and real Qdrant and then performs hard/privacy deletion with database, claim,
outbox, vector and API readback assertions. Current coverage composes multiple real component
and PostgreSQL tests, while the broad Passport flow does not prove that complete tenant
privacy-deletion chain in one execution.

## One isolated next improvement

Add one development-only full-path governance regression scenario:

API source event -> queued extraction -> Celery -> PostgreSQL memory/claim/evidence ->
correction/supersession -> outbox -> real Qdrant -> current and historical readback ->
hard/privacy deletion -> PostgreSQL/Qdrant/API absence checks.

This is benchmark coverage only, not a production behavior change. If it fails, stop at the
first boundary and propose a production repair only after confirming the failure is not
harness drift.
