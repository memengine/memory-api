"""Local-only thin observer for independently authored typed-operation scenarios.

Validation/schema export do not create cases, connect to services or call providers.
Execution requires matching private dataset/manifest hashes and a fresh run directory.
This module does not provision fixtures, inject faults, render UI or qualify a pilot.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from api.schemas.requests import MemoryAddRequest, MemoryRetrieveRequest


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Reference(Closed):
    reference: str = Field(pattern=r"^[a-z][a-z0-9_]*#(/.*)?$")


def at(value: Any, pointer: str) -> Any:
    if not pointer:
        return value
    if not pointer.startswith("/"):
        raise ValueError("invalid JSON pointer")
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def resolve(value: Any, observations: dict) -> Any:
    if isinstance(value, dict):
        if set(value) == {"reference"}:
            source, pointer = Reference.model_validate(value).reference.split("#", 1)
            return at(observations[source], pointer)
        return {key: resolve(item, observations) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, observations) for item in value]
    return value


def references(value: Any) -> list[str]:
    if isinstance(value, dict):
        if set(value) == {"reference"}:
            return [Reference.model_validate(value).reference.split("#", 1)[0]]
        return [name for item in value.values() for name in references(item)]
    if isinstance(value, list):
        return [name for item in value for name in references(item)]
    return []


class Check(Closed):
    pointer: str = Field(pattern=r"^(/.*)?$")
    operator: Literal["equals", "count", "contains", "absent"] = "equals"
    expected: Any = None
    safety: bool = True


class Step(Closed):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    operation: Literal["add", "read", "memory", "history", "reviews", "job", "poll_job", "sql", "external"]
    principal_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    auth_scheme: Literal["ApiKey", "Bearer"] = "ApiKey"
    payload: dict[str, Any] | None = None
    target: str | Reference | None = None
    idempotency_key: str | None = None
    attempts: int = Field(default=1, ge=1, le=5)
    concurrent: bool = False
    fresh_client: bool = False
    retryable_http_statuses: list[Literal[429, 502, 503, 504]] = Field(default_factory=list, max_length=4)
    external_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    display_source: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    external_kind: Literal["fault_injection", "browser_identity", "customer_rendering", "queue_accounting"] | None = None
    invalid_input_control: bool = False
    expected_http_status: int | None = Field(default=None, ge=100, le=599)
    checks: list[Check] = Field(min_length=1)

    @model_validator(mode="after")
    def bounded(self):
        if self.operation == "external":
            if (bool(self.external_env) == bool(self.display_source) or not self.external_kind or self.principal_env or self.payload
                    or self.target or self.attempts != 1 or self.concurrent or self.fresh_client
                    or self.retryable_http_statuses or self.idempotency_key or self.expected_http_status is not None):
                raise ValueError("external observation accepts only a declared owner evidence file")
            if self.display_source and self.external_kind not in {"customer_rendering", "browser_identity"}:
                raise ValueError("display cannot collect fault or queue evidence")
            return self
        if self.external_env or self.external_kind or self.display_source:
            raise ValueError("external fields require external operation")
        if self.operation == "sql":
            if self.principal_env or self.payload or self.target or self.attempts != 1 or self.fresh_client:
                raise ValueError("SQL observation accepts only checks")
        else:
            if not self.principal_env or self.expected_http_status is None:
                raise ValueError("HTTP observations require principal and expected status")
        if self.operation in {"add", "read"}:
            if self.payload is None or self.target is not None:
                raise ValueError("add/read require payload, not target")
            if self.operation == "add":
                claims = self.payload.get("preference_claims")
                if not isinstance(claims, list) or len(claims) != 1:
                    raise ValueError("exactly one explicit declaration is required")
            else:
                if self.payload.get("categories") != ["preference"]:
                    raise ValueError("reads must remain preference-only")
            if not references(self.payload):
                validate_payload(self.operation, self.payload, self.invalid_input_control)
        elif self.operation == "reviews":
            if self.target is not None or not self.payload or set(self.payload) != {"external_user_id"}:
                raise ValueError("reviews require only external_user_id")
        elif self.operation != "sql" and (self.target is None or self.payload is not None):
            raise ValueError("memory/job observations require target, not payload")
        if self.invalid_input_control and (self.operation not in {"add", "read"}
                                          or self.expected_http_status < 400):
            raise ValueError("invalid input controls require expected HTTP refusal")
        if (self.attempts != 1 or self.concurrent or self.idempotency_key) and self.operation != "add":
            raise ValueError("only add supports repetition/idempotency")
        if self.retryable_http_statuses and self.operation not in {"read", "poll_job"}:
            raise ValueError("only declared reads/job polling may retry transient responses; never add")
        return self


def validate_payload(operation: str, payload: dict, invalid_control: bool = False) -> None:
    try:
        parsed = (MemoryAddRequest if operation == "add" else MemoryRetrieveRequest).model_validate(payload)
    except ValidationError:
        if invalid_control:
            return  # Must fail backend request validation, before any semantic route.
        raise
    if invalid_control:
        raise ValueError("invalid input control is valid under backend schema; unsafe to send")
    if operation == "read" and (parsed.preference_context is None or parsed.agent_id is not None):
        raise ValueError("explicit reads require context and no agent")


class Scenario(Closed):
    schema_version: Literal["explicit-operations-observer-3"]
    case_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    requirement: Literal["P2", "P4", "P5", "P6", "P7", "P8", "P1_P9"]
    coverage_group: str = Field(min_length=1, max_length=100)
    language: Literal["en", "hi", "hinglish", "not_applicable"]
    human_adjudicated: Literal[True]
    owners: list[UUID] = Field(min_length=1, max_length=8)
    steps: list[Step] = Field(min_length=1, max_length=30)
    external_requirements: list[Literal["fault_injection", "browser_identity", "customer_rendering", "queue_accounting"]] = Field(default_factory=list)
    recovery_clock_step: str | None = None

    @model_validator(mode="after")
    def ordered(self):
        known = set()
        clocks = []
        write_positions = [i for i, step in enumerate(self.steps) if step.operation == "add"]
        sql_positions = [i for i, step in enumerate(self.steps) if step.operation == "sql"]
        if write_positions and (not sql_positions or min(sql_positions) >= min(write_positions)
                                or max(sql_positions) <= max(write_positions)):
            raise ValueError("write scenarios require SQL baseline and final state observations")
        for step in self.steps:
            if step.operation == "external":
                if step.display_source and (step.display_source not in known or not any(
                        prior.name == step.display_source and prior.principal_env for prior in self.steps)):
                    raise ValueError("display evidence must name an earlier HTTP operation")
                if step.external_kind not in self.external_requirements:
                    raise ValueError("external observations must be declared as required evidence")
                if step.name == self.recovery_clock_step:
                    if step.external_kind != "fault_injection":
                        raise ValueError("recovery clock requires fault-removal evidence")
                    clocks.append(step.name)
            if step.name in known:
                raise ValueError("duplicate step name")
            request_refs = references(step.payload) + references(
                step.target.model_dump() if isinstance(step.target, Reference) else step.target)
            if any(name not in known for name in request_refs):
                raise ValueError("request references must point to earlier steps")
            known.add(step.name)
            if any(name not in known for check in step.checks for name in references(check.expected)):
                raise ValueError("check references must point to current/earlier steps")
        if self.recovery_clock_step is not None and (self.requirement != "P8" or len(clocks) != 1):
            raise ValueError("only P8 may anchor recovery to one declared fault-removal observation")
        if self.requirement == "P8" and "fault_injection" in self.external_requirements and not self.recovery_clock_step:
            raise ValueError("P8 fault recovery requires the fault-removal clock anchor")
        return self


class RuntimePin(Closed):
    source_head: str = Field(pattern=r"^[0-9a-f]{40}$")
    api_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    application_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependencies_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_settings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_verified_running_artifacts: Literal[True]


class DisplayPin(Closed):
    port: int = Field(ge=1024, le=65535)
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    browser_version: str = Field(min_length=1, max_length=120)
    owner_bindings: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def bindings(self):
        import re
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not value.strip()
               for key, value in self.owner_bindings.items()):
            raise ValueError("invalid display principal binding")
        return self


class LocalManifest(Closed):
    schema_version: Literal["explicit-operations-observer-3"]
    run_id: UUID
    observer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime: RuntimePin
    source_head: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_url: str
    database_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    database_schema: str = Field(default="public", pattern=r"^[a-z][a-z0-9_]{0,62}$")
    isolated_synthetic_environment: Literal[True]
    explicit_preferences_enabled: Literal[True]
    automatic_interpretation_disabled: Literal[True]
    v2_disabled: Literal[True]
    external_provider_network_blocked: Literal[True]
    controlled_embeddings: Literal[True]
    owner_attested_effective_settings: Literal[True]
    owners: list[UUID] = Field(min_length=1)
    display: DisplayPin | None = None

    @model_validator(mode="after")
    def aligned(self):
        if self.runtime.source_head != self.source_head:
            raise ValueError("runtime and source pins differ")
        if len(set(self.owners)) != len(self.owners):
            raise ValueError("duplicate manifest owners")
        return self


class OperatorEvidence(Closed):
    run_id: UUID
    case_id: str
    source_head: str = Field(pattern=r"^[0-9a-f]{40}$")
    kind: Literal["fault_injection", "browser_identity", "customer_rendering", "queue_accounting"]
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_adjudicated: bool
    recorded_at: datetime
    fault_removed_at: datetime | None = None
    first_rendered_elapsed_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    browser_identity_checked: bool | None = None
    queue_depth: int | None = Field(default=None, ge=0)
    reserved_job_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def complete(self):
        if self.recorded_at.tzinfo is None or (self.fault_removed_at and self.fault_removed_at.tzinfo is None):
            raise ValueError("operator timestamps require timezone")
        if self.recorded_at > datetime.now(UTC):
            raise ValueError("operator evidence cannot be future dated")
        if self.kind == "fault_injection" and (self.fault_removed_at is None or self.fault_removed_at > self.recorded_at):
            raise ValueError("fault evidence requires observed removal timestamp")
        if self.kind == "customer_rendering" and self.first_rendered_elapsed_ms is None:
            raise ValueError("render evidence requires measured first-render elapsed time")
        if self.kind == "browser_identity" and self.browser_identity_checked is None:
            raise ValueError("browser identity adjudication required")
        if self.kind == "queue_accounting" and (self.queue_depth is None or self.reserved_job_count is None):
            raise ValueError("queue evidence requires depth and reservation observations")
        return self


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_cases(path: Path) -> tuple[list[Scenario], str]:
    raw = path.read_bytes()
    cases = [Scenario.model_validate_json(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("empty dataset or duplicate IDs")
    return cases, digest(raw)


def local_url(url: str, schemes: set[str]) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in schemes or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("only loopback services are permitted")
    if parsed.query or parsed.fragment:
        raise ValueError("URL query/fragment forbidden")


async def sql_snapshot(engine, owners: list[UUID]) -> dict:
    from sqlalchemy import bindparam, text

    queries = {
        "memories": "SELECT id, proxy_user_id, content, category, is_archived, previous_version_id, source_conversation_id, metadata FROM memories WHERE proxy_user_id IN :owners ORDER BY id LIMIT 1001",
        "claims": "SELECT id, tenant_id, proxy_user_id, claim_fingerprint, scope, active_value, status, active_memory_id, winning_revision_id, authority_priority FROM memory_claims WHERE proxy_user_id IN :owners ORDER BY id LIMIT 1001",
        "revisions": "SELECT r.id, r.claim_id, r.memory_id, r.asserted_value, r.status, r.authority_priority FROM memory_claim_revisions r JOIN memory_claims c ON c.id=r.claim_id WHERE c.proxy_user_id IN :owners ORDER BY r.id LIMIT 1001",
        "audit": "SELECT id, proxy_user_id, memory_id, action, old_value, new_value FROM audit_logs WHERE proxy_user_id IN :owners ORDER BY id LIMIT 1001",
        "versions": "SELECT v.id, v.memory_id, v.version_number, v.change_type, v.changed_by, v.created_at FROM memory_versions v JOIN memories m ON m.id=v.memory_id WHERE m.proxy_user_id IN :owners ORDER BY v.id LIMIT 1001",
        "jobs": "SELECT id, tenant_id, proxy_user_id, source_event_id, status, attempts, memories_created, request_key_sha256, request_binding->>'version' AS binding_version, request_binding->>'owner_id' AS binding_owner, request_binding->>'payload_sha256' AS binding_payload_sha256, celery_task_id, queued_at, processing_started_at, completed_at, runtime_route, runtime_applied_at, runtime_hold_released_at, runtime_visibility_finalized_at, (runtime_residual_hold IS NOT NULL) AS residual_hold_present, runtime_withholding_footprint, COALESCE(preference_intent_admission ? 'runtime_source_sha256',false) AS runtime_admission_present FROM extraction_jobs WHERE proxy_user_id IN :owners ORDER BY id LIMIT 1001",
        "sources": "SELECT id, tenant_id, proxy_user_id, source_service, source_event_id, payload_hash, observed_at, received_at FROM memory_source_events WHERE proxy_user_id IN :owners ORDER BY id LIMIT 1001",
        "outbox": "SELECT v.id, v.memory_id, v.operation, v.status, v.attempts, v.created_at, v.synced_at FROM vector_sync_outbox v LEFT JOIN memories m ON m.id=v.memory_id WHERE m.proxy_user_id IN :owners OR v.payload->>'proxy_user_id' IN :owner_text ORDER BY v.id LIMIT 1001",
    }
    result = {}
    async with engine.connect() as connection:
        async with connection.begin():
            await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            await connection.execute(text("SET LOCAL statement_timeout = '3000ms'"))
            for name, query in queries.items():
                statement = text(query).bindparams(bindparam("owners", expanding=True))
                parameters = {"owners": owners}
                if name == "outbox":
                    statement = statement.bindparams(bindparam("owner_text", expanding=True))
                    parameters["owner_text"] = [str(owner) for owner in owners]
                rows = (await connection.execute(statement, parameters)).mappings().all()
                if len(rows) > 1000:
                    raise ValueError("SQL projection exceeded bound; no truncated evidence")
                result[name] = json.loads(json.dumps([dict(row) for row in rows], default=str))
    return result


def check_observation(observation: dict, check: Check, observations: dict) -> bool:
    try:
        value = at(observation, check.pointer)
    except (KeyError, IndexError):
        return check.operator == "absent"
    except (TypeError, ValueError):
        return False
    expected = resolve(check.expected, observations)
    if check.operator == "absent":
        return False
    if check.operator == "count":
        return len(value) == expected
    if check.operator == "contains":
        return expected in value
    return value == expected


async def http_observation(client, step: Step, observations: dict, deadline: float, attempt_log=None) -> dict:
    token = os.environ[step.principal_env]
    headers = {"Authorization": f"{step.auth_scheme} {token}"}
    if step.idempotency_key:
        headers["Idempotency-Key"] = step.idempotency_key
    payload = resolve(step.payload, observations)
    params = None
    if step.operation in {"add", "read"}:
        validate_payload(step.operation, payload, step.invalid_input_control)
        path = "/v1/memories/add" if step.operation == "add" else "/v1/memories/retrieve"
    elif step.operation == "reviews":
        owner = payload.get("external_user_id")
        if not isinstance(owner, str) or not owner:
            raise ValueError("reviews require nonempty application owner")
        params = {"external_user_id": owner}
        path = "/v1/memories/preference-reviews"
        payload = None
    else:
        target = resolve(step.target.model_dump() if isinstance(step.target, Reference) else step.target, observations)
        target = str(UUID(str(target)))
        path = (f"/v1/memories/{target}" if step.operation == "memory" else
                f"/v1/memories/{target}/history" if step.operation == "history" else
                f"/v1/memories/jobs/{target}")

    async def request_once():
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("scenario deadline")
        started = time.perf_counter()
        response = await asyncio.wait_for(client.request("POST" if step.operation in {"add", "read"} else "GET",
                                        path, json=payload if payload is not None else None,
                                        headers=headers, params=params, timeout=remaining), timeout=remaining)
        try:
            body = response.json()
        except ValueError:
            body = {"non_json_sha256": digest(response.content)}
        result = {"status": response.status_code, "body": body,
                  "elapsed_ms": (time.perf_counter() - started) * 1000}
        if attempt_log is not None:
            attempt_log.append(result)
        return result

    if step.operation == "poll_job" or step.retryable_http_statuses:
        logical_started = time.perf_counter()
        attempts = []
        while True:
            result = await request_once()
            attempts.append(result)
            if result["status"] in step.retryable_http_statuses:
                await asyncio.sleep(min(0.1, max(0, deadline - time.perf_counter())))
                continue
            state = result["body"].get("data", {}).get("status")
            if step.operation != "poll_job" or result["status"] != 200 or state in {"completed", "failed", "dead", "unknown"}:
                return {**result, "polls": attempts, "elapsed_ms": (time.perf_counter()-logical_started)*1000}
            await asyncio.sleep(min(0.1, max(0, deadline - time.perf_counter())))
    if step.concurrent:
        results = await asyncio.gather(*(request_once() for _ in range(step.attempts)), return_exceptions=True)
        return {"attempts": [item if isinstance(item, dict) else {"error": type(item).__name__} for item in results]}
    results = [await request_once() for _ in range(step.attempts)]
    return results[0] if step.attempts == 1 else {"attempts": results}


def observer_digest() -> str:
    return digest(Path(__file__).read_bytes())


def preflight(cases, manifest: LocalManifest) -> str:
    local_url(manifest.base_url, {"http"})
    if urlsplit(manifest.base_url).username or urlsplit(manifest.base_url).path not in {"", "/"}:
        raise ValueError("base URL must be a credential-free loopback origin")
    database = os.environ[manifest.database_env]
    local_url(database, {"postgresql+asyncpg"})
    if observer_digest() != manifest.observer_sha256:
        raise ValueError("observer hash differs from frozen manifest")
    if manifest.display:
        adapter = Path(__file__).with_name("explicit_operations_operator.py")
        if digest(adapter.read_bytes()) != manifest.display.artifact_sha256:
            raise ValueError("display artifact differs from frozen manifest")
        if urlsplit(manifest.base_url).port == manifest.display.port:
            raise ValueError("display cannot share backend port")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if head != manifest.source_head:
        raise ValueError("source HEAD differs from manifest")
    if subprocess.call(["git", "diff", "--quiet", "HEAD", "--", "api"]):
        raise ValueError("backend working source differs from pinned HEAD")
    others = subprocess.check_output(["git", "ls-files", "--others", "--", "api"], text=True).splitlines()
    if any(Path(name).suffix.lower() == ".py" for name in others):
        raise ValueError("untracked backend source is not a frozen artifact")
    if any(owner not in manifest.owners for case in cases for owner in case.owners):
        raise ValueError("scenario observes owner outside frozen synthetic manifest")
    for case in cases:
        for step in case.steps:
            if step.display_source and not manifest.display:
                raise ValueError("display evidence requires frozen display artifact")
            if step.principal_env and not os.environ.get(step.principal_env):
                raise ValueError("required principal unavailable")
            if manifest.display and step.principal_env not in manifest.display.owner_bindings and step.principal_env:
                raise ValueError("display principal lacks binding")
            if step.external_env and not os.environ.get(step.external_env):
                raise ValueError("required operator evidence path unavailable")
    return database


def read_operator_evidence(step, case, manifest):
    path = Path(os.environ[step.external_env]).resolve()
    if str(path).startswith(("\\\\", "//")) or not path.is_file():
        raise ValueError("operator evidence must be a regular local file")
    if path.stat().st_size > 16384:
        raise ValueError("operator evidence exceeds bound")
    raw = path.read_bytes()
    if len(raw) > 16384:
        raise ValueError("operator evidence exceeds bound")
    record = OperatorEvidence.model_validate_json(raw)
    if (record.run_id != manifest.run_id or record.case_id != case.case_id
            or record.source_head != manifest.source_head or record.kind != step.external_kind):
        raise ValueError("operator evidence does not match this frozen run/case/kind")
    return {"record": record.model_dump(mode="json"), "receipt_sha256": digest(raw)}


def new_http_client(base_url):
    return httpx.AsyncClient(base_url=base_url, trust_env=False, follow_redirects=False,
                            transport=httpx.AsyncHTTPTransport(retries=0))


async def observe_case(case, manifest, engine, client, display=None):
    observations, failures, provided = {}, [], set()
    started = time.perf_counter()
    deadline = started + 30
    write_attempted = False
    safety_unknown = False
    recovery_origin = None
    for step in case.steps:
        step_started = time.perf_counter()
        attempt_log = []
        identity = None
        try:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError("outcome deadline")
            if step.operation == "sql":
                call = sql_snapshot(engine, case.owners)
            elif step.operation == "external":
                # Closed metadata only; no shell hooks, fault injection or rendering.
                if step.display_source:
                    record = display.receipt(case, step.external_kind, step.display_source, step.name)
                    OperatorEvidence.model_validate(record)
                    observation = {"record": record, "receipt_sha256": digest(json.dumps(record, indent=2).encode())}
                else:
                    observation = read_operator_evidence(step, case, manifest)
                provided.add(step.external_kind)
                if step.name == case.recovery_clock_step:
                    removed = datetime.fromisoformat(observation["record"]["fault_removed_at"])
                    age = (datetime.now(UTC) - removed).total_seconds()
                    recovery_origin = removed.isoformat()
                    deadline = time.perf_counter() + max(0, 30-age)
                    if age >= 30:
                        raise TimeoutError("fault-removal recovery deadline expired")
                call = None
            else:
                if display:
                    identity = await display.begin(case, step, resolve(step.payload, observations), deadline)
                if step.operation == "add":
                    write_attempted = True
                if step.fresh_client:
                    async def reopened():
                        async with new_http_client(manifest.base_url) as fresh:
                            return await http_observation(fresh, step, observations, deadline, attempt_log)
                    call = reopened()
                else:
                    call = http_observation(client, step, observations, deadline, attempt_log)
            if call is not None:
                observation = await asyncio.wait_for(call, timeout=max(0, deadline-time.perf_counter()))
                # Preserve the backend response even if the display acknowledgement times out.
                observations[step.name] = observation
                if display and identity is not None:
                    await display.finish(case, step, observation, identity, deadline)
            observations[step.name] = observation
            if step.expected_http_status is not None:
                replies = observation.get("attempts", [observation])
                if any(reply.get("error") for reply in replies):
                    safety_unknown = write_attempted
                    failures.append({"step": step.name, "reason": "transport_error", "safety": False,
                                     "safety_unknown": safety_unknown})
                    break
                if any(reply.get("status") != step.expected_http_status for reply in replies):
                    failures.append({"step": step.name, "reason": "http_status", "safety": False})
            for index, check in enumerate(step.checks):
                if not check_observation(observation, check, observations):
                    failures.append({"step": step.name, "check": index, "safety": check.safety})
            if time.perf_counter() > deadline:
                raise TimeoutError("outcome deadline exceeded")
            if any(f["safety"] for f in failures):
                break  # No later mutating steps after a failed safety assertion.
        except Exception as exc:
            prior = observations.get(step.name)
            observations[step.name] = {"error": type(exc).__name__, "attempts": attempt_log,
                                      **({"backend_observation": prior} if prior is not None else {}),
                                      "elapsed_ms": (time.perf_counter()-step_started)*1000}
            safety_unknown = write_attempted
            failures.append({"step": step.name, "reason": type(exc).__name__, "safety": False,
                             "safety_unknown": safety_unknown})
            break
    missing = sorted(set(case.external_requirements)-provided)
    if missing:
        failures.append({"reason": "external_evidence_missing", "kinds": missing, "safety": False})
    outcome_elapsed = (time.perf_counter()-started)*1000
    if failures:
        # A late snapshot is diagnostic, never a successful <=30 s outcome.
        after_started = time.perf_counter()
        try:
            state = await asyncio.wait_for(sql_snapshot(engine, case.owners), timeout=3)
            after = {"state": state, "within_outcome_deadline": time.perf_counter() <= deadline}
        except Exception as exc:
            after = {"error": type(exc).__name__, "within_outcome_deadline": False}
            safety_unknown = safety_unknown or write_attempted
        after["elapsed_ms"] = (time.perf_counter()-after_started)*1000
    else:
        after = None
    return {"case_id": case.case_id, "requirement": case.requirement,
        "coverage_group": case.coverage_group, "language": case.language,
        "observations": observations, "failures": failures, "safety_unknown": safety_unknown,
        "external_requirements": case.external_requirements, "external_evidence_provided": sorted(provided),
        "recovery_origin_utc": recovery_origin, "diagnostic_after_state": after,
        "elapsed_ms": outcome_elapsed}


async def execute(cases, manifest: LocalManifest, output: Path):
    from sqlalchemy.ext.asyncio import create_async_engine

    database = preflight(cases, manifest)
    engine = create_async_engine(database, echo=False,
        connect_args={"timeout": 3, "server_settings": {"search_path": manifest.database_schema}})
    results = []
    stopped = False
    display = None
    try:
        if manifest.display:
            from benchmarks.internal.explicit_operations_operator import DisplayOperator
            display = DisplayOperator(manifest, output)
            await display.start()
        async with new_http_client(manifest.base_url) as client:
            for case in cases:
                if stopped:
                    results.append({"case_id": case.case_id, "requirement": case.requirement,
                                    "coverage_group": case.coverage_group, "observations": {},
                                    "failures": [{"reason": "not_executed_after_safety_failure_or_uncertainty", "safety": False}],
                                    "external_requirements": case.external_requirements,
                                    "language": case.language, "external_evidence_provided": [],
                                    "not_executed": True, "elapsed_ms": 0})
                    continue
                result = await observe_case(case, manifest, engine, client, display)
                results.append(result)
                stopped = result["safety_unknown"] or any(f["safety"] for f in result["failures"])
                with (output / "evidence.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    finally:
        try:
            if display:
                await display.close()
        finally:
            await engine.dispose()
    summary = {"cases": len(results), "checks_passed_cases": sum(not r["failures"] for r in results),
               "failed_cases": sum(bool(r["failures"]) for r in results),
               "safety_check_failed_cases": sum(any(f["safety"] for f in r["failures"]) for r in results),
               "not_executed_cases": sum(bool(r.get("not_executed")) for r in results),
               "safety_unknown_cases": sum(bool(r.get("safety_unknown")) for r in results),
               "external_evidence_required_cases": sum(bool(r["external_requirements"]) for r in results),
               "pilot_qualified": False, "interpretation_quality_measured": False}
    timings = [o["elapsed_ms"] for r in results for name, o in r["observations"].items()
               if next(s.operation for c in cases if c.case_id == r["case_id"] for s in c.steps if s.name == name) == "read"
               and "elapsed_ms" in o]
    summary["observed_read_samples"] = len(timings)
    summary["observed_read_p95_ms"] = sorted(timings)[math.ceil(len(timings) * .95) - 1] if timings else None
    summary["latency_is_complete_acceptance_evidence"] = False
    summary["coverage"] = dict(Counter(f"{r['requirement']}/{r['coverage_group']}/{r['language']}" for r in results))
    summary["external_evidence_missing_cases"] = sum(bool(set(r["external_requirements"])-set(r["external_evidence_provided"])) for r in results)
    rendered = [o["record"]["first_rendered_elapsed_ms"] for r in results for o in r["observations"].values()
                if isinstance(o, dict) and o.get("record", {}).get("kind") == "customer_rendering"]
    summary["operator_render_samples"] = len(rendered)
    summary["operator_first_render_p95_ms"] = sorted(rendered)[math.ceil(len(rendered)*.95)-1] if rendered else None
    summary["unadjudicated_external_observations"] = sum(
        o.get("record", {}).get("owner_adjudicated") is False
        for r in results for o in r["observations"].values() if isinstance(o, dict))
    summary["external_adjudication_complete"] = (summary["unadjudicated_external_observations"] == 0
                                               and summary["external_evidence_missing_cases"] == 0)
    summary["runtime_pin"] = manifest.runtime.model_dump(mode="json")
    summary["observer_sha256"] = manifest.observer_sha256
    summary["by_requirement"] = {name: {"cases": sum(r["requirement"] == name for r in results),
        "checks_passed_cases": sum(r["requirement"] == name and not r["failures"] for r in results)}
        for name in sorted({r["requirement"] for r in results})}
    (output / "evidence.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", choices=["scenario", "manifest", "operator"])
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-dataset-sha256")
    parser.add_argument("--approved-manifest-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.schema:
            if args.execute or args.dataset or args.manifest:
                raise ValueError("schema export is separate from validation/execution")
            print(json.dumps({"scenario": Scenario, "manifest": LocalManifest,
                              "operator": OperatorEvidence}[args.schema].model_json_schema(), indent=2))
            return
        if not args.dataset:
            raise ValueError("dataset path required")
        cases, dataset_hash = load_cases(args.dataset)
        coverage = Counter(f"{c.requirement}/{c.coverage_group}/{c.language}" for c in cases)
        if not args.execute:
            print(json.dumps({"schema_valid": True, "dataset_sha256": dataset_hash,
                              "cases": len(cases), "coverage": dict(coverage), "acceptance_passed": False}, indent=2))
            return
        if not args.manifest or not args.output:
            raise ValueError("manifest and fresh private output directory required")
        raw = args.manifest.read_bytes()
        manifest = LocalManifest.model_validate_json(raw)
        if dataset_hash != args.approved_dataset_sha256 or digest(raw) != args.approved_manifest_sha256:
            raise ValueError("explicit matching dataset and manifest approval required")
        # Pure local/source/environment checks before consuming an approved set.
        # This does not contact the target or prove runtime attestations itself.
        preflight(cases, manifest)
        # Per-dataset exclusive marker prevents a normal rerun with a different output directory.
        marker = args.dataset.with_name(args.dataset.name + ".consumed")
        if args.output.exists() or marker.exists():
            raise ValueError("output exists or dataset already consumed")
        args.output.mkdir(parents=True, exist_ok=False)
        with marker.open("x", encoding="utf-8") as handle:
            json.dump({"dataset_sha256": dataset_hash, "manifest_sha256": digest(raw)}, handle)
        (args.output / "freeze.json").write_text(json.dumps({"dataset_sha256": dataset_hash,
            "manifest_sha256": digest(raw), "coverage": dict(coverage)}, indent=2), encoding="utf-8")
        print(json.dumps(asyncio.run(execute(cases, manifest, args.output)), indent=2))
    except Exception as exc:
        parser.exit(1, f"Observer stopped: {type(exc).__name__}. Private details suppressed.\n")


if __name__ == "__main__":
    main()
