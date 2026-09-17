# Phase 2.6b: evidence and capacity foundation

## Implemented boundary

- Tenant-scoped turn hashes keyed by tenant, user, conversation, and turn.
- Explicit assistant proposal registration; ordinary assistant messages do not register proposals.
- Proposal IDs persisted with the extraction job and returned by add/job-status responses.
- Both SDKs preserve conversation/proposal fields; old responses default to empty collections.
- Redis token-owned provider leases, whole-call timeouts, fail-closed admission when configured
  limits cannot be checked, and deterministic retry jitter.
- Queue-wait timing in job status. Rejected evidence rolls back and releases queue reservations.

Roles and source kinds remain authenticated-application assertions, not independent proof
that an end user authored text. This phase does not increase their authority.

## Deliberately not implemented

Semantic acceptance binding, topic classification, calibrated acceptance scores, and automatic
proposal resolution belong to Phase 3A. The one-hour proposal expiry is stored for that future
resolver; consumers must not interpret an active row as valid past its expires_at timestamp.
No MCP implementation, assistant UI, production deployment, or package publication changed.

## Rollout requirements

1. Apply migration phase26b_evidence_ledger before deploying the matching API/worker code.
2. Release the matching Python and TypeScript SDKs before presenting their new fields as public.
3. Configure LLM_PROVIDER_CONCURRENCY_LIMITS explicitly (for example openai=8).
   Empty configuration leaves this additional limiter disabled.
4. Workers sharing provider credentials must share the Redis deployment/database and limits.
   This is concurrency admission, not token-per-minute or request-per-minute enforcement.
5. Lease duration is at least the configured provider timeout plus 15 seconds. Abrupt process
   termination can temporarily occupy a slot until lease expiry.
6. Monitor queue wait, provider-limit logs, retries, and database load during rollout.
   The default provider-state Redis client uses one-second connect/read timeouts
   with no automatic retries. Custom injected clients must likewise be bounded.

The ledger stores hashes rather than another raw transcript. It is deleted with its owning
proxy user or extraction job through foreign keys. Existing payload-redaction behavior remains.
Each request is bounded to 64 turns; database work grows with submitted turns. The contention
test is not a sustained-load benchmark, and no production p99/throughput claim is made.
Stable conversation and turn IDs are required for cross-request identity; without them,
generated identifiers are scoped to an ingestion job.

## Verification

The local PostgreSQL test covers constraints, actual service persistence, replay, changed-turn
rejection, conversation/user isolation, and ordinary assistant messages. The real Redis test
submits 40 concurrent admissions at a cap of three and checks expiry/stale-owner release
and mixed lease lengths.
FAST benchmarks are deterministic contract gates, not real-model extraction accuracy measurements.
