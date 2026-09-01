# Multi-agent coordination phase closeout v4

## Closeout result

The complete frozen 17-scenario coordination runner was rerun against the current backend. Its
raw result is **11/17 (64.71%)**. Two of the six raw failures are stale parent-scenario outcomes
superseded by the subsequently approved governed-retirement slice, which passes **12/12**.

Therefore the integrated phase status is:

- **13/17 coordination capabilities confirmed passing**
- **4/17 genuinely unresolved architecture capabilities**
- no cross-agent, cross-user or cross-tenant leakage
- zero duplicate active revisions
- final focused regression suite: **22 passed**
- fixtures cleaned; holdout untouched

No production behavior was changed during this closeout.

## Confirmed passing areas

- direct conflict detection and compatible coexistence;
- equal-authority dispute behavior and existing-winner preservation;
- concurrent same-claim serialization and single winner;
- sequential and concurrent durable event idempotency;
- distinct-event observations remain allowed;
- revocation before execution and during execution;
- atomic retry after failure;
- governed agent retirement, provenance tombstone, shared continuity and Qdrant consistency;
- privacy deletion remains physically destructive and separate;
- tenant/user/agent authorization isolation.

## Raw failures superseded by an accepted dedicated baseline

1. `delete_agent_private_memories`
2. `provenance_after_agent_delete`

The parent runner still encodes physical-deletion-era expectations. The approved retirement
baseline establishes the explicit policy: Passport memories are user-owned and grant-governed;
retirement preserves the agent tombstone/source provenance and makes non-granted categories
inaccessible. Both capabilities pass in
`originating-agent-retirement-development-v2.json`.

## Four genuine remaining failures

### 1. Out-of-order events

Universal claim revisions have no durable event/observed timestamp used in winner selection.
Arrival order governs processing, so delayed older events cannot be resolved temporally.
Boundary: universal event provenance schema and claim resolver.

### 2. Older higher-authority evidence

Universal claims/revisions have no authority priority or source-authority policy. An older
authoritative agent cannot outrank newer lower-authority evidence because neither authority nor
event time participates in the decision.
Boundary: universal claim schema and resolver contract.

### 3. Cross-agent correction

A changed value from another Passport agent is disputed and archived. Only `user_correction`
has explicit winner-replacement semantics. There is no governed authorized agent-correction
operation.
Boundary: universal claim transition policy.

### 4. Shared-memory update

For the same reason, an authorized agent cannot update the current shared value into one new
active winner; the changed value becomes a disputed historical assertion.
Boundary: universal shared-memory update API and claim transition policy.

These are related but should not be bundled blindly. Event-time/authority resolution is one
architectural problem; governed cross-agent update/supersession is another product-policy problem.

## Phase decision

Close the implemented coordination reliability/security work as stable. Do not claim the four
remaining capabilities as supported. The next change, if approved later, should begin with a
written authority/event-time policy because adding fields without a winner policy would not make
out-of-order behavior correct. Cross-agent correction should remain a separate subsequent design.

Artifacts:

- raw rerun: `artifacts/internal-benchmarks/multi-agent-coordination-closeout-v4.json`
- retirement evidence: `artifacts/internal-benchmarks/originating-agent-retirement-development-v2.json`
