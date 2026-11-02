"""Small HTTP integration example. Never executes paid paths without an opt-in."""
import argparse
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


def call(base, key, method, path, body=None, *, request_key=None):
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    if request_key:
        headers["Idempotency-Key"] = request_key
    request = Request(base.rstrip("/") + path, method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"HTTP {error.code}: {error.read().decode()}") from None


def wait_for_job(base, key, job_id, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = call(base, key, "GET", f"/v1/memories/jobs/{job_id}")
        job = response["data"]
        if job["status"] == "completed":
            return job
        if job["status"] == "dead":
            raise RuntimeError(f"Job {job_id} is dead; preserve the response and stop.")
        # 'failed' can be awaiting the existing automatic retry. Do not resubmit.
        time.sleep(2)
    raise TimeoutError(f"Job {job_id} did not complete within the example's wait budget; do not resubmit.")


def quote_response(response):
    """Literal quotation adapter, not general agent reasoning or answer grounding."""
    if response.get("memory_visibility"):
        return {"status": "unavailable", "notice": response["memory_visibility"], "quotes": []}
    return {"status": "quotation_only", "quotes": [
        {"memory_id": item["id"], "source_event_id": item.get("source_event_id"),
            "content": item["content"], "provenance": item.get("provenance")}
        for item in response.get("data", [])]}


def demo(base, admin):
    run = uuid.uuid4().hex
    owner = f"beta-example-{run}"
    scope = {"kind": "global", "account": owner}
    clients = {}
    for service, priority in (("billing", 20), ("crm", 95)):
        credential = call(base, admin, "POST", "/v1/api-keys", {"name": f"{service}-{run}",
            "permissions": ["read", "write"]})["data"]
        name = f"beta-{service}-{run}"
        call(base, admin, "POST", "/v1/tenant/service-writers", {"service_key": name,
            "display_name": f"Synthetic {service}", "api_key_id": credential["id"],
            "authority_rules": {"default_priority": priority}})
        clients[service] = (name, credential["raw_key"])
    jobs = []
    for service, text in (("billing", "This account has subscription plan Growth."),
        ("billing", "This account has billing currency INR."),
        ("crm", "Correction: this account now has subscription plan Scale instead of Growth."),
        ("crm", "Correction: this account now has subscription plan Enterprise instead of Scale.")):
        event = str(uuid.uuid4())
        source, key = clients[service]
        admitted = call(base, key, "POST", "/v1/memories/add", {
            "external_user_id": owner, "conversation_id": event,
            "messages": [{"role": "user", "source_kind": "direct_user_input",
                "external_turn_id": event, "content": text}],
            "source": {"service": source, "event_id": event,
                "observed_at": datetime.now(UTC).isoformat(), "scope": scope}}, request_key=event)
        jobs.append(wait_for_job(base, key, admitted["data"]["job_id"]))
    response = call(base, clients["crm"][1], "POST", "/v1/memories/retrieve", {
        "external_user_id": owner, "query": "What subscription plan and billing currency are recorded?",
        "categories": ["fact"], "format": "json", "limit": 10})
    reviews = call(base, admin, "GET", "/v1/memories/service-corrections?" + urlencode({"external_user_id": owner}))
    return {"owner": owner, "jobs": jobs, "current_context": quote_response(response),
        "pending_corrections": reviews["data"],
        "next_step": "Review actual records. If pending, explicitly choose a target and use preview; no automatic decision was made."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["demo", "read", "preview", "decide"])
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--credentials", default="beta/credentials.local.json")
    parser.add_argument("--owner")
    parser.add_argument("--candidate", type=uuid.UUID)
    parser.add_argument("--target", type=uuid.UUID)
    parser.add_argument("--preview-file")
    parser.add_argument("--action", choices=["apply", "decline"])
    parser.add_argument("--allow-paid-calls", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.username:
        parser.error("This example is restricted to an explicitly local HTTP API.")
    if (args.mode in {"demo", "read"} or (args.mode == "decide" and args.action == "apply")) and not args.allow_paid_calls:
        parser.error("This path may call the configured backend provider. Supply --allow-paid-calls only after approving the cost.")
    if args.mode != "demo" and not args.owner:
        parser.error("Supply the application-owned --owner explicitly.")
    if args.mode == "preview" and (not args.candidate or not args.target):
        parser.error("Preview requires an explicit --candidate and --target UUID.")
    if args.mode == "decide" and (not args.preview_file or not args.action):
        parser.error("Decision requires a personally reviewed --preview-file and explicit --action.")
    credentials = json.loads(Path(args.credentials).read_text(encoding="utf-8-sig"))
    admin = credentials["admin_api_key"]
    if args.mode == "demo":
        response = demo(args.base_url, admin)
    elif args.mode == "read":
        response = quote_response(call(args.base_url, admin, "POST", "/v1/memories/retrieve", {
            "external_user_id": args.owner, "query": "What is current for this account?",
            "categories": ["fact"], "format": "json", "limit": 10}))
    elif args.mode == "preview":
        response = call(args.base_url, admin, "GET", f"/v1/memories/service-corrections/{args.candidate}?" +
            urlencode({"external_user_id": args.owner, "target_memory_id": str(args.target)}))
    else:
        view = json.loads(Path(args.preview_file).read_text(encoding="utf-8-sig"))["data"]
        response = call(args.base_url, admin, "POST", f"/v1/memories/service-corrections/{view['candidate_id']}/decision", {
            "external_user_id": args.owner, "target_memory_id": view["target_memory_id"],
            "target_version": view["target_version"], "review_version": view["review_version"], "action": args.action})
    rendered = json.dumps(response, indent=2, ensure_ascii=False)
    if args.output:
        with Path(args.output).open("x", encoding="utf-8") as output:
            output.write(rendered)
    else:
        print(rendered)


if __name__ == "__main__":
    main()
