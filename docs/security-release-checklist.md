# MemoryOS security release checklist

Use this checklist only for a separate, disposable environment. It is a staged
validation plan, not authorization to deploy to production.

## Scope

This release validates these controls together:

- TLS for PostgreSQL, Redis, and Qdrant connections
- KMS envelope-encryption dual-write
- Qdrant payloads without memory text
- Redis retrieval cache writes disabled
- expiry redaction for raw job and dead-letter payloads
- durable vector deletion after correction and Passport erasure

Plaintext database columns remain the compatibility read path. Do not remove,
rewrite, or backfill them during this release.

## Preconditions

1. Use an empty non-production PostgreSQL database and a separate Qdrant
   collection. Never point this environment at production data.
2. Apply all Alembic migrations before enabling dual-write.
3. Use a task role that can call `kms:GenerateDataKey` and `kms:Decrypt` on the
   environment's KMS key. Do not provide AWS access keys to the container.
4. Confirm encrypted Redis transport, Redis AUTH, snapshot retention, HTTPS
   Qdrant, and PostgreSQL TLS are enabled.
5. Store all runtime secret values in the environment secret manager. Do not
   place them in tfvars, Docker image layers, source files, terminal logs, or
   this document.

## Controlled configuration

Set these environment values only for the disposable validation environment:

```env
DATA_ENCRYPTION_PROVIDER=aws-kms
DATA_ENCRYPTION_KMS_KEY_ID=<environment KMS key ARN>
DATA_ENCRYPTION_WRITE_MODE=dual-write
VECTOR_PAYLOAD_INCLUDE_CONTENT=false
RETRIEVAL_REDIS_CACHE_WRITE_ENABLED=false
```

Keep `RETRIEVAL_REDIS_CACHE_READ_ENABLED` at its existing value during this
test. Existing cached entries must expire naturally or be invalidated before
using the result as evidence that Redis contains no new plaintext copies.

## Required validation flow

For one synthetic tenant and synthetic user only:

1. Add a durable memory through the public ingestion API and wait for its job.
2. Retrieve it through the public retrieval API; the returned content must be
   correct even though Qdrant stores no `content` payload.
3. Send a correction, retrieve again, and confirm the latest authorized claim
   wins while the prior revision remains auditable.
4. Inspect the new memory/version/job envelopes. Confirm they contain
   ciphertext and never the synthetic memory text.
5. Inspect the Qdrant point. Confirm it has vector and safe metadata but no
   `content` field.
6. Inspect Redis only through a restricted administrator session. Confirm no
   newly-written retrieval result includes the synthetic memory text.
7. Trigger Passport/user deletion. Drain workers and confirm the corresponding
   vector outbox entries converge and the Qdrant point is absent.
8. Create an expired synthetic extraction job, run the redaction task, and
   confirm messages, metadata, free-text errors, and free-text evidence
   references are removed while IDs/hashes remain.

## Acceptance criteria

- API and workers are healthy after configuration change and restart.
- Add, retrieve, correction, provenance, deletion, and retry paths succeed.
- No tenant/user/agent leakage occurs.
- Envelopes decrypt only with the matching tenant or Passport-owner context.
- Qdrant and newly-written Redis retrieval replicas contain no memory text.
- Expired job/dead-letter payloads contain no raw customer text.
- No secret values or memory text appear in application logs or error tracking.
- No unfinished jobs or vector outbox rows remain after drain.

Record only aggregate pass/fail results, timestamps, release commit, migration
revision, and configuration names. Do not save synthetic payload text, secret
values, or screenshots containing them in the release record.

## Stop and rollback

Stop immediately on an incorrect retrieval result, failed KMS call, missing
provenance, deletion failure, or any cross-scope result.

For a dual-write rollout, rollback means set
`DATA_ENCRYPTION_WRITE_MODE=disabled`, retain existing plaintext compatibility
columns, restart API/workers, and investigate with synthetic data only. Do not
delete ciphertext, KMS keys, database rows, or audit records as a rollback
action.

## Production gate

Production requires a separate approval after this checklist passes. The first
production step remains dual-write; ciphertext-only reads, plaintext backfill,
and plaintext-column removal each require their own design, migration,
validation, and rollback plan.
