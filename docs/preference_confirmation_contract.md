# Preference confirmation: internal v2, source checkout

This is a review-first backend/SDK contract, not a production rollout or a
semantic/latency acceptance pass. Runtime capture and worker constructors default
to contract version **1**. Version 2 is an internal opt-in; its policy is saved
with the capture and reused on retries. Clients cannot select it in request JSON.
No assistant, MCP or tenant UI change is included.

## Proposal versus current memory

For a v2 finding such as “Could you make Python my default for every programming
example?”, the backend verifies the actual user role, eligible source, exact
quote, offered property and known scope. If the legacy question-only semantic
gate rejects an otherwise eligible `assert`/`set_current`, v2 can create a pending
review. It does not create a declaration, change the ledger, or activate Python.
Unsupported roles, fabricated spans and unknown identities are not repaired.
V1 and all ordinary writes retain their original evidence checks.

The pending record uses the existing candidate store. Confirming uses the existing
preference writer, version/archive rules and durable vector outbox. There is no
second writer and no generated “user said” evidence.

## API

Both endpoints require an authenticated tenant API key. An unknown external user
returns an empty GET list and cannot answer; a blocked user is refused. The GET
requires read permission; the POST requires write permission and an existing user.
The application must derive `external_user_id` from its authenticated session.

`GET /v1/memories/preference-reviews?external_user_id=user_123` returns an ordinary
API envelope with up to three pending, usable review objects in `data`:

```json
{
  "id": "a10bc57b-bb10-4995-8af3-6a94d81a18c1",
  "version": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "property_key": "examples.language",
  "applicability": {"kind": "global"},
  "current_value": "C++",
  "proposed_value": "Python",
  "question": "Change examples.language to Python in the displayed scope? This proposal is not current.",
  "actions": ["confirm", "keep_current", "dismiss"],
  "expires_at": "2026-10-14T10:00:00Z"
}
```

IDs and versions above are illustrative. Submit the actual returned values.
`current_value` can be null for a verified absent target. Applicability is global
or an exact context namespace/key; it is never inferred by the confirmation call.
If the current target has authority above 20, `confirm` is not offered.

`POST /v1/memories/preference-reviews/{review_id}/answer` has this closed body:

```json
{
  "external_user_id": "user_123",
  "version": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "action": "confirm"
}
```

The only actions are `confirm`, `keep_current`, and `dismiss`. There are no client
value, scope, authority or evidence override fields. The response envelope's
`data` contains `review_id`, `action`, `replayed`, and nullable `memory_id`.
Confirm returns the new memory ID, or the existing current ID for a same-value
deduplication. Non-confirm actions never promote text or change the preference.

Ordinary retrieval also returns `preference_reviews` (default empty). Existing
`source_reviews` and `/source-reviews/{id}/answer` remain separate and **never
activate text**. Do not send `confirm` to the source-review endpoint.

## Transaction and trust boundaries

Before embedding, normal admission and quota checks apply. The final write
rechecks tenant, user, API-key activity/write permission, exact review version,
retained immutable source/capture, original decision, operator catalog, target
version/currentness, known applicability, rollout gate and authority. It cannot
replace a target above authority 20. Review expiry is seven
days; expired confirmations are refused. An owned `dismiss` can cancel an expired
review without mutating memory.

Admission and embedding happen before the final write transaction; database
locks/connections are released before provider I/O. The final candidate, source
and target locks are nonblocking. Busy/stale/conflicting actions return 409, not
an unverified success. Memory write, pending resolution, provenance, audit and
outbox are committed together; audit failure rolls them all back. Exact repeated
action/version returns the stored receipt without another memory. A different
action for an already closed review returns 409. Cache delivery is best-effort
after commit; SQL currentness remains authoritative.

An API key authenticates the **customer application**, not a human independently.
The application must invoke this method only from a user action in its existing
authenticated chat, not give it to an agent as an autonomous tool. MemoryOS does
not claim cryptographic human-presence proof. Confirmation provenance explicitly
remains `client_asserted`, authority **20**, with the application-action event,
original job and external conversation ID. It is not user-confirmed authority 80.

## Bounded reads and deployment prerequisites

Pending eligible identities withhold affected preference context; unrelated facts
can remain available. Reads inspect at most nine pending rows: more than eight
causes broad unavailability rather than dropping unexamined holds. Up to three
usable cards are returned. Expiration never silently activates a proposal or
releases an unresolved hold. Dismissal or a genuinely changed target can release
it. Original-source retention failure never makes a confirmation usable.

The migration `preference_confirmation_index` adds a concurrent partial owner/
status/order index to the existing store. Apply migrations before deploying this
code. Capacity is bounded by the existing tenant/profile candidate row limits;
capacity failure does not silently drop a review and release a guard.

Python exposes `result.preference_reviews`, `list_preference_reviews(user_id)` and
`answer_preference_review(review_id, external_user_id=..., version=..., action=...)`.
The async client has the same methods. TypeScript exposes `preferenceReviews`,
`listPreferenceReviews(userId)` and
`answerPreferenceReview({reviewId, externalUserId, version, action})`.
The SDK transports backend decisions; it does not select an option, infer scope,
poll until promotion, or change the customer's assistant behavior. Keep the API
key server-only. Wire the returned review into the customer's existing chat.
These are source-checkout additions, not a package publication announcement.

Controlled schema, SDK and PostgreSQL tests establish these safety/transaction
boundaries only. No paid v2 semantic run, blind holdout, real-chat acceptance,
human calibration, p99 latency or production load qualification is claimed.
The frozen pilot acceptance checklist and prior latency NO-GO remain in force.

## Controlled verification (2026-10-07)

- 2,459 tracked/new unit tests passed, including unchanged v1 evidence behavior.
- 455 existing/new PostgreSQL governance regressions passed. The final dedicated
  confirmation module passed 19 cases, covering pending-only capture, explicit
  write/replay, scope isolation, ownership/blocking, expiry, higher authority,
  source retention, concurrent commands, permission/target changes during
  embedding, same-value deduplication, audit rollback and post-commit cache failure.
- The owner/status partial index bounded a read to nine rows with no sequential
  scan over 100,000 synthetic pending rows. This is query-plan evidence, not a
  p99 latency or production throughput measurement.
- TypeScript type/installed-package checks passed, with 56 ESM/CJS contracts.
  Scoped type checking of six changed Python modules and fatal/undefined-name
  lint passed. FAST passed 9/9, with zero provider spend.
- The index migration was applied to the task-owned disposable PostgreSQL DB.
  No customer database, live flags, packages, assistant, MCP or tenant UI changed.

These tests use controlled provider outputs, not live semantic accuracy evidence.
