"""Five recording scenes backed by a dedicated local beta API."""

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.getenv("MEMORYOS_RECORDING_DIR", ROOT / "beta"))
STATE = DATA / "recording-state.local.json"
CREDENTIALS = DATA / "recording-credentials.local.json"
API = "http://127.0.0.1:8000"
QUESTION = "What subscription plan is this account on?"
CORRECTIONS = "/v1/memories/service-corrections"
COLORS = {"label": "36", "value": "38;2;255;255;255", "ok": "32", "warning": "33", "note": "38;2;160;168;200"}
HTTP = build_opener(ProxyHandler({}))


def show(label, value, color="value"):
    print(f"  \033[{COLORS['label']}m{label:<17}\033[0m \033[{COLORS[color]}m{value}\033[0m")


def short_id(value):
    return f"{value[:12]}…{value[-4:]}" if len(value) > 17 else value


class Demo:
    def __init__(self, first=False):
        if os.getenv("MEMORYOS_DEMO_MOCK") != "1":
            raise SystemExit("Run against the isolated recording stack with MEMORYOS_DEMO_MOCK=1.")
        self.credentials = json.loads(CREDENTIALS.read_text(encoding="utf-8-sig"))
        self.state = {"owner": f"demo-{uuid4().hex[:10]}"} if first else json.loads(STATE.read_text())
        self.owner = self.state["owner"]

    def http(self, path, body=None, key=None, headers=None):
        request = Request(
            API + path, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"ApiKey {key or self.credentials['admin_api_key']}",
                     "Content-Type": "application/json", **(headers or {})},
        )
        try:
            with HTTP.open(request, timeout=10) as response:
                return json.load(response)
        except HTTPError as exc:
            payload = json.load(exc)
            raise RuntimeError(f"{path}: HTTP {exc.code} {payload.get('error')}") from None

    def save(self):
        STATE.write_text(json.dumps(self.state), encoding="utf-8")

    def submit(self, writer, event, statement):
        event = f"{event}_{self.owner[-10:]}"
        client = self.credentials["clients"][writer]
        scope = {"kind": "global", "account": self.owner}
        receipt = self.http("/v1/memories/add", {
            "external_user_id": self.owner, "conversation_id": event,
            "messages": [{"role": "user", "source_kind": "direct_user_input", "content": statement}],
            "source": {"service": client["service_key"], "event_id": event,
                       "observed_at": datetime.now(UTC).isoformat(), "scope": scope,
                       "evidence": [{"source_type": "event_payload", "reference": statement}]},
        }, key=client["api_key"], headers={"Idempotency-Key": f"{self.owner}-{event}"})
        job = self.poll(
            lambda: self.http(f"/v1/memories/jobs/{receipt['job_id']}", key=client["api_key"])["data"],
            lambda result: result["status"] == "completed",
        )
        return job

    @staticmethod
    def poll(read, ready):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            response = read()
            if ready(response):
                return response
            if response.get("status") in {"dead", "failed"}:
                raise RuntimeError(f"Demo job failed: {response.get('error')}")
            time.sleep(0.25)
        raise TimeoutError("MemoryOS did not return the expected demo state.")

    def record(self, key):
        return self.http(f"/v1/memories/{self.state[key]}")["data"]

    def retrieve(self):
        return self.http("/v1/memories/retrieve", {
            "external_user_id": self.owner, "query": QUESTION,
            "format": "json", "categories": ["fact"],
        })

    def published(self, key):
        return self.poll(self.retrieve, lambda result: not result.get("memory_visibility")
                         and any(item["id"] == self.state[key] for item in result["data"]))

    def preview(self):
        params = urlencode({"external_user_id": self.owner, "target_memory_id": self.state["original"]})
        return self.http(f"{CORRECTIONS}/{self.state['candidate']}?{params}")["data"]

    def history(self, key):
        return self.http(f"/v1/memories/{self.state[key]}/history")["data"]


def source(record):
    provenance = record["metadata"]["provenance"] if "metadata" in record else record["provenance"]
    return provenance


def write():
    demo = Demo(first=True)
    job = demo.submit("billing", "evt_billing_001", "Subscription plan is Growth.")
    demo.state["original"], = job["created_memory_ids"]
    demo.published("original")
    record = demo.record("original")
    demo.save()
    show("MemoryOS", "[local API with mock model providers]", "note")
    show("record", record["content"])
    show("source", short_id(source(record)["service"]))
    show("source event", short_id(source(record)["event_id"]))
    show("evidence", record["metadata"]["provenance"]["evidence"][0]["reference"])
    show("is_archived", record["is_archived"])
    show("job status", job["status"], "ok")
    print()


def conflict():
    demo = Demo()
    job = demo.submit("crm", "evt_crm_002", "Correction: Scale replaces the prior tier.")
    pending, = demo.http(f"{CORRECTIONS}?{urlencode({'external_user_id': demo.owner})}")["data"]
    demo.state["candidate"] = pending["id"]
    preview = demo.preview()
    if job["created_memory_ids"] or preview["apply_blockers"] or "apply" not in preview["actions"]:
        raise RuntimeError("Expected a pending candidate eligible for operator application.")
    demo.save()
    show("current record", preview["target_content"])
    show("correction", preview["candidate_content"], "warning")
    show("source event", short_id(preview["source_external_event_id"]))
    show("authority basis", preview["authority_basis"], "note")
    show("operator actions", ", ".join(preview["actions"]), "warning")
    print()


def ask():
    demo = Demo()
    result = demo.poll(demo.retrieve,
                       lambda response: (response.get("memory_visibility") or {}).get("state") == "unavailable")
    visibility = result["memory_visibility"]
    if result["data"]:
        raise RuntimeError("Expected the held plan to be absent from retrieval.")
    show("Q", QUESTION)
    show("memory_visibility", visibility["state"], "warning")
    show("scope", visibility["scope"])
    show("returned records", len(result["data"]), "note")
    show("degraded read", result["is_degraded"], "warning")
    show("agent", "fallback: \"I can't confirm your plan yet.\"", "warning")
    print()


def resolve():
    demo = Demo()
    preview = demo.preview()
    decision = demo.http(f"{CORRECTIONS}/{preview['candidate_id']}/decision", {
        "external_user_id": demo.owner, "target_memory_id": preview["target_memory_id"],
        "target_version": preview["target_version"], "review_version": preview["review_version"],
        "action": "apply",
    })["data"]
    demo.state["replacement"] = decision["memory_id"]
    previous = demo.record("original")
    current = demo.record("replacement")
    demo.published("replacement")
    admin_secret, = (line.partition("=")[2] for line in
                     (ROOT / "beta/beta.env.example").read_text().splitlines()
                     if line.startswith("ADMIN_SECRET="))
    params = urlencode({"tenant_id": demo.credentials["tenant_id"], "action": "updated"})
    entries = demo.http(f"/v1/internal/audit-logs?{params}",
                        headers={"X-Admin-Secret": admin_secret})["data"]
    audit, = [entry for entry in entries if entry["memory_id"] == decision["memory_id"]]
    demo.state["audit"] = {"action": audit["action"], "created_at": audit["created_at"]}
    demo.save()
    show("target", preview["target_content"])
    show("target event", short_id(source(previous)["event_id"]))
    show("candidate", preview["candidate_content"], "warning")
    show("candidate event", short_id(preview["source_external_event_id"]))
    show("operator action", decision["action"], "ok")
    show("candidate status", decision["candidate_status"], "ok")
    show("old is_archived", previous["is_archived"])
    show("new is_archived", current["is_archived"])
    show("audit action", audit["action"], "ok")
    show("audit timestamp", audit["created_at"], "note")
    print()


def history():
    demo = Demo()
    original = demo.record("original")
    current = demo.record("replacement")
    result = demo.published("replacement")
    record, = result["data"]
    show("account", demo.owner)
    for key, saved in (("original", original), ("replacement", current)):
        show("source", short_id(source(saved)["service"]))
        show("source event", short_id(source(saved)["event_id"]))
        for version in sorted(demo.history(key), key=lambda row: row["version_number"]):
            show(f"version {version['version_number']}", f"{version['change_type']} / {version['change_reason']}", "note")
    show("audit action", demo.state["audit"]["action"], "note")
    show("Q", QUESTION)
    show("agent quotes", record["content"], "ok")
    plan = re.fullmatch(r"Correction: (\w+) replaces the prior tier\.", record["content"])
    if plan is None:
        raise RuntimeError("The retrieved record does not match the demo plan format.")
    show("answer", plan[1], "ok")
    show("cites event", short_id(source(record)["event_id"]))
    demo.state["recorded"] = True
    demo.save()
