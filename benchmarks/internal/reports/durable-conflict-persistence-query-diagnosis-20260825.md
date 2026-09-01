# Durable conflict/persistence query diagnosis — 2026-08-25

## Decision

Do not change transaction ownership again. The confirmed remaining tail is repeated, row-at-a-time cross-user conflict persistence inside the durable memory transaction. The next experiment should preserve the existing conflict candidates and resolution decisions while replacing per-candidate duplicate lookups and flushes with one indexed, set-based reconciliation operation.

No production code was changed during this diagnosis. Holdout was not accessed and provider cost was zero.

## Evidence inspected

- Frozen MODERATE transaction-boundary experiment and its reverted candidate.
- PostgreSQL observer samples from the isolated `auth-singleflight-ownership-20260824` run.
- Sanitized SQL telemetry from that run.
- Current conflict detector, resolver, ORM models, and Alembic indexes.

The earlier two-phase candidate localized the remaining durable-write latency to:

- p50: 1.409 s
- p95: 23.768 s
- p99: 29.913 s
- 212 of 703 durable transactions at least 5 s

## Confirmed execution path

For each stored memory, the current production path:

1. extracts broad shared-context entities;
2. selects every non-superseded signal in the tenant having any matching entity type and belonging to another proxy user;
3. treats every different value within that broad entity type as a conflict candidate;
4. performs a symmetric `cross_user_conflicts` existence query for each candidate;
5. inserts and flushes each new conflict individually;
6. loads/resolves the conflict and persists its resulting update before moving to the next candidate.

This all occurs inside the durable memory/claim/version/provenance/outbox transaction.

## Query and transaction findings

### Cross-user conflict work dominates the observed durable tail

The PostgreSQL observer captured 1,488 long-transaction observations whose current or last statement involved `cross_user_conflicts`:

- 602 active observations;
- 886 idle-in-transaction observations;
- maximum transaction age: 46.673 s;
- zero observations reported a PostgreSQL blocking PID.

Across all four extraction workers, the repeated statement was the symmetric conflict-existence lookup. Worker-specific p95 transaction ages while associated with that lookup ranged from 33.898 s to 36.312 s. These values are transaction ages, not individual statement runtimes.

### Individual statements are not independently 20–40 second queries

Sanitized SQL telemetry contained:

- 2,434 logged conflict-existence `SELECT` events;
- 1,263 logged conflict `INSERT` events;
- 1,134 logged conflict `UPDATE` events.

For all 4,831 logged `cross_user_conflicts` SQL events:

- p50: 6.645 ms;
- p95: 20.950 ms;
- p99: 57.786 ms;
- maximum: 138.856 ms;
- errors: 0.

Therefore, the long tail is cumulative application/transaction work and repeated round trips, not one pathologically slow SQL statement or a confirmed database lock wait.

### Index coverage is incomplete for the persistence lookup

Current indexes are:

- `shared_context_signals(tenant_id, entity_type, entity_value)`;
- `cross_user_conflicts(tenant_id, status)`.

The duplicate lookup filters by tenant, entity type, and a symmetric pair of `user_a_memory_id` / `user_b_memory_id`. The existing conflict index does not support this predicate. There is also no database uniqueness constraint for the unordered memory pair.

The signal scan can use the tenant/entity-type prefix of its existing index, but it must still load all active signals of those broad types for other users and compare values in Python. Its candidate set therefore grows with relevant tenant history rather than remaining bounded to the new memory's semantic claim slot.

## Growth behavior

Let `S` be the number of active other-user signals in the selected broad entity types and `C` the number considered conflicting.

Per stored memory, the current path performs approximately:

- one signal query returning `O(S)` rows;
- `O(S)` Python comparisons per extracted entity;
- `C` duplicate-existence queries;
- up to `C` individual flushes and resolution updates.

Across a tenant history that continues accumulating comparable signals, total work can approach quadratic growth. The benchmark evidence confirms the resulting fan-out and transaction occupancy, but it does not by itself prove that every generated conflict is semantically false. Conflict semantics must remain frozen in the first performance repair.

## Exact failing boundary

**Boundary:** `ConflictResolver._record_shared_context_for_stored_memory` → `_insert_cross_user_conflicts`.

**Classification:** implementation-level persistence inefficiency plus a separate architectural candidate-growth risk.

Not confirmed as causes:

- PostgreSQL connection exhaustion;
- PostgreSQL blocking locks;
- extraction/provider time;
- proxy-user statistics refresh;
- read-context transaction ownership;
- Redis, Qdrant, or outbox processing.

## One isolated repair proposal

### Indexed set-based cross-user conflict reconciliation

Keep conflict detection and automatic resolution semantics unchanged. Change only how the already-produced conflict candidates are reconciled and persisted:

1. Canonicalize each candidate's unordered memory pair in memory.
2. Fetch all existing pairs for the new memory and involved entity types in one query.
3. Add only missing rows.
4. Flush the batch once, then run the existing resolver for exactly those inserted rows.
5. Add PostgreSQL indexes for both memory orientations scoped by tenant/entity type; add a canonical unordered-pair uniqueness invariant if PostgreSQL expression-index compatibility is verified during the experiment.

This removes the N+1 existence checks and row-by-row flushes without changing which conflicts are detected, how winners are chosen, or how memories/claims are archived.

The broader semantic-slot/candidate-bounding issue should be benchmarked separately only after this semantics-preserving repair is measured.

## Frozen acceptance criteria for the proposed experiment

- Conflict candidates and resolution actions identical to the current frozen coordination/conflict baselines.
- Conflict detection accuracy: 100% on registered scenarios.
- Winner correctness and single-winner correctness: 100%.
- Duplicate conflict rows for an unordered memory pair: 0, including concurrent delivery.
- Cross-user conflict existence-query count reduced from `O(C)` to at most one reconciliation query per stored memory.
- Conflict persistence flush count reduced from `O(C)` to one batch flush per stored memory.
- Durable-write transaction p95 improves by at least 25% from 23.768 s under the unchanged frozen MODERATE workload.
- Durable-write p99 does not regress from 29.913 s.
- API error rate and dropped arrivals materially improve; all established correctness invariants remain 100%.
- Existing unit, PostgreSQL integration, coordination, conflict, FAST, and post-load gates remain green.
- No holdout access and zero paid-provider cost.

If this experiment fails, revert it and investigate semantic candidate bounding separately. Do not combine that investigation with this persistence repair.
