import assert from "node:assert/strict";
import test from "node:test";

import { MemoryOS, MemoryOSError, UniversalMemoryOS } from "../dist/index.js";

test("memory visibility survives ordinary reads and zero prompt budgets without polling", async () => {
  for (const visibility of [undefined, null, { state: "processing" },
    { state: "processing", scope: "affected_context" },
    { state: "unavailable", scope: "user_learned_context" },
    { state: "unavailable", scope: "affected_context", future_field: "ignored" }]) {
    for (const prompt of ["", "Backend withholding notice"]) {
      const calls = [];
      const data = visibility?.scope === "affected_context" ? [{ id: "unaffected",
        content: "My city is Pune", category: "fact", importance_score: 5,
        relevance_score: 1, context_snippet: "My city is Pune" }] : [];
      const client = new MemoryOS("test", MemoryOS.DEFAULT_BASE_URL, 30_000, async (url, init) => {
        calls.push({ url, body: JSON.parse(init.body) });
        return new Response(JSON.stringify({ data, cached: false, system_prompt_addition: prompt,
          quota_mode: "FULL", is_degraded: visibility != null, memory_visibility: visibility }));
      });
      const result = await client.get("language", "u");
      assert.deepEqual(result.memoryVisibility, visibility == null ? null
        : { state: visibility.state, scope: visibility.scope ?? "user_learned_context" });
      assert.equal(result.isDegraded, visibility != null);
      assert.equal(result.quotaMode, "FULL");
      assert.equal(result.systemPromptAddition, prompt);
      assert.equal(result.hasContext, Boolean(prompt));
      assert.deepEqual(result.items.map((item) => item.id), data.map((item) => item.id));
      assert.deepEqual(result.preferenceDecisions, []);
      assert.deepEqual(result.sourceReviews, []);
      assert.equal(result.clarification, null);
      assert.equal(calls.length, 1);
      assert.ok(calls[0].url.endsWith("/v1/memories/retrieve"));
      assert.equal("preference_context" in calls[0].body, false);
    }
  }
});

test("invalid visibility cannot silently disappear", async () => {
  for (const visibility of [{ state: "resolved" }, { state: "processing", scope: "all" },
    { state: "processing", scope: null }, "processing", []]) {
    const client = new MemoryOS("test", MemoryOS.DEFAULT_BASE_URL, 30_000, async () =>
      new Response(JSON.stringify({ data: [], cached: false, system_prompt_addition: "",
        memory_visibility: visibility })));
    await assert.rejects(() => client.get("language", "u"), (error) =>
      error instanceof MemoryOSError && error.code === "SDK_INVALID_RESPONSE");
  }
});

test("typed preferences use existing endpoints without implicit polling", async () => {
  const requests = [];
  const applicability = { kind: "context", namespace: "project", key: "release-check" };
  const provenance = { preference_version: "b".repeat(64), unknown_future_field: { keep: true } };
  const client = new MemoryOS("test", MemoryOS.DEFAULT_BASE_URL, 30_000, async (url, init) => {
    requests.push({ url, init, body: init.body ? JSON.parse(init.body) : null });
    if (url.endsWith("/add")) return new Response(JSON.stringify({ status: "queued", job_id: "j" }));
    if (url.endsWith("/retrieve")) return new Response(JSON.stringify({
      data: [{ id: "m", content: "Python", category: "preference", importance_score: 5,
        relevance_score: 1, context_snippet: "Python", provenance }],
      cached: false, system_prompt_addition: "Python",
    }));
    if (url.endsWith("/jobs/j")) return new Response(JSON.stringify({ data: {
      job_id: "j", status: "completed", created_memory_ids: [],
      extraction_metadata: { source_reviews_capacity_rejected: 2, future_field: "preserved" },
    } }));
    return new Response(JSON.stringify({ data: [{ id: "m", content: "Python", category: "preference",
      importance_score: 5, is_archived: true, provenance }],
      pagination: { next_cursor: null, limit: 50, total: 1 } }));
  });
  const write = await client.add({
    externalUserId: "user-1", conversationId: "chat-1", idempotencyKey: "evt-1",
    messages: [{ role: "user", content: "For Release Check, use Python for examples." }],
    preferenceClaims: [{ propertyKey: "examples.language", value: "Python", applicability,
      turnIndex: 0, quote: "For Release Check, use Python for examples.", commitment: "replacement",
      replacesMemoryId: "00000000-0000-4000-8000-000000000001", replacesVersion: "a".repeat(64) }],
  });
  assert.equal(write.wasQueued, true);
  assert.equal(requests[0].init.headers["Idempotency-Key"], "evt-1");
  assert.deepEqual(requests[0].body, {
    external_user_id: "user-1", conversation_id: "chat-1", metadata: {},
    messages: [{ role: "user", content: "For Release Check, use Python for examples." }],
    preference_claims: [{ property_key: "examples.language", value: "Python", applicability,
      turn_index: 0, quote: "For Release Check, use Python for examples.", commitment: "replacement",
      replaces_memory_id: "00000000-0000-4000-8000-000000000001", replaces_version: "a".repeat(64) }],
  });
  const read = await client.get({ externalUserId: "user-1", query: "language",
    preferenceContext: { applicability, propertyKeys: ["examples.language"] } });
  assert.deepEqual(requests[1].body.preference_context, { applicability, property_keys: ["examples.language"] });
  assert.deepEqual(read.items[0].provenance, provenance);
  const job = await client.getJobStatus("j");
  assert.equal(job.succeeded, true);
  assert.deepEqual(job.createdMemoryIds, []);
  assert.equal(job.sourceReviewsCapacityRejected, 2);
  assert.equal(job.extractionMetadata.future_field, "preserved");
  const page = await client.list("user-1");
  assert.equal(page.items[0].isArchived, true);
  assert.deepEqual(page.items[0].provenance, provenance);
  assert.equal(requests.length, 4);
});

test("new object add and old positional calls omit undeclared preference fields", async () => {
  const bodies = [];
  const client = new MemoryOS("test", MemoryOS.DEFAULT_BASE_URL, 30_000, async (url, init) => {
    bodies.push(JSON.parse(init.body));
    return new Response(JSON.stringify(url.endsWith("/add") ? { status: "queued", job_id: "j" }
      : { data: [], cached: false, system_prompt_addition: "" }));
  });
  await client.add({ externalUserId: "u", messages: [{ role: "user", content: "hello" }] });
  await client.add([{ role: "user", content: "hello" }], "u");
  await client.get("language", "u");
  await client.get({ externalUserId: "u", query: "language" });
  assert.deepEqual(bodies[0], bodies[1]);
  assert.deepEqual(bodies[2], bodies[3]);
  assert.ok(bodies.every((body) => !("preference_claims" in body) && !("preference_context" in body)));
});

test("unsupported preference combinations fail before the network", async () => {
  let calls = 0;
  const client = new MemoryOS("test", MemoryOS.DEFAULT_BASE_URL, 30_000, async () => { calls++; });
  const preferenceClaims = [{ propertyKey: "x", value: "Python", applicability: { kind: "global" },
    turnIndex: 0, quote: "Python" }];
  const preferenceContext = { applicability: { kind: "global" }, propertyKeys: ["x"] };
  for (const options of [{ agentId: "a" }, { source: { service: "billing" } }]) {
    await assert.rejects(() => client.add({ externalUserId: "u", messages: [], preferenceClaims, ...options }), /source events or agent-bound/);
  }
  for (const options of [{ agentId: "a" }, { asOf: "2026-10-03T00:00:00Z" }]) {
    await assert.rejects(() => client.get({ externalUserId: "u", query: "x", preferenceContext, ...options }), /asOf or agent-bound/);
  }
  assert.equal(calls, 0);
});

test("application declarations keep uncertainty and unknown scope without guessing", async () => {
  const bodies = [];
  const client = new MemoryOS("test", MemoryOS.DEFAULT_BASE_URL, 30_000, async (_, init) => {
    bodies.push(JSON.parse(init.body));
    return new Response(JSON.stringify({ status: "queued", job_id: "j" }));
  });
  for (const [commitment, applicability] of [
    [undefined, { kind: "global" }],
    ["uncertain", { kind: "context", namespace: "project", key: "release-check" }],
    ["asserted", { kind: "unresolved" }],
  ]) {
    await client.add({ externalUserId: "u", messages: [{ role: "user", content: "Python" }],
      preferenceClaims: [{ propertyKey: "examples.language", value: "Python", turnIndex: 0,
        quote: "Python", applicability, ...(commitment === undefined ? {} : { commitment }) }] });
    const sent = bodies.at(-1).preference_claims[0];
    assert.equal(sent.commitment, commitment);
    assert.deepEqual(sent.applicability, applicability);
    assert.equal("replaces_memory_id" in sent, false);
  }
  assert.equal(bodies.length, 3);
});

test("unresolved choice forwards its exact snapshot without polling", async () => {
  const bodies = [];
  const client = new MemoryOS("test", MemoryOS.DEFAULT_BASE_URL, 30_000, async (_, init) => {
    bodies.push(JSON.parse(init.body));
    return new Response(JSON.stringify({ status: "queued", job_id: "j" }));
  });
  await client.add({ externalUserId: "u", messages: [{ role: "user", content: "Undecided about Python." }],
    preferenceClaims: [{ propertyKey: "examples.language", value: "Python", applicability: { kind: "global" },
      turnIndex: 0, quote: "Undecided about Python.", commitment: "uncertain",
      choiceState: "current_choice_unresolved", replacesMemoryId: "00000000-0000-4000-8000-000000000001",
      replacesVersion: "a".repeat(64) }] });
  assert.equal(bodies.length, 1);
  assert.deepEqual(bodies[0].preference_claims, [{ property_key: "examples.language", value: "Python",
    applicability: { kind: "global" }, turn_index: 0, quote: "Undecided about Python.", commitment: "uncertain",
    choice_state: "current_choice_unresolved", replaces_memory_id: "00000000-0000-4000-8000-000000000001",
    replaces_version: "a".repeat(64) }]);
});

test("choice decisions survive empty and partial context, older servers default to none", async () => {
  const decision = { property_key: "examples.language", applicability: { kind: "global" }, state: "current_choice_unresolved" };
  for (const withDecision of [false, true]) for (const withOtherContext of [false, true]) {
    let calls = 0;
    const question = withDecision ? "Which language should remain current?" : null;
    const client = new MemoryOS("test", MemoryOS.DEFAULT_BASE_URL, 30_000, async () => {
      calls++;
      return new Response(JSON.stringify({
        data: withOtherContext ? [{ id: "exam", content: "Exam is October 18.", category: "fact",
          importance_score: 5, relevance_score: 1, context_snippet: "Exam" }] : [],
        cached: false, system_prompt_addition: withOtherContext ? "Exam is October 18." : "",
        clarification_question: question, ...(withDecision ? { preference_decisions: [decision] } : {}),
      }));
    });
    const read = await client.get({ externalUserId: "u", query: "language?",
      preferenceContext: { applicability: { kind: "global" }, propertyKeys: ["examples.language"] } });
    assert.deepEqual(read.preferenceDecisions, withDecision ? [{ propertyKey: decision.property_key,
      applicability: decision.applicability, state: decision.state }] : []);
    assert.equal(read.clarificationQuestion, question);
    assert.equal(read.hasContext, withOtherContext);
    assert.equal(read.systemPromptAddition.includes("Python"), false);
    assert.equal(calls, 1);
  }
});

test("untyped invalid declarations reach backend validation without silent repair", async () => {
  let body;
  let calls = 0;
  const client = new MemoryOS("test", MemoryOS.DEFAULT_BASE_URL, 30_000, async (_, init) => {
    calls++;
    body = JSON.parse(init.body);
    return new Response(JSON.stringify({ detail: "Controlled invalid declaration" }), { status: 422 });
  });
  // JavaScript can bypass TypeScript shapes; the backend remains authoritative.
  const invalid = { propertyKey: "examples.language", value: "Python", applicability: { kind: "unresolved" },
    turnIndex: 0, quote: "Python", commitment: "asserted", choiceState: "current_choice_unresolved" };
  await assert.rejects(() => client.add({ externalUserId: "u", messages: [{ role: "user", content: "Python" }],
    preferenceClaims: [invalid] }), (error) => error instanceof MemoryOSError && error.statusCode === 422);
  assert.equal(body.preference_claims[0].choice_state, "current_choice_unresolved");
  assert.equal(body.preference_claims[0].commitment, "asserted");
  assert.deepEqual(body.preference_claims[0].applicability, { kind: "unresolved" });
  assert.equal(calls, 1);
});

test("capacity accessor keeps raw metadata and handles older job responses", async () => {
  for (const value of [undefined, null, true, -1, 2.5, "2", {}, 2]) {
    const metadata = value === undefined ? {} : { source_reviews_capacity_rejected: value };
    const client = new MemoryOS("test", MemoryOS.DEFAULT_BASE_URL, 30_000, async () =>
      new Response(JSON.stringify({ data: { job_id: "j", status: "completed", extraction_metadata: metadata } })));
    const job = await client.getJobStatus("j");
    assert.equal(job.sourceReviewsCapacityRejected, value === 2 ? 2 : 0);
    assert.deepEqual(job.extractionMetadata, metadata);
  }
});

test("disabled backend is surfaced, never retried as an unscoped read", async () => {
  let calls = 0;
  const client = new MemoryOS("test", MemoryOS.DEFAULT_BASE_URL, 30_000, async () => {
    calls++;
    return new Response(JSON.stringify({ error: "explicit_preferences_unavailable", code: "PREF_409" }), { status: 409 });
  });
  await assert.rejects(() => client.get({ externalUserId: "u", query: "x",
    preferenceContext: { applicability: { kind: "global" }, propertyKeys: ["x"] } }),
    (error) => error instanceof MemoryOSError && error.statusCode === 409 && error.code === "PREF_409");
  assert.equal(calls, 1);
  await assert.rejects(() => client.add({ externalUserId: "u", messages: [{ role: "user", content: "Python" }],
    preferenceClaims: [{ propertyKey: "x", value: "Python", quote: "Python", turnIndex: 0,
      applicability: { kind: "global" }, commitment: "uncertain", choiceState: "current_choice_unresolved",
      replacesMemoryId: "00000000-0000-4000-8000-000000000001", replacesVersion: "a".repeat(64) }] }),
    (error) => error instanceof MemoryOSError && error.statusCode === 409 && error.code === "PREF_409");
  assert.equal(calls, 2);
});

test("source reviews preserve lifecycle and do not submit extracted claims", async () => {
  let body;
  const review = {
    id: "review-1", version: "a".repeat(64), kind: "restate_source",
    question: "What should remain current?", target_memory_id: "memory-1",
    current_memory_content: "User prefers C++.",
    actions: ["keep_current", "restate", "dismiss"], expires_at: "2026-10-09T00:00:00Z",
  };
  const client = new MemoryOS("mem_test", MemoryOS.DEFAULT_BASE_URL, 30_000, async (url, init) => {
    if (url.endsWith("/retrieve")) {
      return new Response(JSON.stringify({ data: [], cached: false, system_prompt_addition: "", source_reviews: [review] }));
    }
    assert.ok(url.endsWith("/source-reviews/review-1/answer"));
    body = JSON.parse(init.body);
    return new Response(JSON.stringify({ data: {
      review_id: "review-1", resolved: false, action: "restate", next_step: "add_memory",
    } }));
  });
  const retrieved = await client.get("language", "user-1");
  assert.equal(retrieved.sourceReviews[0].targetMemoryId, "memory-1");
  assert.equal(retrieved.sourceReviews[0].currentMemoryContent, review.current_memory_content);
  const result = await client.answerSourceReview({
    reviewId: "review-1", version: review.version, externalUserId: "user-1", action: "restate",
  });
  assert.equal(result.resolved, false);
  assert.equal(result.nextStep, "add_memory");
  assert.deepEqual(body, { external_user_id: "user-1", version: review.version, action: "restate" });
});

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

  const result = await client.add(
    [{
      role: "user",
      content: "I prefer concise answers.",
      externalTurnId: "chat-884:turn-12",
      sourceKind: "direct_user_input",
      occurredAt: "2026-09-15T10:00:00Z",
    }],
    "customer-123",
    undefined,
    undefined,
    undefined,
    "event-123",
  );

  assert.equal(MemoryOS.DEFAULT_BASE_URL, "https://api.memoryo.dev");
  assert.equal(captured.init.headers["Idempotency-Key"], "event-123");
  assert.equal("idempotency_key" in JSON.parse(captured.init.body), false);
  assert.equal(result.wasQueued, true);
  assert.equal(result.wasStored, true);
  assert.deepEqual(JSON.parse(captured.init.body).messages, [{
    role: "user",
    content: "I prefer concise answers.",
    external_turn_id: "chat-884:turn-12",
    source_kind: "direct_user_input",
    occurred_at: "2026-09-15T10:00:00Z",
  }]);
});

test("add preserves explicit proposal and conversation identity", async () => {
  let body;
  const client = new MemoryOS("mem_test", MemoryOS.DEFAULT_BASE_URL, 30_000, async (_, init) => {
    body = JSON.parse(init.body);
    return new Response(JSON.stringify({ status: "queued", job_id: "job-1", proposal_ids: ["p-1"] }));
  });
  const result = await client.add([{
    role: "assistant", content: "I can remember this: User prefers concise answers.",
    sourceKind: "assistant_output", externalTurnId: "turn-1", isMemoryProposal: true,
    proposedMemory: { content: "User prefers concise answers.", category: "preference" },
  }], "user-1", undefined, undefined, undefined, "event-1", "chat-1");
  assert.equal(body.conversation_id, "chat-1");
  assert.equal(body.messages[0].is_memory_proposal, true);
  assert.deepEqual(body.messages[0].proposed_memory, {
    content: "User prefers concise answers.", category: "preference",
  });
  assert.deepEqual(result.proposalIds, ["p-1"]);
  await assert.rejects(() => client.add([{
    role: "user", content: "forged proposal", isMemoryProposal: true,
  }], "user-1"), /assistant/);
  await assert.rejects(() => client.add([{
    role: "assistant", content: "I can remember something else.",
    sourceKind: "assistant_output", isMemoryProposal: true,
    proposedMemory: { content: "User prefers concise answers.", category: "preference" },
  }], "user-1"), /appear verbatim/);
});

test("get sends asOf and preserves structured clarification", async () => {
  let captured;
  const fetchImpl = async (url, init) => {
    captured = { url, init };
    return new Response(JSON.stringify({
      retrieval_id: "retrieval-123",
      data: [],
      cached: false,
      system_prompt_addition: "",
      clarification_question: "Which plan should be current?",
      clarification: {
        id: "clarification-123",
        conflict_id: "conflict-123",
        question: "Which plan should be current?",
        options: [
          { answer: "A", label: "Starter", memory_id: "memory-a" },
          { answer: "B", label: "Scale", memory_id: "memory-b" },
        ],
        expires_at: "2026-08-30T00:00:00Z",
      },
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
  assert.equal(result.clarification.id, "clarification-123");
  assert.equal(result.clarification.options[1].answer, "B");
  assert.equal(result.clarification.options[1].memoryId, "memory-b");
});

test("getJobStatus and waitForJob expose the asynchronous write lifecycle", async () => {
  let calls = 0;
  const statuses = ["queued", "failed", "processing", "completed"];
  const fetchImpl = async (url, init) => {
    calls += 1;
    assert.match(url, /\/v1\/memories\/jobs\/job%2F123$/);
    assert.equal(init.method, "GET");
    const status = statuses[calls - 1];
    const completed = status === "completed";
    return new Response(JSON.stringify({
      data: {
        job_id: "job/123",
        status,
        memories_created: completed ? 1 : 0,
        attempts: 1,
        proposal_ids: ["proposal-1"],
        created_memory_ids: completed ? ["memory-123"] : [],
        operational_metrics: { queue_wait_ms: 12 },
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const client = new MemoryOS("mem_test", MemoryOS.DEFAULT_BASE_URL, 30_000, fetchImpl);

  const first = await client.getJobStatus("job/123");
  assert.equal(first.succeeded, false);
  assert.deepEqual(first.proposalIds, ["proposal-1"]);
  assert.deepEqual(first.operationalMetrics, { queue_wait_ms: 12 });
  const completed = await client.waitForJob("job/123", { timeoutMs: 100, pollIntervalMs: 1 });
  assert.equal(completed.succeeded, true);
  assert.equal(completed.memoriesCreated, 1);
  assert.deepEqual(completed.createdMemoryIds, ["memory-123"]);
  assert.equal(calls, 4);
});

test("waitForJob stops when retries are exhausted", async () => {
  const fetchImpl = async () => new Response(JSON.stringify({
    data: { job_id: "job-dead", status: "dead", attempts: 3 },
  }), { status: 200, headers: { "Content-Type": "application/json" } });
  const client = new MemoryOS("mem_test", MemoryOS.DEFAULT_BASE_URL, 30_000, fetchImpl);

  const job = await client.waitForJob("job-dead", { timeoutMs: 100, pollIntervalMs: 1 });
  assert.equal(job.status, "dead");
  assert.equal(job.succeeded, false);
});

test("answerClarification resolves through the tenant API", async () => {
  let captured;
  const fetchImpl = async (url, init) => {
    captured = { url, init };
    return new Response(JSON.stringify({
      data: {
        resolved: true,
        clarification_id: "clarification/123",
        conflict_id: "conflict-123",
        resolution: "B",
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const client = new MemoryOS("mem_test", MemoryOS.DEFAULT_BASE_URL, 30_000, fetchImpl);

  const result = await client.answerClarification({
    clarificationId: "clarification/123",
    externalUserId: "customer-123",
    answer: "B",
    freeText: "The Scale plan is current.",
  });

  assert.match(captured.url, /\/v1\/memories\/clarifications\/clarification%2F123\/answer$/);
  assert.deepEqual(JSON.parse(captured.init.body), {
    external_user_id: "customer-123",
    answer: "B",
    free_text: "The Scale plan is current.",
  });
  assert.equal(result.resolved, true);
  assert.equal(result.resolution, "B");
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

  await client.add([{ role: "user", content: "Remember this.", externalTurnId: "chat-884:turn-13", sourceKind: "direct_user_input" }], {}, "universal-event-123");
  const addBody = JSON.parse(captured.init.body);
  assert.equal(addBody.idempotency_key, "universal-event-123");
  const result = await client.get("preferences", 5, { format: "json", contextMaxTokens: 900 });
  assert.deepEqual(addBody.messages, [{
    role: "user",
    content: "Remember this.",
    external_turn_id: "chat-884:turn-13",
    source_kind: "direct_user_input",
  }]);
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
