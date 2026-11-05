"""Package boundaries and no-accidental-paid-call gates; no live services."""
import importlib.util
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("local_beta_example", ROOT / "beta" / "example.py")
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


def test_visibility_hold_never_becomes_a_quoted_current_answer():
    response = {"memory_visibility": {"state": "unavailable", "scope": "affected_context"},
        "data": [{"id": "old", "content": "Old fact must not be quoted"}]}
    assert example.quote_response(response)["quotes"] == []
    assert example.quote_response(response)["status"] == "unavailable"


def test_quotation_is_literal_and_preserves_each_records_source_association():
    records = [{"id": "plan", "content": "Enterprise", "source_event_id": "crm"},
        {"id": "currency", "content": "INR", "source_event_id": "billing"}]
    quoted = example.quote_response({"data": records})
    assert quoted["status"] == "quotation_only"
    assert [(q["memory_id"], q["content"], q["source_event_id"]) for q in quoted["quotes"]] == [
        ("plan", "Enterprise", "crm"), ("currency", "INR", "billing")]


@pytest.mark.parametrize("mode", ["demo", "read", "decide"])
def test_paid_paths_fail_before_credentials_or_network_without_opt_in(monkeypatch, mode):
    monkeypatch.setattr(sys, "argv", ["example.py", mode, "--action", "apply"])
    monkeypatch.setattr(example, "call", lambda *a, **k: pytest.fail("Network must not be reached"))
    with pytest.raises(SystemExit) as error:
        example.main()
    assert error.value.code == 2


@pytest.mark.parametrize("url", ["https://api.memoryos.io", "http://postgres", "http://key@localhost:18000"])
def test_example_refuses_nonlocal_or_credential_bearing_urls(monkeypatch, url):
    monkeypatch.setattr(sys, "argv", ["example.py", "preview", "--base-url", url])
    with pytest.raises(SystemExit) as error:
        example.main()
    assert error.value.code == 2


def test_retryable_failed_job_is_polled_under_the_same_id(monkeypatch):
    seen = []
    responses = iter([{"data": {"status": "failed"}}, {"data": {"status": "completed"}}])
    def call(*args):
        seen.append(args)
        return next(responses)
    monkeypatch.setattr(example, "call", call)
    monkeypatch.setattr(example.time, "sleep", lambda _: None)
    assert example.wait_for_job("http://127.0.0.1:18000", "synthetic-key", "job") == {"status": "completed"}
    assert len(seen) == 2
    assert all(args[2:] == ("GET", "/v1/memories/jobs/job") for args in seen)


def test_container_build_context_is_an_allowlist_without_local_keys_or_evidence():
    rules = (ROOT / "beta" / "Dockerfile.dockerignore").read_text().splitlines()
    assert "**" in rules
    allowed = [line[1:] for line in rules if line.startswith("!")]
    assert "api/**" in allowed
    assert "beta/bootstrap.py" in allowed
    assert not any(".env" in path or "credentials" in path or "benchmarks" in path for path in allowed)


def test_http_client_uses_the_public_apikey_authorization_contract(monkeypatch):
    requests = []
    def response(request, timeout):
        requests.append(request)
        return io.BytesIO(b'{"data": []}')
    monkeypatch.setattr(example, "urlopen", response)
    assert example.call("http://127.0.0.1:18000", "synthetic", "GET", "/v1/api-keys") == {"data": []}
    assert requests[0].get_header("Authorization") == "ApiKey synthetic"
    assert requests[0].get_header("X-api-key") is None


def test_demo_requires_bootstrap_catalog_before_any_network_call(monkeypatch):
    monkeypatch.setattr(example, "call", lambda *a, **k: pytest.fail("Network must not be reached"))
    with pytest.raises(ValueError, match="writer catalog missing"):
        example.demo("http://127.0.0.1:18000", {"admin_api_key": "synthetic"})


def test_demo_uses_bound_client_keys_without_dashboard_registration(monkeypatch):
    seen = []
    def call(base, key, method, path, body=None, **kwargs):
        seen.append((key, method, path, body))
        if path == "/v1/memories/add":
            return {"job_id": "synthetic-job", "status": "queued"}
        if path == "/v1/memories/retrieve":
            return {"data": []}
        if path.startswith("/v1/memories/service-corrections?"):
            return {"data": []}
        pytest.fail(f"Unexpected request: {method} {path}")
    monkeypatch.setattr(example, "call", call)
    monkeypatch.setattr(example, "wait_for_job", lambda *a: {"status": "completed"})
    credentials = {"admin_api_key": "operator", "clients": {
        service: {"service_key": service, "api_key": f"key-{service}"}
        for service in ("billing", "crm")}}
    result = example.demo("http://127.0.0.1:18000", credentials)
    writes = [item for item in seen if item[2] == "/v1/memories/add"]
    assert [(key, body["source"]["service"]) for key, _, _, body in writes] == [
        ("key-billing", "billing"), ("key-billing", "billing"),
        ("key-crm", "crm"), ("key-crm", "crm")]
    assert seen[-2][0] == "key-crm"
    assert seen[-1][0] == "operator"
    assert "key-billing" not in str(result) and "key-crm" not in str(result)
