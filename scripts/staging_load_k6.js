import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Rate } from "k6/metrics";

const API_BASE = (__ENV.API_BASE || "https://api.memoryo.dev").replace(/\/$/, "");
const TENANT_KEY = __ENV.TENANT_KEY || "";
const SUPPORT_KEY = __ENV.SUPPORT_KEY || TENANT_KEY;
const BILLING_KEY = __ENV.BILLING_KEY || TENANT_KEY;
const RUN_ID = __ENV.RUN_ID || `${Date.now()}-${Math.random().toString(16).slice(2)}`;

const retrieveLatency = new Trend("memoryos_retrieve_latency_ms");
const addAckLatency = new Trend("memoryos_add_ack_latency_ms");
const feedbackLatency = new Trend("memoryos_feedback_latency_ms");
const apiErrors = new Rate("memoryos_api_errors");

export const options = {
  scenarios: {
    ramped_staging_probe: {
      executor: "ramping-vus",
      stages: [
        { duration: __ENV.RAMP_1 || "1m", target: Number(__ENV.VUS_1 || 10) },
        { duration: __ENV.RAMP_2 || "2m", target: Number(__ENV.VUS_2 || 50) },
        { duration: __ENV.RAMP_3 || "2m", target: Number(__ENV.VUS_3 || 100) },
        { duration: __ENV.RAMP_DOWN || "1m", target: 0 },
      ],
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    memoryos_api_errors: ["rate<0.01"],
    memoryos_retrieve_latency_ms: ["p(95)<500"],
    memoryos_add_ack_latency_ms: ["p(95)<500"],
  },
};

function headers(apiKey) {
  return {
    Authorization: `ApiKey ${apiKey}`,
    "Content-Type": "application/json",
  };
}

function postJson(path, apiKey, body) {
  return http.post(`${API_BASE}${path}`, JSON.stringify(body), {
    headers: headers(apiKey),
    timeout: "30s",
  });
}

function randomUser() {
  return `k6_stage_${RUN_ID}_${__VU}_${Math.floor(Math.random() * 250)}`;
}

function recordResult(response) {
  const ok = response.status >= 200 && response.status < 300;
  apiErrors.add(!ok);
  return ok;
}

function retrieveFlow() {
  const started = Date.now();
  const response = postJson("/v1/memories/retrieve", TENANT_KEY, {
    external_user_id: randomUser(),
    query: "What should the agent remember about this user?",
    limit: 8,
    format: "json",
    context_max_tokens: 800,
  });
  retrieveLatency.add(Date.now() - started);
  check(response, {
    "retrieve status is 2xx": (r) => r.status >= 200 && r.status < 300,
  });
  recordResult(response);
  return response;
}

function addFlow(apiKey = TENANT_KEY, service = "load-service") {
  const user = randomUser();
  const started = Date.now();
  const response = postJson("/v1/memories/add", apiKey, {
    external_user_id: user,
    messages: [
      {
        role: "user",
        content: `Load test durable preference ${RUN_ID}: user prefers concise debugging answers in Python.`,
      },
      {
        role: "assistant",
        content: "I will remember that preference for future technical help.",
      },
    ],
    source: {
      service,
      event_id: `k6-${service}-${RUN_ID}-${__VU}-${__ITER}`,
      observed_at: new Date().toISOString(),
      scope: { test: "staging-k6", run_id: RUN_ID },
      evidence: [{ source_type: "k6", reference: `vu/${__VU}/iter/${__ITER}` }],
    },
    metadata: { test: "staging-k6", run_id: RUN_ID },
  });
  addAckLatency.add(Date.now() - started);
  check(response, {
    "add ack status is 2xx": (r) => r.status >= 200 && r.status < 300,
  });
  recordResult(response);
  return response;
}

function feedbackFlow() {
  const retrieve = retrieveFlow();
  let retrievalId = null;
  let memoryIds = [];

  try {
    const payload = retrieve.json();
    retrievalId = payload.retrieval_id;
    memoryIds = (payload.data || []).map((item) => item.id).filter(Boolean).slice(0, 5);
  } catch (_) {
    return;
  }

  if (!retrievalId) {
    return;
  }

  const started = Date.now();
  const response = postJson("/v1/memories/retrieval-feedback", TENANT_KEY, {
    retrieval_id: retrievalId,
    outcome: "used_successfully",
    used_memory_ids: memoryIds,
    agent_confidence: 0.82,
    metadata: { test: "staging-k6-feedback", run_id: RUN_ID },
  });
  feedbackLatency.add(Date.now() - started);
  check(response, {
    "feedback status is 2xx": (r) => r.status >= 200 && r.status < 300,
  });
  recordResult(response);
}

function multiServiceFlow() {
  const user = `k6_conflict_${RUN_ID}_${__VU}_${__ITER}`;

  const support = postJson("/v1/memories/add", SUPPORT_KEY, {
    external_user_id: user,
    messages: [
      { role: "user", content: "Support says the customer's current subscription plan is Starter." },
      { role: "assistant", content: "Support recorded Starter as the plan." },
    ],
    source: {
      service: "support-service",
      event_id: `k6-support-${RUN_ID}-${__VU}-${__ITER}`,
      observed_at: new Date(Date.now() - 60000).toISOString(),
      scope: { test: "staging-k6-conflict", source: "support" },
      evidence: [{ source_type: "support_ticket", reference: `k6-ticket-${__VU}-${__ITER}` }],
    },
    metadata: { test: "staging-k6-conflict", run_id: RUN_ID },
  });

  const billing = postJson("/v1/memories/add", BILLING_KEY, {
    external_user_id: user,
    messages: [
      { role: "user", content: "Billing says the customer's current subscription plan is Growth." },
      { role: "assistant", content: "Billing confirmed Growth as the active plan." },
    ],
    source: {
      service: "billing-service",
      event_id: `k6-billing-${RUN_ID}-${__VU}-${__ITER}`,
      observed_at: new Date().toISOString(),
      scope: { test: "staging-k6-conflict", source: "billing" },
      evidence: [{ source_type: "billing_record", reference: `k6-sub-${__VU}-${__ITER}` }],
    },
    metadata: { test: "staging-k6-conflict", run_id: RUN_ID },
  });

  recordResult(support);
  recordResult(billing);
  check(support, { "support add status is 2xx": (r) => r.status >= 200 && r.status < 300 });
  check(billing, { "billing add status is 2xx": (r) => r.status >= 200 && r.status < 300 });
}

export default function () {
  if (!TENANT_KEY) {
    throw new Error("Set TENANT_KEY before running k6.");
  }

  const roll = Math.random();
  if (roll < 0.70) {
    retrieveFlow();
  } else if (roll < 0.90) {
    addFlow();
  } else if (roll < 0.95) {
    feedbackFlow();
  } else {
    multiServiceFlow();
  }

  sleep(Number(__ENV.SLEEP_SECONDS || 1));
}




