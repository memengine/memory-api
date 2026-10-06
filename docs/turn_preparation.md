# Current-turn preparation (default off, source checkout)

`POST /v1/memories/prepare-turn` registers one incoming user turn **before**
calling the existing tenant retrieval path. It closes the retrieve-before-write
ordering gap; it does not synchronously understand, store, or resolve that turn.
Existing `/add`, `/retrieve`, `add()`, and `get()` behavior is unchanged.

## Gates and boundaries

The operator must enable `RUNTIME_TURN_PREPARATION_ENABLED` (default `false`),
the existing runtime preference gate and tenant allowlist, and explicit
preferences. The selected tenant must have a valid operator-managed runtime
catalog. A disabled rollout returns HTTP 409, not a legacy read.

This tenant API requires both API-key `read` and `write` permissions, or `admin`.
The customer backend supplies its authenticated user's `external_user_id`;
browser input must not choose another user's identity. Tenant and owner binding
are enforced by the backend. Agent-bound and historical reads are unsupported.
No MCP, universal API, application UI, or deployment is changed by this slice.

## Request

```json
{
  "external_user_id": "customer-user-123",
  "conversation_id": "customer-chat-456",
  "turn_id": "customer-message-789",
  "message": "Python instead",
  "query": "Which language should this example use?",
  "limit": 10,
  "format": "bullets",
  "context_max_tokens": 500
}
```

Required fields are `external_user_id`, `conversation_id`, `turn_id`, `message`,
and `query`. The SDK defaults `query` to `message`. Message size is bounded to
16,000 characters; conversation/turn IDs to 255 nonblank characters. Additional
read options are the existing `categories`, `time_filter_days`, and
`preference_context`. Applicability comes from the customer application or an
explicit user clarification, never guessed from a query by this operation.
Unknown top-level request fields are rejected.

Optional `preceding_assistant` accepts one bounded existing conversation-message
object with `role: "assistant"`, `source_kind: "assistant_output"`, and
`content`. Supply the actual preceding assistant text when a reply refers to it;
do not insert tool/document text or fabricate a proposal. This role declaration
is not independently attested. The incoming user turn is marked
`source_kind: "client_assertion"`. Registration never raises its authority.
Normal writer/evidence checks still apply; currently ineligible registered
source-writer paths return HTTP 409 rather than bypassing those checks.

## Response and retries

The response reuses the complete retrieve response and adds required `admission`:

```json
{"state": "accepted", "job_id": "c43c3c09-04a7-5b56-a6fb-03e1306d58fb", "reason": null}
```

| State | Meaning |
| --- | --- |
| `accepted` | Job, source event, evidence ledger and runtime guard committed before this read. |
| `replayed` | The exact same owned turn was already committed; no second job or dispatch. |
| `rejected` | No admission, e.g. budget, rate or queue capacity refusal. No job ID. |
| `unavailable` | No verified admission, e.g. dependency/catalog failure. No job ID. |

Admission is separate from `memory_visibility`. Accepted/replayed does **not**
mean processing completed or context is usable. Pending unknown meaning can
return `data: []` with `memory_visibility.state: "processing"` and scope
`user_learned_context`. A read failure after admission returns the committed job
ID plus empty context and visibility `unavailable`. Rejected/unavailable
admission also returns empty context with visibility `unavailable`. Typed status
must be checked independently of the prompt notice and token budget.
An HTTP/network error is not a usable read result.

Reuse the same conversation ID, turn ID, message and preceding assistant object
after timeout or network failure. The backend binds a deterministic job UUID to
tenant, owner, conversation and turn, plus a private normalized transcript hash.
The minimal hash survives raw payload redaction. A changed transcript under the
same IDs returns `turn_payload_mismatch` (HTTP 409). Query/read options are not
part of the transcript binding and may change on an exact replay. Concurrent
same-turn admission uses a nonblocking PostgreSQL transaction lock; contention
returns `turn_admission_in_progress` (HTTP 409), so retry those same IDs later.
Retries still pass rate/budget/auth gates and are not an unmetered bypass.

Do not issue a second `add()` for the same turn after calling this operation.
Do not catch a refusal and automatically retry using ordinary `get()` or a new
turn ID. Such fallback would defeat the ordering guarantee. A customer agent
can continue from the live conversation without unavailable learned context;
MemoryOS does not prescribe its reply text or add a separate clarification UI.

## Critical path and recovery

Admission uses existing rate/budget checks, SQL job/evidence persistence, and
Redis queue capacity reservation. It skips lexical quality rejection, semantic
deduplication, embeddings, classifiers, worker polling, and broker transport on
the admission critical path. A short reply such as "No" is a turn to register,
not a low-quality memory to discard. The subsequent retrieval uses the existing
guards and may do ordinary read work if the worker already applied the turn.

After the HTTP response, a background task attempts compact broker dispatch.
Broker failure leaves the durable queued job and guard intact. The existing
watchdog can recover an unleased queued job after its grace period; worker job
claims still handle duplicate delivery. This is not exactly-once transport.
Usage notification is best-effort background dispatch for newly admitted turns;
replays do not repeat it. This slice does not add an atomic budget reservation,
durable billing outbox, or synchronous budget alert delivery. No new table,
migration, cache acknowledgement, or parallel extraction pipeline is added.

## Deliberate release limit

This feature remains off. Registering every incoming question can temporarily
withhold all learned context until meaning is known, including an ordinary
question that changes nothing. The contract prevents stale reads but does not
by itself establish an acceptable conversational availability/latency policy.
No production p99, throughput, model quality, or assistant acceptance result is
claimed. A separate measured semantic/current-turn policy and integration
evaluation are needed before a production chat rollout; do not enable this
flag as a shortcut to that work.

## Local verification (2026-10-06)

The isolated PostgreSQL/Redis checks used synthetic identities and controlled
semantic responses, not live providers or blind holdouts:

- 2,411 tracked unit tests passed.
- 444 governance/runtime SQL integration tests passed.
- Final focused endpoint/worker, SDK, permission and failure checks: 171 passed.
- TypeScript installed-package ESM/CJS contracts: 52 passed; consumer type checks passed.
- FAST regression gate: 9/9 passed; reported provider cost $0.
- Scoped Python SDK type checks and fatal-error/undefined-name lint passed.

The backend type check still reports 15 pre-existing errors in requests/router
code; a shadow-file check of the previous commit reported 26. This is not a
claim that the entire backend type check is clean. These checks validate
deterministic contracts and failure boundaries, not semantic quality, live
assistant behavior, production load capacity, or response-time targets.
