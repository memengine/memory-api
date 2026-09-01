"""Phase-aware synthetic MemoryOS replay; never bootstraps or resolves conflicts."""
from __future__ import annotations

import argparse, asyncio, json, os, re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = Path(__file__).with_name("fixtures") / "prelaunch_traffic_v2.json"
FEATURES = {"general", "edtech", "support", "universal"}
TERMINAL = {"completed", "processed", "failed", "error", "blocked", "dead_letter"}
FAILED = {"failed", "error", "dead_letter", "poll_timeout"}


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists(): return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def load_fixture(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("workflows"): raise ValueError("Fixture must contain workflows")
    seen: set[tuple[str, str]] = set()
    for workflow in data["workflows"]:
        feature = workflow.get("feature", "general")
        if feature not in FEATURES: raise ValueError(f"Unsupported feature: {feature}")
        if not workflow.get("user_id"): raise ValueError(f"Missing user_id: {workflow.get('id')}")
        for event in workflow.get("events", []):
            if not event.get("messages"): raise ValueError(f"Empty event: {workflow.get('id')}")
            if feature == "universal": continue
            source = event["source"]
            key = (source["service"], source["event_id"])
            if key in seen and not event.get("duplicate_of"):
                raise ValueError(f"Duplicate source identity without duplicate_of: {key}")
            seen.add(key)
            datetime.fromisoformat(source["observed_at"].replace("Z", "+00:00"))
    return data


def service_key_env(service: str) -> str:
    return "MEMORYOS_SERVICE_KEY_" + re.sub(r"[^A-Z0-9]+", "_", service.upper()).strip("_")


def build_payload(workflow: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    metadata = {"traffic_class":"synthetic_prelaunch", "fixture_version":"v2",
                "workflow_id":workflow["id"], "category":workflow["category"],
                **event.get("metadata", {})}
    if workflow["feature"] == "universal":
        return {"messages":event["messages"], "metadata":metadata,
                "idempotency_key":f"prelaunch-v2:{workflow['id']}:{event['id']}"}
    result = {"external_user_id":workflow["user_id"], "messages":event["messages"],
              "metadata":metadata, "source":event["source"]}
    if event.get("agent_id_env"):
        value = os.getenv(event["agent_id_env"], "").strip()
        if not value: raise ValueError(f"{event['agent_id_env']} is required")
        result["agent_id"] = value
    return result


@dataclass(frozen=True)
class Target:
    feature: str; base_url: str; api_key: str; uui_token: str = ""
    @property
    def headers(self) -> dict[str, str]:
        headers = {"Authorization": f"ApiKey {self.api_key}"}
        if self.feature == "universal": headers["X-MemoryOS-UUI"] = self.uui_token
        return headers


def target_for(feature: str) -> Target:
    base = os.getenv(f"MEMORYOS_{feature.upper()}_API_BASE_URL",
                     os.getenv("MEMORYOS_API_BASE_URL", "http://localhost:8000")).rstrip("/")
    if feature == "universal":
        return Target(feature, base, os.getenv("MEMORYOS_UNIVERSAL_AGENT_API_KEY", "").strip(),
                      os.getenv("MEMORYOS_UUI_TOKEN", "").strip())
    key = os.getenv(f"MEMORYOS_{feature.upper()}_API_KEY",
                    os.getenv("MEMORYOS_API_KEY", "")).strip()
    return Target(feature, base, key)


async def preflight(target: Target, timeout: float) -> list[str]:
    if not target.api_key: return ["required API key is missing"]
    if target.feature == "universal" and not target.uui_token: return ["MEMORYOS_UUI_TOKEN is missing"]
    try:
        async with httpx.AsyncClient(base_url=target.base_url, headers=target.headers, timeout=timeout) as client:
            if target.feature == "universal":
                response = await client.post("/v1/universal/memories/retrieve",
                                             json={"query":"credential preflight", "limit":1})
                return [] if response.status_code == 200 else [f"Universal auth/grant rejected ({response.status_code})"]
            response = await client.get("/v1/tenant/domain-schema")
            if response.status_code != 200: return [f"tenant auth rejected ({response.status_code})"]
            actual = (response.json().get("data") or {}).get("domain_schema")
            expected = target.feature if target.feature in {"edtech", "support"} else None
            return [] if actual == expected else [f"domain_schema={actual!r}; expected {expected!r}"]
    except httpx.HTTPError as exc:
        return [f"unreachable: {exc.__class__.__name__}"]


async def wait_job(client: httpx.AsyncClient, path: str, poll: float, timeout: float) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.get(path); response.raise_for_status()
        body = response.json(); job = body.get("data", body)
        status = str(job.get("status", job.get("state", ""))).lower()
        if status in TERMINAL: return job
        if asyncio.get_running_loop().time() >= deadline: return {**job, "status":"poll_timeout"}
        await asyncio.sleep(poll)


async def replay(args: argparse.Namespace) -> int:
    load_dotenv(); fixture = load_fixture(Path(args.fixture))
    phases = FEATURES if args.phase == "all" else {args.phase}
    workflows = [w for w in fixture["workflows"] if w["feature"] in phases]
    if not workflows: raise SystemExit(f"No workflows for {sorted(phases)}")
    targets = {phase:target_for(phase) for phase in phases}
    errors = [f"{phase}: {error}" for phase,target in sorted(targets.items())
              for error in await preflight(target, args.request_timeout)]
    if errors:
        print("Preflight failed"); [print(f"- {error}") for error in errors]; return 2
    print("Preflight passed for " + ", ".join(sorted(phases)))
    if args.preflight_only: return 0
    totals: Counter[str] = Counter(); features: Counter[str] = Counter()
    services: Counter[str] = Counter(); categories: Counter[str] = Counter()
    clients = {phase:httpx.AsyncClient(base_url=t.base_url,headers=t.headers,timeout=args.request_timeout)
               for phase,t in targets.items()}
    try:
        for index, workflow in enumerate(workflows, 1):
            feature = workflow["feature"]; ok = True
            for event_index, event in enumerate(workflow["events"], 1):
                totals["requests"] += 1; headers: dict[str,str] = {}
                service = "universal" if feature == "universal" else event["source"]["service"]
                if feature != "universal":
                    override = os.getenv(service_key_env(service), "").strip()
                    if override: headers["Authorization"] = f"ApiKey {override}"
                    headers["Idempotency-Key"] = f"prelaunch-v2:{service}:{event['source']['event_id']}"
                path = "/v1/universal/memories/add" if feature == "universal" else "/v1/memories/add"
                try:
                    response = await clients[feature].post(path,json=build_payload(workflow,event),headers=headers)
                    response.raise_for_status(); result = response.json()
                    status = str(result.get("status", "unknown")).lower(); job_id = result.get("job_id")
                    if args.wait and job_id and status == "queued":
                        job_path = (f"/v1/universal/memories/jobs/{job_id}" if feature == "universal"
                                    else f"/v1/memories/jobs/{job_id}")
                        job = await wait_job(clients[feature],job_path,args.poll_seconds,args.job_timeout)
                        status = str(job.get("status",job.get("state",status))).lower()
                        totals["memories_created"] += int(job.get("memories_created",0) or 0)
                        totals["pending_buffered"] += int(job.get("pending_candidates_buffered",0) or 0)
                    if status in FAILED: raise RuntimeError(f"job {job_id or '-'} ended as {status}")
                    totals["successful"] += 1; features[feature] += 1
                    services[service] += 1; categories[workflow["category"]] += 1
                except (httpx.HTTPError,RuntimeError,ValueError) as exc:
                    totals["failed"] += 1; ok = False
                    print(f"  FAIL {workflow['id']} event {event_index}: {exc}")
                    if args.fail_fast: return 1
                if args.delay_seconds: await asyncio.sleep(args.delay_seconds)
            totals["workflows"] += 1
            print(f"[{index:02d}/{len(workflows)}] {workflow['id']} [{feature}] {'ok' if ok else 'partial'}")
    finally:
        await asyncio.gather(*(client.aclose() for client in clients.values()))
    print("\nReplay totals")
    print(f"workflows={totals['workflows']} requests={totals['requests']} successful={totals['successful']} "
          f"failed={totals['failed']} memories_created={totals['memories_created'] if args.wait else 'not_polled'} "
          f"pending_buffered={totals['pending_buffered'] if args.wait else 'not_polled'}")
    for name,counter in (("feature",features),("service",services),("category",categories)):
        print(f"by_{name}=" + ", ".join(f"{k}:{v}" for k,v in sorted(counter.items())))
    return 1 if totals["failed"] else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture",default=os.getenv("MEMORYOS_TRAFFIC_FILE",str(DEFAULT_FIXTURE)))
    parser.add_argument("--phase",choices=["all",*sorted(FEATURES)],default="general")
    parser.add_argument("--preflight-only",action="store_true"); parser.add_argument("--wait",action="store_true")
    parser.add_argument("--delay-seconds",type=float,default=0.0); parser.add_argument("--poll-seconds",type=float,default=1.0)
    parser.add_argument("--job-timeout",type=float,default=120.0); parser.add_argument("--request-timeout",type=float,default=30.0)
    parser.add_argument("--fail-fast",action="store_true"); return parser.parse_args()


if __name__ == "__main__": raise SystemExit(asyncio.run(replay(parse_args())))
