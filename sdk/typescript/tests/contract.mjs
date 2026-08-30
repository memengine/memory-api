import assert from "node:assert/strict";
import test from "node:test";

import { MemoryOS, UniversalMemoryOS } from "../dist/index.js";

test("add forwards Idempotency-Key without adding it to the body", async () => {
  let captured;
  const fetchImpl = async (url, init) => {
    captured = { url, init };
    return new Response(JSON.stringify({
      job_id: "job-123",
      status: "queued",
      request_id: "request-123",
      timestamp: "2026-08-29T00:00:00Z",
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const client = new MemoryOS("mem_test", MemoryOS.DEFAULT_BASE_URL, 30_000, fetchImpl);

  await client.add(
    [{ role: "user", content: "I prefer concise answers." }],
    "customer-123",
    undefined,
    undefined,
    undefined,
    "event-123",
  );

  assert.equal(MemoryOS.DEFAULT_BASE_URL, "https://api.memoryo.dev");
  assert.equal(captured.init.headers["Idempotency-Key"], "event-123");
  assert.equal("idempotency_key" in JSON.parse(captured.init.body), false);
});

test("get sends asOf and preserves clarificationQuestion", async () => {
  let captured;
  const fetchImpl = async (url, init) => {
    captured = { url, init };
    return new Response(JSON.stringify({
      retrieval_id: "retrieval-123",
      data: [],
      cached: false,
      system_prompt_addition: "",
      clarification_question: "Which plan should be current?",
      request_id: "request-123",
      timestamp: "2026-08-29T00:00:00Z",
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const client = new MemoryOS("mem_test", MemoryOS.DEFAULT_BASE_URL, 30_000, fetchImpl);

  const result = await client.get({
    query: "What plan was active?",
    externalUserId: "customer-123",
    asOf: "2026-08-01T12:00:00Z",
  });

  assert.equal(JSON.parse(captured.init.body).as_of, "2026-08-01T12:00:00Z");
  assert.equal(result.clarificationQuestion, "Which plan should be current?");
});

test("getJobStatus and waitForJob expose the asynchronous write lifecycle", async () => {
  let calls = 0;
  const fetchImpl = async (url, init) => {
    calls += 1;
    assert.match(url, /\/v1\/memories\/jobs\/job%2F123$/);
    assert.equal(init.method, "GET");
    const completed = calls > 1;
    return new Response(JSON.stringify({
      data: {
        job_id: "job/123",
        status: completed ? "completed" : "processing",
        memories_created: completed ? 1 : 0,
        attempts: 1,
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const client = new MemoryOS("mem_test", MemoryOS.DEFAULT_BASE_URL, 30_000, fetchImpl);

  const first = await client.getJobStatus("job/123");
  assert.equal(first.succeeded, false);
  const completed = await client.waitForJob("job/123", { timeoutMs: 100, pollIntervalMs: 1 });
  assert.equal(completed.succeeded, true);
  assert.equal(completed.memoriesCreated, 1);
});

test("list scopes by external user and export uses tenant proxy-user route", async () => {
  const captured = [];
  const fetchImpl = async (url) => {
    captured.push(url);
    if (url.includes("/export")) {
      return new Response(JSON.stringify({
        data: { tenant_id: "tenant-123", proxy_user_id: "proxy-123", memories: [] },
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return new Response(JSON.stringify({
      data: [],
      pagination: { next_cursor: null, limit: 50, total: 0 },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const client = new MemoryOS("mem_test", MemoryOS.DEFAULT_BASE_URL, 30_000, fetchImpl);

  await client.list("customer-123");
  const exported = await client.export("customer/123");

  assert.match(captured[0], /external_user_id=customer-123/);
  assert.match(captured[1], /\/v1\/users\/customer%2F123\/export$/);
  assert.equal(exported.tenantId, "tenant-123");
  assert.equal(exported.proxyUserId, "proxy-123");
});

test("universal client preserves provenance and sends supported options", async () => {
  let captured;
  const fetchImpl = async (url, init) => {
    captured = { url, init };
    if (url.endsWith("/add")) {
      return new Response(JSON.stringify({ job_id: "job-u", status: "queued" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(JSON.stringify({
      retrieval_id: "retrieval-u",
      data: [{
        id: "memory-u",
        content: "User prefers concise answers.",
        category: "preference",
        importance_score: 7,
        last_accessed: null,
        relevance_score: 0.9,
        context_snippet: "User prefers concise answers.",
        source_event_id: "event-u",
        provenance: { source: "chat" },
      }],
      cached: false,
      system_prompt_addition: "context",
      context_token_count: 12,
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const client = new UniversalMemoryOS("agent-key", "uui-token", undefined, 30_000, fetchImpl);

  await client.add([{ role: "user", content: "Remember this." }], {}, "universal-event-123");
  assert.equal(JSON.parse(captured.init.body).idempotency_key, "universal-event-123");
  const result = await client.get("preferences", 5, { format: "json", contextMaxTokens: 900 });
  const body = JSON.parse(captured.init.body);
  assert.equal(body.format, "json");
  assert.equal(body.context_max_tokens, 900);
  assert.equal(result.retrievalId, "retrieval-u");
  assert.equal(result.contextTokenCount, 12);
  assert.deepEqual(result.items[0].provenance, { source: "chat" });
  assert.equal(result.items[0].sourceEventId, "event-u");
});

test("delete supports the tenant-scoped call and legacy signature", async () => {
  const captured = [];
  const fetchImpl = async (url) => {
    captured.push(url);
    return new Response(JSON.stringify({ data: { deleted: true } }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  const client = new MemoryOS("mem_test", MemoryOS.DEFAULT_BASE_URL, 30_000, fetchImpl);

  assert.equal(await client.delete("memory-1", true), true);
  assert.equal(await client.delete("memory-2", "customer-123", false), true);
  assert.match(captured[0], /memory-1\?hard_delete=true$/);
  assert.match(captured[1], /memory-2\?hard_delete=false$/);
});
