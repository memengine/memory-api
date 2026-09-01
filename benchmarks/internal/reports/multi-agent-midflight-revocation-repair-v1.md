# Multi-agent mid-flight revocation repair v1

The isolated repair passed and is retained. The universal extraction worker now revalidates the
same active, unexpired `read_write` grant immediately before commit. If authorization changed
during extraction, it rolls back the transaction and returns `write_not_permitted` with zero
memories created.

## Post-repair frozen baseline

- Coordination scenarios: **10/17 passed** (baseline 9/17)
- Revocation enforcement: **100%** (baseline 50%)
- Conflict detection: **100%**
- Winner correctness: **100%**
- Concurrent single-winner correctness: **100%**
- Cross-agent/user/tenant leakage: **0**
- All unrelated metrics unchanged

The live probe revoked the grant in a separate PostgreSQL transaction after the worker's initial
check and before its final check. The worker rejected the operation. A focused transaction test
also verifies staged memory/version/claim/outbox work is rolled back.

Final focused regression suite: **35 passed**. Temporary fixtures were removed. Holdout was not
accessed. No authority, idempotency, conflict, extraction, ranking or sharing rules changed.

Machine-readable artifact:
`artifacts/internal-benchmarks/multi-agent-coordination-development-v2.json`.
