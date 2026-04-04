# MemoryOS TypeScript SDK

```bash
npm install @memoryos/sdk
```

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
import { MemoryOS } from "@memoryos/sdk";

const client = new MemoryOS("mem_live_xxx", "https://api.memoryos.io");

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

`@memoryos/sdk` is a strict, zero-runtime-dependency client for the MemoryOS API.

- Native `fetch` only
- Works in Node.js 18+ and modern browsers
- Built-in retries for `429` and `5xx`
- Typed inputs, outputs, and errors
- ESM and CJS bundles

## Quickstart

1. Create an API key in MemoryOS.
2. Install with `npm install @memoryos/sdk`.
3. Import `MemoryOS`.
4. Construct the client with your API key.
5. Override `baseUrl` only for local or self-hosted environments.
6. Call `add()` with the tenant's `externalUserId`.
7. Check `result.status` and `result.quotaMode` to handle `blocked` or `passthrough` cleanly.
8. Call `get()` with the same `externalUserId`.
9. Use `retrieveResult.systemPromptAddition` only when `retrieveResult.quotaMode !== "PASSTHROUGH"`.
10. Call `list()` to paginate stored memories.
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
