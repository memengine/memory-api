# MemoryOS TypeScript SDK

```bash
npm install memoryo-sdk@0.1.3
```

## Connect to MemoryOS

Follow the [quickstart](../../README.md#quickstart) to start the API and save your first memory. Set `MEMORYOS_API_URL` to your server URL and `MEMORYOS_API_KEY` to your application key. The Docker setup uses `http://127.0.0.1:18000`.

## Start simple: solo builders and small teams

> **For whom:** solo developers, MVPs, small SaaS apps, and single-agent products.
> You do **not** need `eventId`, `runId`, service writers, or authority rules to start. MemoryOS creates safe internal source metadata automatically.

```ts
import { MemoryOS } from "memoryo-sdk";

const mem = new MemoryOS(process.env.MEMORYOS_API_KEY!, process.env.MEMORYOS_API_URL!);

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

if (context.memoryVisibility) {
  throw new Error("Memory visibility hold; stop before answering.");
}
const systemPrompt = context.hasContext
  ? `${BASE_SYSTEM_PROMPT}\n\n${context.systemPromptAddition}`
  : BASE_SYSTEM_PROMPT;
```

This is the recommended first integration. Add source metadata only when more than one backend service writes memories for the same users.

### Read withholding status

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

This adds transport support, not scope inference. On an operator-enabled tenant
(`RUNTIME_PREFERENCE_DISCOVERY_ENABLED`, default false), ordinary current reads
can discover declared keys from existing search nominations and the tenant catalog.
A relevant property with scoped bindings and no application scope is withheld:
`result.preferenceScopeRequest` (`PreferenceScopeRequest`) contains
`state: "application_scope_required"`, `propertyKeys`, and `availableScopes`.
Ask this question in your existing chat; send the explicitly selected application
scope and returned keys in `preferenceContext` on the next read. Never default to
global or infer scope from query wording. This is not a value write or permission
change, and the typed question survives zero prompt budget. With discovery off,
generic reads still exclude declared preferences. No automatic polling or extra
read-time model call is added. Source changes are not a published package release.

## Multi-service mode: support, billing, CRM, and product services

> **For whom:** teams where multiple services can write facts about the same user.
> Use this when you need auditability, deduplication, conflict handling, or source-of-truth routing.

After registering the service writers in the Tenant Dashboard, teams can pass their registered `source.service` keys:

```ts
import { MemoryOS } from "memoryo-sdk";

const mem = new MemoryOS(process.env.MEMORYOS_API_KEY!, process.env.MEMORYOS_API_URL!);

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

const client = new MemoryOS(process.env.MEMORYOS_API_KEY!, process.env.MEMORYOS_API_URL!);

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

if (result.memoryVisibility) {
  throw new Error("Memory visibility hold; stop before answering.");
} else if (result.isPassthrough) {
  // MemoryOS quota exhausted or system degraded
  // Your AI still works — just without memory context
  systemPrompt = BASE_SYSTEM_PROMPT;
} else if (result.isDegraded) {
  // Degraded does not itself establish currentness; apply your answer policy.
  systemPrompt = BASE_SYSTEM_PROMPT + result.systemPromptAddition;
} else {
  // Full memory context
  systemPrompt = BASE_SYSTEM_PROMPT + result.systemPromptAddition;
}

// Call the model only after holds and application answer policy have been checked.
const response = await llm.complete(systemPrompt, userMessage);
```

```ts
const result = await client.add(messages, "...");

if (result.processingStatus === "delayed") {
  // Accepted work is delayed; completion and applied state remain unverified.
  // ETA in seconds:
  const eta = result.processingEtaSeconds;
  // Show truthful pending status where the application needs it.
}
```
