# Bounded durable add replay repair — 2026-10-11

Base HEAD: `4477b839f428f53f787c5a638a7a480dd741e820`. Repair is uncommitted.
This records the owner's approved existing-job admission repair, not pilot
qualification. The earlier cache mismatch reproduction and cache-only repair
remain separate evidence. No private acceptance inputs or labels were used.
Reviewable code/test diff: `durable-add-replay-repair-20261011.patch` in this
directory (eight files, including new files; unrelated changes excluded).

## Diff scope

| File | Necessary change |
| --- | --- |
| `api/db/models.py` | Two nullable receipt columns on ExtractionJob: key SHA-256 and versioned binding; paired/size check and tenant/key unique constraint. |
| `api/db/migrations/versions/durable_add_replay.py` | Revision `durable_add_replay`, parent `preference_confirmation_index`; adds columns/check/uniqueness, reversible. No new table or legacy backfill. |
| `api/services/memory_add_replay.py` | Canonical request binding, key hash and narrow eligible-lane predicate; mismatches/unverifiable bindings refuse without job metadata. |
| `api/services/memory_service.py` | Current SQL tenant/key/owner permission checks; durable receipt lookup; transaction advisory guard before reservation, 5 s lock limit; unique-conflict winner verification and existing loser slot cleanup. |
| `api/routers/memories.py` | Router replay lookup supplies the same binding and current key identity, then uses SQL authority. |
| `tests/unit/test_memory_add_replay_binding.py`, `tests/unit/test_memory_service.py` | Replay boundary checks and corrected SQL-receipt fixtures. |
| `tests/integration/test_durable_add_replay.py` | Synthetic isolated real PostgreSQL/Redis checks; controlled existing worker storage path. |
| `AGENTS.md`, `docs/EXPLICIT_REPLAY_ADMISSION_SPEC_20261011.md`, this report | Approved lifetime, completed evidence, roadmap checkpoint and residual limits. |

No worker mutation code, watchdog, governance rules, frozen contract, settings,
MCP, UI, assistant or infrastructure changes. Unrelated working-tree changes
are preserved. Alembic reports `durable_add_replay (head)`.

## Approved guarantees and limits

For eligible tenant-bound adds, same tenant/key and identical authorized binding
returns the original job's persisted status. Changed owner or payload conflicts;
fresh SQL checks reject inactive tenants, revoked/read-only keys and blocked or
unavailable receipt owners before replay information. The binding includes key
identity and ingestion fields; changing them is a changed request, not a replay.
External authentication still passes through existing authentication middleware.

SQL uniqueness is authoritative, with the advisory transaction guard before
queue reservation. Redis acceptance cache loss/expiry/outage does not admit a
second job while its receipt exists. Redis queue availability is still needed
for fresh admission. A receipt means **admitted, not applied**.

Existing job cleanup redacts raw payloads without deleting jobs; receipt columns
remain for at least 24 h and longer while nonterminal under that lifecycle.
Explicit job/owner/tenant deletion remains effective and ends protection. Later
requests can be admitted again under current permissions. No tombstone or
permanent duplicate prevention. Service-event/agent/MCP/prepared-turn contracts
remain outside the new durable promise; historical unbound rows are not backfilled.

## Focused local test evidence

Commands (from backend root):

```powershell
.venv\Scripts\python.exe -B -m pytest tests/integration/test_durable_add_replay.py -q -x --tb=short -p no:cacheprovider -p no:asyncio
# Exit 0: 18 passed in 7.26 s
.venv\Scripts\python.exe -B -m pytest tests/unit/test_memory_service.py tests/unit/test_memory_add_replay_binding.py tests/unit/test_explicit_preference_contract.py -q -x --tb=short -p no:cacheprovider
# Exit 0: 59 passed in 15.80 s
```

| Synthetic check | Observed evidence |
| --- | --- |
| Five simultaneous exact adds | One SQL job/source event and one Redis reserved slot; all return original job. |
| Forced unique-index concurrent losers | Five attempts with advisory guard bypassed in test only; SQL winner verified, losing source/job inserts rolled back, final reservation depth/member count one. |
| Cache deletion, real TTL expiry, stale cache and prohibited-cache outage | Original SQL receipt returned; no new queue reservation. |
| Changed owner only / payload only | Conflict, original receipt retained. |
| Revoked key, read-only key, blocked owner, inactive tenant | 403; router check also rejects stale request-state key metadata after SQL revocation. |
| Full queue before admission | No receipt committed; later authorized retry admits after capacity restoration. |
| Raw redaction, queued and completed receipts aged 48 h | Messages erased; independent key/binding columns preserved; original receipt still returned. Ageing is not a 48 h soak test. |
| Job deletion, owner/tenant FK cascades | Receipt removed; job/owner deletion permits fresh authorized admission despite old cache; deleted tenant refuses. SQL deletion/cascades exercised, not external deletion API. |
| Migration roundtrip and uniqueness | Actual Alembic Operations downgrade/upgrade within disposable schema; duplicate tenant/key insert rejected by PostgreSQL. |
| Duplicate worker delivery during processing and after completion | Existing worker claim/storage path ignores duplicates; exactly one memory; revision/audit/outbox counts unchanged on duplicates. Controlled declaration pipeline/embedding, not live model/broker. |

Environment: cached PostgreSQL 16 and Redis 7 container images, `--pull=never`, no
mounted production volumes; dedicated localhost ports 55471/55472, synthetic
credentials, random disposable SQL schema and synthetic tenant keys. No configured
deployment DSNs used. Real SQL transactions and Redis Lua reservations run through
synchronous adapters at async boundaries; this does not prove asyncpg pool or HTTP
middleware behavior. Explicit preference settings enabled only on test-process
copies; runtime/V2 and real settings unchanged. Provider/vector dependencies
prohibited except controlled embedding outputs for the worker mutation check.
The original sandbox denied local TCP; execution was retried with permission for
these isolated ports. No AWS resources or provider calls. No acceptance dataset.
After verification, only the two disposable replay-test containers were stopped
and automatically removed; existing project containers were left untouched.

## Unresolved limits and candidate freeze

- Watchdog remains 60 s queued grace and 120 s schedule. Retained P8 recovery
  threshold remains 30 s; this admission fix does not establish it.
- Duplicate delivery checks do not cover crash between memory commit and job
  completion, real broker redelivery, deployed operation or every mutation path.
- SQL/index rollout has not been performed on a populated deployment; migration
  takes table/index locks (5 s lock timeout). Mixed old/new API versions and legacy
  unbound receipts are not covered by the new guarantee.
- No pilot acceptance, application/SDK alignment, ordinary latency or production
  claim is established. Minimum retention follows current no-auto-purge lifecycle;
  a future purge must honor it and is outside this change.
- **Candidate must be repinned**: old HEAD alone excludes this working-tree repair
  and new migration. A reviewed code artifact plus matching schema/environment must
  be frozen before any separately approved independent evaluation. The frozen
  acceptance contract was not edited and no candidate was silently repinned.

Checkpoint: review this scoped diff and evidence; stop for approval. No next
implementation, acceptance run, deployment or further diagnostic is authorized.
