import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

const API_BASE = (__ENV.API_BASE || "http://127.0.0.1:8000").replace(/\/$/, "");
const API_KEY = __ENV.BENCHMARK_API_KEY || "";
const SERVICE = __ENV.SCALE_SOURCE_SERVICE || "";
const RUN_ID = __ENV.RUN_ID || "";
const STAGE = (__ENV.SCALE_STAGE || "LOW").toUpperCase();
const AGENTS = [__ENV.SCALE_AGENT_A, __ENV.SCALE_AGENT_B].filter(Boolean);
const MAX_REQUESTS = Number(__ENV.MAX_REQUESTS || 10000);
const SUMMARY_PATH = __ENV.K6_SUMMARY_PATH || `k6-${RUN_ID}-${STAGE.toLowerCase()}.json`;

if (!API_KEY || !SERVICE || !RUN_ID) throw new Error("BENCHMARK_API_KEY, SCALE_SOURCE_SERVICE and RUN_ID are required");
if (__ENV.MEMORYOS_SCALE_DEDICATED !== "1") throw new Error("Dedicated scale-test marker is required");
if (STAGE !== "LOW" && __ENV.APPROVE_NON_LOW !== "1") throw new Error("Only LOW is approved; set APPROVE_NON_LOW=1 after separate approval");

const profiles = {
  PREFLIGHT: { vus: 2, rate: 1, duration: "3m" },
  DIAGNOSTIC_1RPS: { vus: 4, rate: 1, duration: "3m" },
  DIAGNOSTIC_2RPS: { vus: 5, rate: 2, duration: "3m" },
  LOW: { vus: 5, rate: 2, duration: "10m" },
  MODERATE: { vus: 20, rate: 8, duration: "20m" },
  HIGHER: { vus: 40, rate: 15, duration: "15m" },
  SUSTAINED: { vus: 20, rate: 8, duration: "2h" },
};
const profile = profiles[STAGE];
if (!profile) throw new Error(`Unknown SCALE_STAGE ${STAGE}`);

export const options = {
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
  scenarios: { mixed: { executor: "constant-arrival-rate", rate: profile.rate, timeUnit: "1s", duration: profile.duration, preAllocatedVUs: profile.vus, maxVUs: Math.min(50, profile.vus * 2) } },
  thresholds: {
    http_req_failed: ["rate<0.005"], api_errors: ["rate<0.005"],
    add_ack_ms: ["p(95)<500", "p(99)<1000"], retrieve_ms: ["p(95)<750", "p(99)<1500"],
    job_completion_ms: ["p(95)<10000"], correctness_probe_failures: ["count==0"],
  },
};

const addAck = new Trend("add_ack_ms"), retrieveLatency = new Trend("retrieve_ms"), jobLatency = new Trend("job_completion_ms"), apiErrors = new Rate("api_errors"), correctnessFailures = new Counter("correctness_probe_failures"), requests = new Counter("scale_requests");
const headers = { Authorization: `ApiKey ${API_KEY}`, "Content-Type": "application/json" };

function post(path, body) {
  if (__ITER * Math.max(1, profile.vus) >= MAX_REQUESTS) return null;
  requests.add(1); const response = http.post(`${API_BASE}${path}`, JSON.stringify(body), { headers, timeout: "30s", tags: { stage: STAGE } });
  apiErrors.add(response.status < 200 || response.status >= 300); return response;
}
function externalUser(cohort = "ordinary") { return `scale_${RUN_ID}_${cohort}_${__VU % 200}`; }
function source(eventId) { return { service: SERVICE, event_id: eventId, observed_at: new Date().toISOString(), scope: { scale_run_id: RUN_ID, stage: STAGE }, evidence: [{ source_type: "scale_benchmark", reference: `${RUN_ID}/${__VU}/${__ITER}` }] }; }
function add(user, content, eventId, agentId = null) {
  const started = Date.now(); const body = { external_user_id: user, messages: [{ role: "user", content }], metadata: { scale_run_id: RUN_ID, stage: STAGE }, source: source(eventId) }; if (agentId) body.agent_id = agentId;
  const response = post("/v1/memories/add", body); if (!response) return; addAck.add(Date.now() - started);
  const ok = check(response, { "add accepted": r => r.status >= 200 && r.status < 300 }); if (!ok) return;
  let jobId; try { jobId = response.json("job_id"); } catch (_) { return; } if (!jobId) return;
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) { const status = http.get(`${API_BASE}/v1/memories/jobs/${jobId}`, { headers, timeout: "10s" }); let state; try { state = status.json("data.status"); } catch (_) {} if (["completed", "failed", "dead"].includes(state)) { jobLatency.add(Date.now() - started); if (state !== "completed") correctnessFailures.add(1); break; } sleep(.25); }
}
function retrieve(user, historical = false, agentId = null) {
  const body = { external_user_id: user, query: historical ? "What was previously true about the user's work context?" : "What preferences and current work context should be remembered?", limit: 8, format: "json", context_max_tokens: 800 };
  if (historical) body.as_of = new Date(Date.now() - 86400000).toISOString(); if (agentId) body.agent_id = agentId;
  const started = Date.now(); const response = post("/v1/memories/retrieve", body); if (!response) return; retrieveLatency.add(Date.now() - started); check(response, { "retrieve accepted": r => r.status >= 200 && r.status < 300 });
}

export default function () {
  const roll = Math.random(), user = externalUser(roll > .9 ? "hot" : "ordinary"), event = `${RUN_ID}-${__VU}-${__ITER}`, agent = AGENTS.length ? AGENTS[__ITER % AGENTS.length] : null;
  if (roll < .45) retrieve(user, false, agent);
  else if (roll < .65) add(user, `I consistently prefer concise Python debugging explanations. Observation ${__ITER}.`, `normal-${event}`, agent);
  else if (roll < .73) add(user, `Correction: my current project language is Rust, replacing the earlier Go project context.`, `correction-${event}`, agent);
  else if (roll < .78) add(externalUser("hot"), __ITER % 2 ? "My current incident channel is Atlas." : "Correction: my current incident channel is Beacon.", `conflict-${event}`, agent);
  else if (roll < .83) add(user, `Agent ${agent || "default"} observes that I review release notes every Friday.`, `agent-${event}`, agent);
  else if (roll < .87) { const duplicate = `duplicate-${RUN_ID}-${__VU}-${Math.floor(__ITER / 2)}`; add(user, "I prefer ISO 8601 dates in operational reports.", duplicate, agent); }
  else if (roll < .90) add(user, "I might prefer audio summaries, but I am not sure yet.", `pending-${event}`, agent);
  else if (roll < .95) retrieve(user, true, agent);
  else retrieve(user, false, agent);
}

export function handleSummary(data) {
  return { [SUMMARY_PATH]: JSON.stringify(data, null, 2), stdout: JSON.stringify({ stage: STAGE, checks: data.metrics.checks, errors: data.metrics.api_errors }, null, 2) };
}
