# MemoryOS TypeScript SDK

```bash
npm install memoryo-sdk
```

## Start simple: solo builders and small teams

> **For whom:** solo developers, MVPs, small SaaS apps, and single-agent products.
> You do **not** need `eventId`, `runId`, service writers, or authority rules to start. MemoryOS creates safe internal source metadata automatically.

```ts
import { MemoryOS } from "memoryo-sdk";

const mem = new MemoryOS(process.env.MEMORYOS_API_KEY!);

const write = await mem.add(
  [
    { role: "user", content: "I prefer short answers when debugging." },
    { role: "assistant", content: "Got it. I will keep debugging replies concise." },
  ],
  "user_123",
);

if (!write.jobId) throw new Error(`Memory write was not queued: ${write.status}`);
const job = await mem.waitForJob(write.jobId);
if (!job.succeeded) throw new Error(`Memory write failed: ${job.errorSummary ?? job.status}`);

const context = await mem.get(
  "How should I answer this user?",
  "user_123",
);

const systemPrompt = context.hasContext
  ? `${BASE_SYSTEM_PROMPT}\n\n${context.systemPromptAddition}`
  : BASE_SYSTEM_PROMPT;
```

This is the recommended first integration. Add source metadata only when more than one backend service writes memories for the same users.

### Read withholding status (source checkout)

`get()` preserves `result.memoryVisibility` as `MemoryVisibility`, or `null`
when the server supplies no hold. Its `state` is `processing` or `unavailable`;
its `scope` is `user_learned_context` or `affected_context`. Older responses
without `scope` default to the conservative whole learned-context scope.
Malformed/unknown status values raise `MemoryOSError` with
`code: "SDK_INVALID_RESPONSE"`; they are not silently discarded.

`isDegraded` preserves the backend flag even when `quotaMode` is `FULL`.
A healthy circuit and available quota do not establish currentness. For an
`affected_context` hold, unaffected eligible items may remain. The backend notice
is preserved and typed status survives a zero prompt budget. `hasContext` only
means a prompt addition exists; that can be a withholding notice with no memory
items. It is not a clarification request or confirmation of storage.

This adds transport support, not scope inference: generic reads still exclude
declared preferences. Continue using explicit `preferenceContext` with
application-owned scope and property keys. There is no automatic polling, new
model call, assistant-specific rule, reset, or authority decision. These changes
are in source, not a claim that a new package has been published.

## Opt-in scoped preferences (0.1.2 release candidate)

This source checkout exposes an application-declared contract, **not automatic
natural-language scope/uncertainty recognition**. These fields are included in
the `0.1.2` source/tarball, not the published `0.1.1` package. Preparing a tarball
does not publish a release. The backend feature remains disabled by default; disabled
requests fail with `409 / PREF_409` rather than falling back to unscoped writes.

```ts
const applicability = { kind: "context", namespace: "project", key: "release-check" } as const;
const quote = "For Release Check, use Python for examples.";
const write = await mem.add({
  externalUserId: "user_123",
  messages: [{ role: "user", content: quote }],
  preferenceClaims: [{
    propertyKey: "examples.language", value: "Python", applicability,
    turnIndex: 0, quote,
  }],
});
// Queued, not confirmed storage. Check status separately, not during streaming.
const context = await mem.get({
  externalUserId: "user_123", query: "Which language should examples use?",
  preferenceContext: { applicability, propertyKeys: ["examples.language"] },
});
```

Existing positional `add()` and `get()` calls remain supported. Omitted preference
fields stay omitted. When declarations are supplied, the job processes only those
declarations, not other facts in the transcript. Property/scope keys must be stable
and application-owned; never guess unknown scope as global. Declarations stay
`client_asserted`, authority capped at 20. Do not combine them with `source` or
`agentId`, or explicit reads with `asOf`. Types are not runtime security checks;
the backend validates evidence, limits, permissions and authority.

For intentional replacement, use `commitment: "replacement"`, `replacesMemoryId`
and the exact string at `provenance.preference_version` returned by the backend.
Tentative uncertainty stays pending without withholding an existing default;
review actions never activate candidate text. Existing job status
preserves raw `extractionMetadata` and provides `sourceReviewsCapacityRejected`;
completion alone does not mean storage. Immediate reads may precede job completion.
There is no automatic polling, new model call, authority decision, or read-after-write
guarantee in the SDK. See [the full contract](../../docs/explicit_preference_contract.md).

### An explicitly unresolved current choice

The `0.1.2` types also accept `choiceState: "current_choice_unresolved"`
with `commitment: "uncertain"`, known applicability, and both existing-winner
references. This is an application declaration, not automatic interpretation of
the user's wording. Merely tentative interest is not a reason to withhold a default.

```ts
// currentId/currentVersion come from an owned backend management or prior
// retrieval result; never fabricate them from assistant text.
const undecidedQuote = "For Release Check, I am undecided between my current C++ default and Python.";
const held = await mem.add({
  externalUserId: "user_123", messages: [{ role: "user", content: undecidedQuote }],
  preferenceClaims: [{
    propertyKey: "examples.language", value: "Python", applicability,
    turnIndex: 0, quote: undecidedQuote, commitment: "uncertain",
    choiceState: "current_choice_unresolved",
    replacesMemoryId: currentId, replacesVersion: currentVersion,
  }],
});
```

The backend validates evidence, exact identity, ownership, currentness, authority
and version. JavaScript can bypass TypeScript shapes; the SDK does not silently
repair invalid declarations or strip fields after server rejection. A queued
write is not confirmed withholding: raw job `extractionMetadata.preference_choices`
records processing outcomes. Poll only when your workflow explicitly needs that
confirmation, not automatically while generating an answer.

Tenant `get()` returns exported `PreferenceReadDecision` entries in
`context.preferenceDecisions` (an empty array from old servers without the field)
and preserves `context.clarificationQuestion`. The result type's field is optional
for compatibility with existing consumers constructing result objects. Check it
independently of `context.hasContext` and `context.items`: unresolved choice can
be the only information, or accompany usable unrelated facts.

```ts
const reviewed = await mem.get({
  externalUserId: "user_123", query: "Which language should examples use?",
  preferenceContext: { applicability, propertyKeys: ["examples.language"] },
});
for (const decision of reviewed.preferenceDecisions ?? []) {
  // Pass this structured outcome to your existing chat workflow. The SDK
  // does not render UI, change an agent prompt, choose a value or resolve it.
  console.log(decision.propertyKey, decision.applicability, decision.state);
}
```

Review dismissal/expiry cannot restore a withheld default. A matching
`keep_current` review can reaffirm an existing valid value; an exact version-bound
replacement can resolve the choice. Neither raises authority or activates
candidate text. This capability remains unpublished and disabled by default,
and does not add automatic semantic admission.

### Validate a release before publication

From `sdk/typescript`, after installing development dependencies:

```bash
npm run typecheck
npm run test:package
```

`test:package` builds via the real `prepack` lifecycle, packs the release, and
installs that tarball into a disposable consumer offline. It reuses the existing
contract tests through the public package name in ESM and CommonJS, and checks
positive/negative preference type fixtures with Node16, NodeNext and Bundler
resolution. It also rejects unexpected package files or runtime dependencies.
The disposable consumer and tarball are removed after testing; nothing is published.

For a tarball to hand off, run `npm pack` after those checks. Packing rebuilds
`dist` so an old local bundle cannot silently become the release. Publication
also runs the typecheck and installed-package gate in `prepublishOnly`.
Both lifecycles follow [npm's documented script ordering](https://docs.npmjs.com/cli/v11/using-npm/scripts/).

These are client packaging/contract checks against controlled responses, not
proof of live extraction accuracy or permission to enable backend features.
Consumers must upgrade their dependency explicitly; publishing a release does
not update a deployed assistant automatically.

## Multi-service mode: support, billing, CRM, and product services

> **For whom:** teams where multiple services can write facts about the same user.
> Use this when you need auditability, deduplication, conflict handling, or source-of-truth routing.

After registering the service writers in the Tenant Dashboard, teams can pass their registered `source.service` keys:

```ts
import { MemoryOS } from "memoryo-sdk";

const mem = new MemoryOS(process.env.MEMORYOS_API_KEY!);

await mem.add(
  [{ role: "assistant", content: "Customer is on the Growth plan." }],
  "cust_123", // externalUserId: your stable customer/user id
  "support-bot", // agentId: your app or AI agent id
  { ticketId: "TCK-8842", channel: "billing" }, // metadata: optional app context
  MemoryOS.source("billing-service", {
    eventId: "invoice_evt_8842",
    scope: { workspaceId: "ws_123" },
    evidence: [{ sourceType: "billing_record", reference: "invoice_evt_8842" }],
  }),
);

await mem.add(
  [{ role: "assistant", content: "Customer support previously saw the Starter plan." }],
  "cust_123", // same externalUserId, so MemoryOS can compare facts
  "support-bot",
  { ticketId: "SUP-2109", channel: "support" },
  MemoryOS.source("support-service"), // eventId and observedAt are generated
);
```

Production teams should also bind dedicated API keys to those writers. Then Billing, Support, CRM, and other services can have different authority rules without changing the basic SDK flow.

## Webhook Signature Verification

```ts
import crypto from "node:crypto";

function verifyMemoryOSWebhook(
  body: string,
  signatureHeader: string,
  webhookSecret: string,
): boolean {
  const expected = crypto
    .createHmac("sha256", webhookSecret)
    .update(body, "utf8")
    .digest("hex");

  return crypto.timingSafeEqual(
    Buffer.from(expected, "utf8"),
    Buffer.from(signatureHeader, "utf8"),
  );
}

const signature = request.headers["x-memoryos-signature"];
if (!signature || !verifyMemoryOSWebhook(rawBody, signature, WEBHOOK_SECRET)) {
  throw new Error("Invalid MemoryOS webhook signature");
}
```

```ts
import { MemoryOS } from "memoryo-sdk";

const client = new MemoryOS("mem_live_xxx", "https://api.memoryo.dev");

const result = await client.add(
  [{ role: "user", content: "I prefer Python and FastAPI" }],
  "student_44821",
);

const memories = await client.get(
  "programming preferences",
  "student_44821",
  3,
);

console.log(result.status, result.quotaMode);
console.log(memories.quotaMode);
console.log(memories.items[0]?.content ?? "No memories found");
```

`memoryo-sdk` is a strict, zero-runtime-dependency client for the MemoryOS API.

- Native `fetch` only
- Works in server-side Node.js 18+
- Built-in retries for `429` and `5xx`
- Typed inputs, outputs, and errors
- ESM and CJS bundles

> **Security:** Keep tenant API keys on your server. Do not embed a MemoryOS API key in browser JavaScript or any `NEXT_PUBLIC_*` environment variable.

## Quickstart

1. Create an API key in MemoryOS.
2. Install with `npm install memoryo-sdk`.
3. Import `MemoryOS`.
4. Construct the client with your API key.
5. Override `baseUrl` only for local or self-hosted environments.
6. Call `add()` with the tenant's `externalUserId`.
7. Check `result.status` and `result.quotaMode` to handle `blocked` or `passthrough` cleanly.
8. Call `get()` with the same `externalUserId`.
9. Use `retrieveResult.systemPromptAddition` only when `retrieveResult.quotaMode !== "PASSTHROUGH"`.
10. Call `list()` to paginate stored memories.

`add().wasQueued` means extraction was queued, not that storage is complete.
Use `waitForJob()` and `job.createdMemoryIds` when you need exact created
records. `wasStored` remains as a deprecated compatibility alias.
`waitForJob()` continues polling through the backend's retryable `failed`
state and returns when the job completes or exhausts retries as `dead`.

When retrieval returns `result.clarification`, render its question in your
existing chat and send the user's selected answer with
`client.answerClarification(...)`. MemoryOS—not the model—validates identity,
expiry, and the conflict transition.

## Source reviews (0.1.1)

With source-merge containment deployed, reviews can also represent a blocked automatic
merge, not only uncertainty. The current memory remains active and the incoming candidate
is pending. This backend behavior uses the same SDK methods; it does not require a new
package version. A completed ingestion job can create zero memories. Read the job outcome
and present the returned review in your existing chat; do not report the merge as stored.

When recognized uncertainty has no usable alternative, `result.sourceReviews`
may contain a non-activating review. Present its question and allowed actions in
your existing chat. Submit the actual user's selection, never a model decision:

```ts
const review = result.sourceReviews?.[0];
if (review) {
  // userAction is the user's choice from review.actions in your chat.
  const resolution = await client.answerSourceReview({
    reviewId: review.id,
    externalUserId: "user_1",
    version: review.version,
    action: userAction,
  });
}
```

`keep_current` and `dismiss` close the pending interpretation without changing
stored memory or raising authority. `restate` leaves it pending and returns
`resolved: false`, `nextStep: "add_memory"`: obtain the user's actual new statement
and submit it through normal `add()` ingestion. It is not completed storage.
Reviews are bounded to three recent items, expire seven days after the server's
last observation, and are absent for historical retrieval. This fallback does
not fix semantic misclassification or suppress all pending-property context.

## Other operations

11. Call `delete()` to archive or hard-delete a memory.
12. Call `export()` to download the user export bundle.
13. All failures throw typed SDK errors.
14. `401` maps to `AuthError`.
15. `404` maps to `NotFoundError`.
16. `429` maps to `RateLimitError`.
17. See `examples/` for integration patterns.

## Handling Degraded Responses

```ts
const result = await client.get(
  "...",
  "...",
);

if (result.isPassthrough) {
  // MemoryOS quota exhausted or system degraded
  // Your AI still works — just without memory context
  systemPrompt = BASE_SYSTEM_PROMPT;
} else if (result.isDegraded) {
  // Serving from cache — fewer memories than usual
  // Still worth using
  systemPrompt = BASE_SYSTEM_PROMPT + result.systemPromptAddition;
} else {
  // Full memory context
  systemPrompt = BASE_SYSTEM_PROMPT + result.systemPromptAddition;
}

// Always call your LLM — never skip because of MemoryOS state
const response = await llm.complete(systemPrompt, userMessage);
```

```ts
const result = await client.add(messages, "...");

if (result.processingStatus === "delayed") {
  // Queue is busy — memory will be stored but takes longer
  // ETA in seconds:
  const eta = result.processingEtaSeconds;
  // Log it — do not surface to end user
}
```
