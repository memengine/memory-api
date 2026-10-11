"""Synthetic observer regression fixtures only; never private acceptance inputs."""
import asyncio
import importlib.util
import json
import time
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("unit_observer", ROOT / "benchmarks/internal/explicit_operations_pilot.py")
observer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = observer
spec.loader.exec_module(observer)
OWNER = UUID(int=1)
HEAD = "a" * 40


def manifest():
    return observer.LocalManifest(schema_version="explicit-operations-observer-2", run_id=uuid4(),
        observer_sha256=observer.observer_digest(), source_head=HEAD, base_url="http://127.0.0.1:55081",
        database_env="UNIT_DB", database_schema="replay_unit", isolated_synthetic_environment=True,
        explicit_preferences_enabled=True, automatic_interpretation_disabled=True, v2_disabled=True,
        external_provider_network_blocked=True, controlled_embeddings=True,
        owner_attested_effective_settings=True, owners=[OWNER], runtime={"source_head": HEAD,
            "api_artifact_sha256": "a"*64, "worker_artifact_sha256": "b"*64,
            "application_artifact_sha256": "c"*64, "dependencies_sha256": "d"*64,
            "effective_settings_sha256": "e"*64, "owner_verified_running_artifacts": True})


def step(name="job", operation="job", **kw):
    data = dict(name=name, operation=operation, principal_env="UNIT_TOKEN", target=str(OWNER),
        expected_http_status=200, checks=[{"pointer": "/status", "expected": 200}])
    if operation == "sql":
        data = dict(name=name, operation=operation, checks=[{"pointer": "/memories", "operator": "count", "expected": 0}])
    data.update(kw)
    return observer.Step(**data)


def add_step(**kw):
    payload = {"external_user_id": "unit-owner", "messages": [{"role": "user", "content": "My examples should use C++."}],
        "preference_claims": [{"property_key": "examples.language", "value": "C++", "applicability": {"kind": "global"},
            "turn_index": 0, "quote": "My examples should use C++."}]}
    return step("add", "add", target=None, payload=payload, **kw)


def case(steps, **kw):
    data = dict(schema_version="explicit-operations-observer-2", case_id="unit-only", requirement="P2",
        coverage_group="unit-only", language="en", human_adjudicated=True, owners=[OWNER], steps=steps)
    data.update(kw)
    return observer.Scenario(**data)


def proof(manifest, kind="fault_injection", **kw):
    now = datetime.now(UTC)
    data = dict(run_id=str(manifest.run_id), source_head=HEAD, case_id="unit-only", kind=kind,
        artifact_sha256="f"*64, owner_adjudicated=True, recorded_at=now.isoformat(),
        fault_removed_at=(now-timedelta(seconds=1)).isoformat())
    data.update(kw)
    return data


@pytest.mark.parametrize("operation", ["memory", "history", "reviews", "job"])
def test_read_endpoint_paths_and_owner_query(operation, monkeypatch):
    monkeypatch.setenv("UNIT_TOKEN", "synthetic-token")
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []})
    s = (step(operation=operation, target=None, payload={"external_user_id": "unit-owner"})
         if operation == "reviews" else step(operation=operation))
    async def run():
        async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
            return await observer.http_observation(client, s, {}, time.perf_counter()+1)
    result = asyncio.run(run())
    assert result["status"] == 200 and requests[0].method == "GET"
    suffix = f"/{OWNER}/history" if operation == "history" else f"/{OWNER}" if operation == "memory" else "/preference-reviews" if operation == "reviews" else f"/jobs/{OWNER}"
    assert requests[0].url.path == "/v1/memories"+suffix
    if operation == "reviews": assert requests[0].url.params["external_user_id"] == "unit-owner"


def test_absolute_http_deadline_cancels_slow_request(monkeypatch):
    monkeypatch.setenv("UNIT_TOKEN", "synthetic-token")
    async def slow(request):
        await asyncio.sleep(.2)
        return httpx.Response(200, json={"data": []})
    async def run():
        async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(slow)) as client:
            await observer.http_observation(client, step(), {}, time.perf_counter()+.02)
    started = time.perf_counter()
    with pytest.raises(TimeoutError): asyncio.run(run())
    assert time.perf_counter()-started < .2


def test_transient_poll_requires_opt_in_and_retains_attempts(monkeypatch):
    monkeypatch.setenv("UNIT_TOKEN", "synthetic-token")
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(503, json={}) if len(calls)==1 else httpx.Response(200, json={"data": {"status": "completed"}})
    async def run(s):
        async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
            return await observer.http_observation(client, s, {}, time.perf_counter()+1)
    opted = asyncio.run(run(step(operation="poll_job", retryable_http_statuses=[503])))
    assert [r["status"] for r in opted["polls"]] == [503,200] and opted["elapsed_ms"] >= 90
    calls.clear()
    unopted = asyncio.run(run(step(operation="poll_job")))
    assert unopted["status"]==503 and len(calls)==1
    with pytest.raises(ValidationError): add_step(retryable_http_statuses=[503])


def test_fresh_client_is_closed_and_not_shared(monkeypatch):
    monkeypatch.setenv("UNIT_TOKEN", "synthetic-token")
    fresh = httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(lambda r: httpx.Response(200,json={})))
    monkeypatch.setattr(observer, "new_http_client", lambda origin: fresh)
    shared = SimpleNamespace(request=AsyncMock(side_effect=AssertionError("shared client must not be used")))
    result = asyncio.run(observer.observe_case(case([step(fresh_client=True)]), manifest(), None, shared))
    assert not result["failures"] and fresh.is_closed
    shared.request.assert_not_called()


@pytest.mark.parametrize("broken", ["timeout", "concurrent_error", "safety_check"])
def test_write_failure_retains_after_state_and_stops_later_writes(broken, monkeypatch):
    monkeypatch.setenv("UNIT_TOKEN", "synthetic-token")
    requests = []
    async def handler(request):
        requests.append(request)
        if broken in {"timeout", "concurrent_error"}: raise httpx.ReadTimeout("private URL/token must not escape")
        return httpx.Response(200, json={"job_id": "wrong"})
    first = add_step(concurrent=broken=="concurrent_error", attempts=2 if broken=="concurrent_error" else 1)
    if broken == "safety_check": first = first.model_copy(update={"checks":[observer.Check(pointer="/body/job_id",expected="expected") ]})
    second = add_step().model_copy(update={"name":"must_not_run"})
    snapshot = AsyncMock(return_value={"memories": [], "jobs": [{"status": "queued"}]})
    monkeypatch.setattr(observer, "sql_snapshot", snapshot)
    async def run():
        async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
            return await observer.observe_case(case([step("before","sql"),first,second,step("after","sql")]), manifest(), None, client)
    result=asyncio.run(run())
    assert "must_not_run" not in result["observations"]
    assert result["diagnostic_after_state"]["state"]["jobs"] == [{"status":"queued"}]
    assert len(requests)==(2 if broken=="concurrent_error" else 1)
    assert "private URL/token" not in json.dumps(result)
    if broken != "safety_check": assert result["safety_unknown"]


def test_timeout_preserves_completed_poll_attempts(monkeypatch):
    monkeypatch.setenv("UNIT_TOKEN", "synthetic-token")
    count=0
    async def handler(request):
        nonlocal count
        count+=1
        if count==1: return httpx.Response(200,json={"data":{"status":"queued"}})
        raise httpx.ReadTimeout("secret")
    monkeypatch.setattr(observer,"sql_snapshot",AsyncMock(return_value={"memories":[]}))
    async def run():
        async with httpx.AsyncClient(base_url="http://127.0.0.1",transport=httpx.MockTransport(handler)) as client:
            return await observer.observe_case(case([step(operation="poll_job")]),manifest(),None,client)
    r=asyncio.run(run())
    assert r["observations"]["job"]["attempts"][0]["body"]["data"]["status"]=="queued"


@pytest.mark.parametrize("mismatch", ["run_id", "case_id", "kind", "source_head"])
def test_operator_evidence_is_bound_to_run_case_kind_and_source(mismatch, monkeypatch, tmp_path):
    m=manifest(); raw=proof(m)
    raw[mismatch] = str(uuid4()) if mismatch=="run_id" else "foreign" if mismatch=="case_id" else "customer_rendering" if mismatch=="kind" else "b"*40
    if mismatch=="kind": raw["first_rendered_elapsed_ms"]=10
    path=tmp_path/"synthetic-operator.json"; path.write_text(json.dumps(raw))
    monkeypatch.setenv("UNIT_PROOF",str(path))
    s=observer.Step(name="signal",operation="external",external_env="UNIT_PROOF",external_kind="fault_injection",checks=[{"pointer":"/record/owner_adjudicated","expected":True}])
    with pytest.raises(ValueError): observer.read_operator_evidence(s,case([s],external_requirements=["fault_injection"]),m)


def test_recovery_deadline_uses_fault_removal_not_receipt_time(monkeypatch,tmp_path):
    m=manifest(); now=datetime.now(UTC)
    path=tmp_path/"synthetic-operator.json"; path.write_text(json.dumps(proof(m,fault_removed_at=(now-timedelta(seconds=31)).isoformat())))
    monkeypatch.setenv("UNIT_PROOF",str(path)); monkeypatch.setattr(observer,"sql_snapshot",AsyncMock(return_value={"memories":[]}))
    s=observer.Step(name="signal",operation="external",external_env="UNIT_PROOF",external_kind="fault_injection",checks=[{"pointer":"/record/owner_adjudicated","expected":True}])
    r=asyncio.run(observer.observe_case(case([s],requirement="P8",external_requirements=["fault_injection"],recovery_clock_step="signal"),m,None,None))
    assert r["failures"][0]["reason"]=="TimeoutError" and not r["diagnostic_after_state"]["within_outcome_deadline"]


def test_missing_external_evidence_is_a_failure(monkeypatch):
    monkeypatch.setattr(observer,"sql_snapshot",AsyncMock(return_value={"memories":[]}))
    r=asyncio.run(observer.observe_case(case([step("sql","sql")],external_requirements=["customer_rendering"]),manifest(),None,None))
    assert r["failures"][0]["reason"]=="external_evidence_missing"


def test_schema_rejects_old_version_arbitrary_schema_and_runtime_mismatch():
    data=manifest().model_dump()
    for change in [{"schema_version":"explicit-operations-observer-1"},{"database_schema":"public;drop table memories"},{"runtime":{**data["runtime"],"source_head":"b"*40}}]:
        with pytest.raises(ValidationError): observer.LocalManifest.model_validate({**data,**change})
    with pytest.raises(ValidationError,match="fault-removal clock"):
        case([step("before","sql")],requirement="P8",external_requirements=["fault_injection"])


def test_preflight_rejects_untracked_backend_and_missing_observer_pin(monkeypatch):
    monkeypatch.setenv("UNIT_DB","postgresql+asyncpg://synthetic:synthetic@127.0.0.1:55471/test")
    monkeypatch.setenv("UNIT_TOKEN","synthetic")
    monkeypatch.setattr(observer.subprocess,"call",lambda *a,**k:0)
    monkeypatch.setattr(observer.subprocess,"check_output",lambda cmd,**kw:HEAD if cmd[1]=="rev-parse" else "api/unfrozen.py")
    with pytest.raises(ValueError,match="untracked backend"): observer.preflight([case([step()])],manifest())
    with pytest.raises(ValueError,match="observer hash"): observer.preflight([],manifest().model_copy(update={"observer_sha256":"0"*64}))


class Context:
    def __init__(self,value): self.value=value
    async def __aenter__(self): return self.value
    async def __aexit__(self,*args): return False


def test_sql_projection_read_only_scoped_bounded_and_no_raw_payload():
    statements=[]
    class Connection:
        def begin(self): return Context(None)
        async def execute(self,statement,params=None):
            statements.append((str(statement),params))
            return SimpleNamespace(mappings=lambda:SimpleNamespace(all=lambda:[]))
    engine=SimpleNamespace(connect=lambda:Context(Connection()))
    result=asyncio.run(observer.sql_snapshot(engine,[OWNER]))
    assert set(result)=={"memories","claims","revisions","audit","versions","jobs","sources","outbox"}
    assert "REPEATABLE READ, READ ONLY" in statements[0][0]
    selects=[(q,p) for q,p in statements if q.startswith("SELECT")]
    assert len(selects)==8 and all(p["owners"]==[OWNER] and "LIMIT 1001" in q for q,p in selects)
    assert all("payload_envelope" not in q and "embedding" not in q and "api_key_id" not in q for q,p in selects)
    assert selects[-1][1]["owner_text"]==[str(OWNER)]


def test_sql_overflow_fails_instead_of_truncating():
    class Connection:
        def begin(self):return Context(None)
        async def execute(self,statement,params=None):
            rows=[{}]*1001 if str(statement).startswith("SELECT") else []
            return SimpleNamespace(mappings=lambda:SimpleNamespace(all=lambda:rows))
    with pytest.raises(ValueError,match="exceeded bound"):
        asyncio.run(observer.sql_snapshot(SimpleNamespace(connect=lambda:Context(Connection())),[OWNER]))


def test_preflight_failure_does_not_consume_synthetic_set(monkeypatch,tmp_path):
    data=tmp_path/"unit-only.jsonl"
    data.write_text(case([step("sql","sql")]).model_dump_json()+"\n")
    man=tmp_path/"unit-manifest.json"; m=manifest(); man.write_text(m.model_dump_json())
    out=tmp_path/"output"
    monkeypatch.setattr(observer,"preflight",lambda *a: (_ for _ in ()).throw(ValueError("blocked")))
    monkeypatch.setattr(sys,"argv",["observer","--dataset",str(data),"--manifest",str(man),"--execute","--approved-dataset-sha256",observer.digest(data.read_bytes()),"--approved-manifest-sha256",observer.digest(man.read_bytes()),"--output",str(out)])
    with pytest.raises(SystemExit): observer.main()
    assert not out.exists() and not data.with_name(data.name+".consumed").exists()


def test_executor_stops_later_cases_after_unknown_write_and_retains_summary(monkeypatch,tmp_path):
    import sqlalchemy.ext.asyncio
    monkeypatch.setenv("UNIT_TOKEN","synthetic-token")
    monkeypatch.setattr(observer,"preflight",lambda *a:"postgresql+asyncpg://127.0.0.1/unit")
    engine=SimpleNamespace(dispose=AsyncMock())
    options=[]
    monkeypatch.setattr(sqlalchemy.ext.asyncio,"create_async_engine",lambda url,**kw:options.append(kw) or engine)
    monkeypatch.setattr(observer,"sql_snapshot",AsyncMock(return_value={"memories":[]}))
    async def failed(request): raise httpx.ReadTimeout("do not print token")
    monkeypatch.setattr(observer,"new_http_client",lambda url:httpx.AsyncClient(base_url=url,transport=httpx.MockTransport(failed)))
    first=case([step("before","sql"),add_step(),step("after","sql")])
    second=case([step()]).model_copy(update={"case_id":"must-not-execute"})
    summary=asyncio.run(observer.execute([first,second],manifest(),tmp_path))
    assert summary["safety_unknown_cases"]==1 and summary["not_executed_cases"]==1
    assert summary["checks_passed_cases"]==0 and summary["pilot_qualified"] is False
    assert summary["coverage"]=={"P2/unit-only/en":2}
    assert options[0]["connect_args"]["server_settings"]["search_path"]=="replay_unit"
    rows=[json.loads(line) for line in (tmp_path/"evidence.jsonl").read_text().splitlines()]
    assert rows[0]["diagnostic_after_state"]["state"]=={"memories":[]}
    assert rows[1]["not_executed"] and "do not print token" not in json.dumps(rows)


def test_recent_recovery_receipt_is_recorded_and_external_render_metrics(monkeypatch,tmp_path):
    m=manifest(); metadata=proof(m)
    path=tmp_path/"synthetic-recovery.json"; path.write_text(json.dumps(metadata))
    monkeypatch.setenv("UNIT_PROOF",str(path))
    signal=observer.Step(name="signal",operation="external",external_env="UNIT_PROOF",external_kind="fault_injection",checks=[{"pointer":"/record/owner_adjudicated","expected":True}])
    r=asyncio.run(observer.observe_case(case([signal],requirement="P8",external_requirements=["fault_injection"],recovery_clock_step="signal"),m,None,None))
    assert not r["failures"] and r["recovery_origin_utc"]
    assert r["observations"]["signal"]["receipt_sha256"]==observer.digest(path.read_bytes())


def test_partial_timeout_read_is_a_latency_sample_not_silently_omitted(monkeypatch,tmp_path):
    import sqlalchemy.ext.asyncio
    monkeypatch.setenv("UNIT_TOKEN","synthetic-token")
    monkeypatch.setattr(observer,"preflight",lambda *a:"postgresql+asyncpg://127.0.0.1/unit")
    monkeypatch.setattr(sqlalchemy.ext.asyncio,"create_async_engine",lambda *a,**kw:SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(observer,"sql_snapshot",AsyncMock(return_value={"memories":[]}))
    async def failed(request): raise httpx.ReadTimeout("secret")
    monkeypatch.setattr(observer,"new_http_client",lambda url:httpx.AsyncClient(base_url=url,transport=httpx.MockTransport(failed)))
    s=step(operation="read",target=None,payload={"external_user_id":"unit-owner","query":"unused explicit query",
        "categories":["preference"],"preference_context":{"applicability":{"kind":"global"},"property_keys":["examples.language"]}})
    summary=asyncio.run(observer.execute([case([s],requirement="P1_P9")],manifest(),tmp_path))
    assert summary["observed_read_samples"]==1 and summary["observed_read_p95_ms"] is not None
    assert summary["failed_cases"]==1 and not summary["latency_is_complete_acceptance_evidence"]


def test_operator_render_measurement_aggregates_without_auto_qualification(monkeypatch,tmp_path):
    import sqlalchemy.ext.asyncio
    m=manifest()
    path=tmp_path/"synthetic-render.json"
    path.write_text(json.dumps(proof(m,kind="customer_rendering",first_rendered_elapsed_ms=4200)))
    monkeypatch.setenv("UNIT_PROOF",str(path))
    monkeypatch.setattr(observer,"preflight",lambda *a:"postgresql+asyncpg://127.0.0.1/unit")
    monkeypatch.setattr(sqlalchemy.ext.asyncio,"create_async_engine",lambda *a,**kw:SimpleNamespace(dispose=AsyncMock()))
    s=observer.Step(name="render",operation="external",external_env="UNIT_PROOF",external_kind="customer_rendering",checks=[{"pointer":"/record/owner_adjudicated","expected":True}])
    c=case([s],requirement="P1_P9",external_requirements=["customer_rendering"])
    summary=asyncio.run(observer.execute([c],m,tmp_path))
    assert summary["operator_render_samples"]==1 and summary["operator_first_render_p95_ms"]==4200
    assert summary["external_evidence_missing_cases"]==0 and summary["pilot_qualified"] is False
