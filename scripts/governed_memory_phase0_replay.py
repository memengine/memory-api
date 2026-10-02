"""Evaluate governed-memory journeys through the public MemoryOS API.

This is a read/write diagnostic for fresh synthetic users. It does not modify
production behavior and never writes the API key to its artifact.

Environment:
  MEMORYOS_API_KEY       Required with --execute.
  MEMORYOS_API_BASE_URL  Optional API origin override.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = (
    Path(__file__).with_name("fixtures") / "governed_memory_phase0_v1.json"
)
DEFAULT_FIXTURE_VERSION = "phase0-v3"
DEFAULT_BASE_URL = "https://api.memoryo.dev"
ALLOWED_LANGUAGES = {"en", "hi", "hinglish", "system"}
TERMINAL_JOB_STATUSES = {
    "blocked",
    "completed",
    "dead",
    "dead_letter",
    "error",
}


def load_fixture(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return validate_fixture(payload)


def validate_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("Fixture must contain at least one scenario")
    seen: set[str] = set()
    for scenario in scenarios:
        scenario_id = str(scenario.get("id") or "").strip()
        if not scenario_id or scenario_id in seen:
            raise ValueError("Each scenario must have a unique non-empty id")
        seen.add(scenario_id)
        language = str(scenario.get("language") or "").strip().lower()
        family = str(scenario.get("family") or "").strip()
        if language and language not in ALLOWED_LANGUAGES:
            raise ValueError(f"{scenario_id}.language is unsupported")
        if "language" in scenario and not language:
            raise ValueError(f"{scenario_id}.language must be non-empty")
        if "family" in scenario and not family:
            raise ValueError(f"{scenario_id}.family must be non-empty")
        if "safety_critical" in scenario and not isinstance(
            scenario["safety_critical"], bool
        ):
            raise TypeError(f"{scenario_id}.safety_critical must be boolean")
        fields = ["initial_messages", "update_messages"]
        if "setup_messages" in scenario:
            fields.append("setup_messages")
        for field in fields:
            messages = scenario.get(field)
            if not isinstance(messages, list) or not messages:
                raise ValueError(f"{scenario_id}.{field} must contain messages")
            if any(
                not isinstance(message, dict)
                or message.get("role") not in {"user", "assistant", "system"}
                or not str(message.get("content") or "").strip()
                for message in messages
            ):
                raise ValueError(f"{scenario_id}.{field} contains an invalid message")
        for field in ("warm_query", "verification_query"):
            if not str(scenario.get(field) or "").strip():
                raise ValueError(f"{scenario_id}.{field} must be non-empty")
        expected = scenario.get("expected")
        if not isinstance(expected, dict) or not isinstance(
            expected.get("clarification_required"), bool
        ):
            raise TypeError(f"{scenario_id}.expected is invalid")
        after_resolution = expected.get("after_resolution")
        if after_resolution is not None and not isinstance(after_resolution, dict):
            raise TypeError(f"{scenario_id}.expected.after_resolution is invalid")
        resolve_with_answer = expected.get("resolve_with_answer")
        if resolve_with_answer not in {None, "A", "B", "both", "neither"}:
            raise ValueError(f"{scenario_id}.resolve_with_answer is invalid")
        resolve_to_initial = expected.get("resolve_to_initial_memory")
        if resolve_to_initial not in {None, True}:
            raise ValueError(f"{scenario_id}.resolve_to_initial_memory is invalid")
        resolution_selectors = sum(
            bool(value)
            for value in (
                resolve_with_answer,
                expected.get("resolve_to_label_contains"),
                resolve_to_initial,
            )
        )
        if resolution_selectors > 1:
            raise ValueError(f"{scenario_id} has multiple resolution selectors")
        has_resolution = resolution_selectors == 1
        if has_resolution != bool(after_resolution):
            raise ValueError(
                f"{scenario_id} must define a resolution selector and after_resolution"
            )
        restatement = scenario.get("restatement")
        if restatement is not None:
            if not isinstance(restatement, dict) or has_resolution:
                raise ValueError(
                    f"{scenario_id}.restatement cannot combine with a resolution selector"
                )
            messages = restatement.get("messages")
            if (
                not isinstance(messages, list)
                or not messages
                or any(
                    not isinstance(message, dict)
                    or message.get("role") != "user"
                    or not str(message.get("content") or "").strip()
                    for message in messages
                )
            ):
                raise ValueError(
                    f"{scenario_id}.restatement requires actual user messages"
                )
            for name in ("initial_project", "general", "replacement"):
                selector = restatement.get(name)
                if not isinstance(selector, dict) or not selector.get("contains_all"):
                    raise ValueError(
                        f"{scenario_id}.restatement.{name} requires a selector"
                    )
                for field in ("contains_all", "contains_none"):
                    terms = selector.get(field, [])
                    if not isinstance(terms, list) or any(
                        not isinstance(term, str) or not term.strip() for term in terms
                    ):
                        raise ValueError(
                            f"{scenario_id}.restatement.{name}.{field} is invalid"
                        )
            if expected.get("governance_attention_required") is not True:
                raise ValueError(
                    f"{scenario_id}.restatement requires governance attention"
                )
        update_http_status = expected.get("update_http_status", 200)
        if not isinstance(update_http_status, int) or update_http_status < 100:
            raise ValueError(f"{scenario_id}.update_http_status is invalid")
        if (
            update_http_status >= 400
            and not str(expected.get("update_error_code") or "").strip()
        ):
            raise ValueError(
                f"{scenario_id}.update_error_code is required for rejected updates"
            )
    return payload


def build_add_payload(
    scenario: dict[str, Any],
    *,
    external_user_id: str,
    conversation_id: str,
    phase: str,
    fixture_version: str = DEFAULT_FIXTURE_VERSION,
) -> dict[str, Any]:
    messages_field = {
        "setup": "setup_messages",
        "initial": "initial_messages",
        "update": "update_messages",
    }[phase]
    return {
        "external_user_id": external_user_id,
        "messages": scenario[messages_field],
        "conversation_id": conversation_id,
        "evidence_mode": "conversation_evidence",
        "metadata": {
            "traffic_class": "synthetic_phase0_characterization",
            "fixture_version": fixture_version,
            "scenario_id": scenario["id"],
            "phase": phase,
        },
    }


def build_retrieve_payload(*, external_user_id: str, query: str) -> dict[str, Any]:
    return {
        "external_user_id": external_user_id,
        "query": query,
        "limit": 10,
        "context_max_tokens": 1000,
    }


def evaluate_retrieval(
    response: dict[str, Any],
    expected: dict[str, Any],
    *,
    check_clarification: bool = True,
    excluded_memory_ids: set[str] | None = None,
) -> dict[str, Any]:
    raw_memories = response.get("data")
    memories: list[dict[str, Any]] = (
        [item for item in raw_memories if isinstance(item, dict)]
        if isinstance(raw_memories, list)
        else []
    )
    searchable = "\n".join(str(item.get("content") or "") for item in memories).lower()
    include_terms = [str(item).lower() for item in expected.get("must_include_any", [])]
    include_all_terms = [
        str(item).lower() for item in expected.get("must_include_all", [])
    ]
    exclude_terms = [str(item).lower() for item in expected.get("must_exclude_all", [])]
    memory_ids = {
        str(item.get("id") or "") for item in memories if str(item.get("id") or "")
    }
    clarification_present = bool(response.get("clarification"))
    checks: dict[str, bool] = {
        "required_value": not include_terms
        or any(term in searchable for term in include_terms),
        "superseded_value_absent": not any(
            term in searchable for term in exclude_terms
        ),
    }
    if "must_include_all" in expected:
        checks["all_required_values"] = all(
            term in searchable for term in include_all_terms
        )
    if excluded_memory_ids:
        checks["superseded_memory_absent"] = memory_ids.isdisjoint(excluded_memory_ids)
    if check_clarification:
        checks["clarification"] = (
            bool(clarification_present or response.get("source_reviews"))
            if expected.get("governance_attention_required")
            else clarification_present == bool(expected.get("clarification_required"))
        )
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            "clarification_present": clarification_present,
            "memory_count": len(memories),
            "memory_ids": sorted(memory_ids),
            "matched_required_terms": [
                term for term in include_terms if term in searchable
            ],
            "matched_excluded_terms": [
                term for term in exclude_terms if term in searchable
            ],
        },
    }


def evaluate_scenario(
    immediate: dict[str, Any],
    settled: dict[str, Any],
    expected: dict[str, Any],
    *,
    initial_memory_ids: set[str] | None = None,
) -> dict[str, Any]:
    initial_ids = set(initial_memory_ids or set())
    excluded_ids = initial_ids if expected.get("exclude_initial_memory") else set()
    immediate_state = evaluate_retrieval(
        immediate,
        expected,
        check_clarification=False,
        excluded_memory_ids=excluded_ids,
    )
    settled_state = evaluate_retrieval(
        settled,
        expected,
        check_clarification=False,
        excluded_memory_ids=excluded_ids,
    )
    clarification_observations = [
        bool(immediate.get("clarification")),
        bool(settled.get("clarification")),
    ]
    clarification_required = bool(expected.get("clarification_required"))
    clarification_check = (
        any(clarification_observations)
        if clarification_required
        else not any(clarification_observations)
    )
    immediate_empty_is_safe = bool(expected.get("allow_empty_immediate")) and (
        immediate_state["observed"]["memory_count"] == 0
        and not immediate_state["observed"]["clarification_present"]
    )
    if expected.get("governance_attention_required"):
        clarification_check = any(
            body.get("clarification") or body.get("source_reviews")
            for body in (immediate, settled)
        )
    checks: dict[str, bool] = {
        "immediate_memory_state": (
            immediate_state["passed"] or immediate_empty_is_safe
        ),
        "settled_memory_state": settled_state["passed"],
        "clarification_policy_satisfied": clarification_check,
    }
    if expected.get("require_initial_memory"):
        checks["initial_memory_created"] = bool(initial_ids)
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "clarification_observations": clarification_observations,
        "immediate": immediate_state,
        "settled": settled_state,
    }


def created_memory_ids(add_result: dict[str, Any]) -> set[str]:
    terminal = add_result.get("terminal")
    job = terminal.get("job") if isinstance(terminal, dict) else None
    raw_ids = job.get("created_memory_ids") if isinstance(job, dict) else None
    if not isinstance(raw_ids, list):
        return set()
    return {str(item) for item in raw_ids if str(item).strip()}


def select_clarification_option(
    clarification: dict[str, Any], label_contains: str
) -> dict[str, Any]:
    needle = label_contains.casefold().strip()
    options = clarification.get("options")
    matches = (
        [
            option
            for option in options
            if isinstance(option, dict)
            and needle in str(option.get("label") or "").casefold()
        ]
        if isinstance(options, list)
        else []
    )
    if len(matches) != 1:
        labels = (
            [
                str(option.get("label") or "")
                for option in options
                if isinstance(option, dict)
            ]
            if isinstance(options, list)
            else []
        )
        raise ValueError(
            f"Expected one clarification option containing {label_contains!r}; "
            f"found {len(matches)} in labels {labels!r}"
        )
    return matches[0]


def select_clarification_answer(
    clarification: dict[str, Any], answer: str
) -> dict[str, Any]:
    options = clarification.get("options")
    matches = (
        [
            option
            for option in options
            if isinstance(option, dict) and option.get("answer") == answer
        ]
        if isinstance(options, list)
        else []
    )
    if len(matches) != 1:
        raise ValueError(
            f"Expected one clarification option with answer {answer!r}; found {len(matches)}"
        )
    return matches[0]


def select_clarification_memory(
    clarification: dict[str, Any], memory_ids: set[str]
) -> dict[str, Any]:
    options = clarification.get("options")
    matches = (
        [
            option
            for option in options
            if isinstance(option, dict)
            and str(option.get("memory_id") or "") in memory_ids
        ]
        if isinstance(options, list)
        else []
    )
    if len(matches) != 1:
        raise ValueError(
            "Expected exactly one clarification option backed by the target memory IDs; "
            f"found {len(matches)}"
        )
    return matches[0]


def evaluate_provenance(
    response: dict[str, Any],
    *,
    expected_conversation_id: str,
    expected_authority_label: str | None = None,
    expected_authority_level: int | None = None,
    allow_empty: bool = False,
) -> dict[str, Any]:
    memories = (
        [item for item in response.get("data", []) if isinstance(item, dict)]
        if isinstance(response.get("data"), list)
        else []
    )
    item_checks: list[dict[str, Any]] = []
    for memory in memories:
        provenance = memory.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        extraction = provenance.get("extraction_evidence")
        extraction = extraction if isinstance(extraction, dict) else {}
        authority = extraction.get("authority")
        authority = authority if isinstance(authority, dict) else {}
        references = extraction.get("turn_references")
        references = references if isinstance(references, list) else []
        eligible_user_references = [
            reference
            for reference in references
            if isinstance(reference, dict)
            and reference.get("role") == "user"
            and str(reference.get("source_kind") or "direct_user_input").strip().lower()
            in {"direct_user_input", "client_assertion"}
        ]
        checks = {
            "source_event_present": bool(provenance.get("event_id")),
            "external_conversation_matches": (
                provenance.get("external_conversation_id") == expected_conversation_id
            ),
            "authority_present": bool(authority.get("label"))
            and isinstance(authority.get("level"), int),
            "user_evidence_present": bool(eligible_user_references),
        }
        if expected_authority_label is not None:
            checks["authority_label_matches"] = (
                authority.get("label") == expected_authority_label
            )
        if expected_authority_level is not None:
            checks["authority_level_matches"] = (
                authority.get("level") == expected_authority_level
            )
        item_checks.append(
            {
                "memory_id": str(memory.get("id") or ""),
                "passed": all(checks.values()),
                "checks": checks,
            }
        )
    return {
        "passed": (allow_empty or bool(item_checks))
        and all(item["passed"] for item in item_checks),
        "memory_count": len(memories),
        "items": item_checks,
    }


def evaluate_idempotency(
    first: dict[str, Any], replayed: dict[str, Any]
) -> dict[str, Any]:
    first_job_id = str(first.get("job_id") or "")
    replayed_job_id = str(replayed.get("job_id") or "")
    checks = {
        "job_id_present": bool(first_job_id),
        "same_job_id": first_job_id == replayed_job_id,
    }
    return {"passed": all(checks.values()), "checks": checks}


def evaluate_isolation(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    checks = {
        "no_memories": isinstance(data, list) and not data,
        "no_clarification": not bool(response.get("clarification")),
        "no_source_reviews": not bool(response.get("source_reviews")),
    }
    return {"passed": all(checks.values()), "checks": checks}


def _request_identity(response: httpx.Response, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": body.get("request_id") or response.headers.get("x-request-id"),
        "http_status": response.status_code,
    }


async def wait_for_job(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        response = await client.get(f"/v1/memories/jobs/{job_id}")
        response.raise_for_status()
        body = response.json()
        job = body.get("data", body)
        status = str(job.get("status") or "unknown").lower()
        if status in TERMINAL_JOB_STATUSES:
            return {
                **_request_identity(response, body),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "job": job,
            }
        if asyncio.get_running_loop().time() >= deadline:
            return {
                **_request_identity(response, body),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "job": {**job, "status": "poll_timeout"},
            }
        await asyncio.sleep(poll_seconds)


async def add_and_wait(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    poll_seconds: float,
    timeout_seconds: float,
    expected_http_status: int = 200,
) -> dict[str, Any]:
    started = time.perf_counter()
    response = await client.post(
        "/v1/memories/add",
        json=payload,
        headers={"Idempotency-Key": idempotency_key},
    )
    acknowledgement_ms = round((time.perf_counter() - started) * 1000, 2)
    body = response.json()
    if response.status_code != expected_http_status:
        response.raise_for_status()
        raise RuntimeError(
            f"Expected HTTP {expected_http_status}, received {response.status_code}"
        )
    if expected_http_status >= 400:
        return {
            **_request_identity(response, body),
            "acknowledgement_ms": acknowledgement_ms,
            "rejected": True,
            "error_code": body.get("code"),
            "error": body.get("error") or body.get("message"),
        }
    response.raise_for_status()
    job_id = body.get("job_id")
    result: dict[str, Any] = {
        **_request_identity(response, body),
        "acknowledgement_ms": acknowledgement_ms,
        "job_id": job_id,
        "queue_status": body.get("status"),
        "processing_eta_seconds": body.get("processing_eta_seconds"),
        "processing_status": body.get("processing_status"),
    }
    if job_id and str(body.get("status") or "").lower() == "queued":
        result["terminal"] = await wait_for_job(
            client,
            str(job_id),
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
    return result


async def retrieve(
    client: httpx.AsyncClient, *, external_user_id: str, query: str
) -> dict[str, Any]:
    started = time.perf_counter()
    response = await client.post(
        "/v1/memories/retrieve",
        json=build_retrieve_payload(external_user_id=external_user_id, query=query),
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    response.raise_for_status()
    body = response.json()
    return {
        **_request_identity(response, body),
        "latency_ms": latency_ms,
        "retrieval_id": body.get("retrieval_id"),
        "cached": body.get("cached"),
        "is_degraded": body.get("is_degraded"),
        "is_passthrough": body.get("is_passthrough"),
        "clarification": body.get("clarification"),
        "source_reviews": body.get("source_reviews", []),
        "data": body.get("data", []),
    }


async def answer_source_review(
    client: httpx.AsyncClient,
    *,
    external_user_id: str,
    review: dict[str, Any],
    expected_http_status: int = 200,
) -> dict[str, Any]:
    """Probe only restate: this action never activates or closes a review."""
    if "restate" not in review.get("actions", []):
        raise ValueError("Review does not allow restate")
    started = time.perf_counter()
    response = await client.post(
        f"/v1/memories/source-reviews/{review['id']}/answer",
        json={
            "external_user_id": external_user_id,
            "version": review["version"],
            "action": "restate",
        },
    )
    body = response.json()
    if response.status_code != expected_http_status:
        response.raise_for_status()
        raise RuntimeError(
            f"Expected HTTP {expected_http_status}, received {response.status_code}"
        )
    return {
        **_request_identity(response, body),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "data": body.get("data"),
        "code": body.get("code"),
    }


def select_memory(response: dict[str, Any], selector: dict[str, Any]) -> dict[str, Any]:
    """Development assertions only; never a production scope classifier."""
    matches = []
    for item in response.get("data", []):
        content = str(item.get("content") or "").casefold()
        if all(
            term.casefold() in content for term in selector["contains_all"]
        ) and not any(
            term.casefold() in content for term in selector.get("contains_none", [])
        ):
            matches.append(item)
    if len(matches) != 1 or not matches[0].get("id"):
        raise ValueError(f"Expected one scoped memory; found {len(matches)}")
    return matches[0]


def memory_invariants(memory: dict[str, Any]) -> dict[str, Any]:
    # Retrieval counters may change. Identity, content, lineage and authority must not.
    provenance = memory.get("provenance") or {}
    return {
        name: memory.get(name)
        for name in (
            "id",
            "content",
            "is_archived",
            "previous_version_id",
            "source_event_id",
        )
    } | {"authority": (provenance.get("extraction_evidence") or {}).get("authority")}


async def memory_snapshot(client: httpx.AsyncClient, memory_id: str) -> dict[str, Any]:
    response = await client.get(f"/v1/memories/{memory_id}")
    response.raise_for_status()
    return response.json()["data"]


async def replay_restatement(
    client: httpx.AsyncClient,
    *,
    scenario: dict[str, Any],
    warm: dict[str, Any],
    settled: dict[str, Any],
    initial: dict[str, Any],
    update: dict[str, Any],
    external_user_id: str,
    conversation_id: str,
    run_id: str,
    fixture_version: str,
    args: argparse.Namespace,
    setup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One optional extension of the existing journey, not a second runner."""
    config = scenario["restatement"]
    record: dict[str, Any] = {"checks": {}, "review_resolution": "not_observed"}
    checks = record["checks"]
    for name, operation in (("initial", initial), ("uncertainty", update)):
        checks[f"{name}_job_completed"] = (operation.get("terminal") or {}).get(
            "job", {}
        ).get("status") == "completed"
    if "setup_messages" in scenario:
        checks["setup_job_completed"] = ((setup or {}).get("terminal") or {}).get(
            "job", {}
        ).get("status") == "completed"
    try:
        old = select_memory(warm, config["initial_project"])
        general = select_memory(warm, config["general"])
    except ValueError as exc:
        return {
            **record,
            "passed": False,
            "error": str(exc),
            "checks": {**checks, "distinct_scoped_memories": False},
        }
    old_id, general_id = str(old["id"]), str(general["id"])
    checks["distinct_scoped_memories"] = old_id != general_id
    checks["initial_ids_verified"] = (
        old_id in created_memory_ids(initial)
        and general_id in created_memory_ids(setup or {})
        if "setup_messages" in scenario
        else {old_id, general_id}.issubset(created_memory_ids(initial))
    )
    if not all(checks.values()):
        return {
            **record,
            "passed": False,
            "error": "Initial write preconditions failed",
        }
    uncertainty_memories = [
        await memory_snapshot(client, memory_id)
        for memory_id in sorted(created_memory_ids(update))
    ]
    record["uncertainty_memory_records"] = uncertainty_memories
    # A canonical clarification may create an archived alternative. Created IDs
    # alone must not be mistaken for admitted active memories.
    checks["uncertainty_created_no_active_memory"] = all(
        memory.get("is_archived") is True for memory in uncertainty_memories
    )
    before = {
        name: await memory_snapshot(client, memory_id)
        for name, memory_id in (("project", old_id), ("general", general_id))
    }
    for name, original in (("project", old), ("general", general)):
        checks[f"uncertainty_preserved_{name}"] = (
            before[name].get("content") == original.get("content")
            and before[name].get("is_archived") is False
        )
    reviews = [
        r
        for r in settled.get("source_reviews", [])
        if r.get("target_memory_id") == old_id
    ]
    checks["review_or_clarification_available"] = bool(
        reviews or settled.get("clarification")
    )
    review = reviews[0] if len(reviews) == 1 else None
    record["attention_path"] = (
        "source_review"
        if review
        else "clarification"
        if settled.get("clarification")
        else "missing"
    )
    if reviews:
        checks["unique_project_review"] = review is not None
    if review:
        answer = await answer_source_review(
            client, external_user_id=external_user_id, review=review
        )
        record["review_answer"] = answer
        checks["restate_is_pending"] = answer.get("data") == {
            "review_id": review["id"],
            "action": "restate",
            "resolved": False,
            "next_step": "add_memory",
        }
        after_click = await retrieve(
            client,
            external_user_id=external_user_id,
            query=scenario["verification_query"],
        )
        record["after_click_retrieval"] = after_click
        checks["same_review_redelivered"] = any(
            r.get("id") == review["id"] and r.get("version") == review["version"]
            for r in after_click.get("source_reviews", [])
        )
        for name, memory_id in (("project", old_id), ("general", general_id)):
            snapshot = await memory_snapshot(client, memory_id)
            checks[f"click_did_not_change_{name}"] = memory_invariants(
                snapshot
            ) == memory_invariants(before[name])
    if not all(checks.values()):
        return {**record, "passed": False, "before": before}
    final_scenario = {**scenario, "update_messages": config["messages"]}
    final_add = await add_and_wait(
        client,
        build_add_payload(
            final_scenario,
            external_user_id=external_user_id,
            conversation_id=conversation_id,
            phase="update",
            fixture_version=fixture_version,
        ),
        idempotency_key=f"phase0:{run_id}:{scenario['id']}:restatement",
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.job_timeout,
    )
    record["restatement_add"] = final_add
    checks["restatement_job_completed"] = (final_add.get("terminal") or {}).get(
        "job", {}
    ).get("status") == "completed"
    if args.index_settle_seconds:
        await asyncio.sleep(args.index_settle_seconds)
    final = await retrieve(
        client, external_user_id=external_user_id, query=scenario["verification_query"]
    )
    record["final_retrieval"] = final
    try:
        replacement = select_memory(final, config["replacement"])
        final_general = select_memory(final, config["general"])
    except ValueError as exc:
        return {
            **record,
            "passed": False,
            "error": str(exc),
            "checks": {**checks, "scoped_replacement_present": False},
        }
    checks["replacement_id_verified"] = str(replacement["id"]) in created_memory_ids(
        final_add
    )
    checks["general_id_preserved"] = final_general["id"] == general_id
    checks["old_project_not_retrieved"] = all(
        m.get("id") != old_id for m in final["data"]
    )
    after = {
        name: await memory_snapshot(client, memory_id)
        for name, memory_id in (
            ("project", old_id),
            ("general", general_id),
            ("replacement", str(replacement["id"])),
        )
    }
    record.update(before=before, after=after)
    checks["old_project_archived"] = after["project"].get("is_archived") is True
    checks["replacement_active"] = after["replacement"].get("is_archived") is False
    checks["replacement_links_predecessor"] = (
        after["replacement"].get("previous_version_id") == old_id
    )
    checks["general_unchanged"] = memory_invariants(
        after["general"]
    ) == memory_invariants(before["general"])
    for name in ("project", "replacement"):
        response = await client.get(f"/v1/memories/{after[name]['id']}/history")
        response.raise_for_status()
        history = response.json()["data"]
        record[f"{name}_history"] = history
        checks[f"{name}_history_recorded"] = bool(history) and any(
            entry.get("content") == after[name]["content"] for entry in history
        )
        checks[f"{name}_history_transition"] = any(
            entry.get("change_type")
            in (
                {"conflict_update", "archived", "conflict_resolved"}
                if name == "project"
                else {"created"}
            )
            for entry in history
        )
    record["provenance_evaluation"] = evaluate_provenance(
        final,
        expected_conversation_id=conversation_id,
        expected_authority_label=scenario["expected"].get("authority_label"),
        expected_authority_level=scenario["expected"].get("authority_level"),
    )
    checks["final_provenance"] = record["provenance_evaluation"]["passed"]
    if review:
        checks["old_review_not_redelivered"] = all(
            r.get("id") != review["id"] for r in final.get("source_reviews", [])
        )
        stale = await answer_source_review(
            client,
            external_user_id=external_user_id,
            review=review,
            expected_http_status=409,
        )
        record["stale_review_probe"] = stale
        checks["old_review_unavailable"] = stale.get("code") == "REV_409"
        # Public API does not expose the pending row's final status. Do not invent it.
        record["review_resolution"] = (
            "unavailable_after_target_change_not_proof_of_resolution"
        )
    checks["no_final_clarification"] = not bool(final.get("clarification"))
    if scenario["expected"].get("verify_foreign_user_isolation"):
        foreign = await retrieve(
            client,
            external_user_id=f"phase0-foreign-{scenario['id']}-{run_id}",
            query=scenario["verification_query"],
        )
        record["final_isolation_retrieval"] = foreign
        checks["final_user_isolation"] = evaluate_isolation(foreign)["passed"]
    return {**record, "passed": all(checks.values())}


async def answer_clarification(
    client: httpx.AsyncClient,
    *,
    external_user_id: str,
    clarification: dict[str, Any],
    label_contains: str | None = None,
    answer: str | None = None,
    memory_ids: set[str] | None = None,
) -> dict[str, Any]:
    if sum(bool(value) for value in (label_contains, answer, memory_ids)) != 1:
        raise ValueError("Provide exactly one clarification selector")
    if label_contains:
        option = select_clarification_option(clarification, label_contains)
    elif answer:
        option = select_clarification_answer(clarification, answer)
    else:
        option = select_clarification_memory(clarification, memory_ids or set())
    clarification_id = str(clarification.get("id") or "")
    started = time.perf_counter()
    response = await client.post(
        f"/v1/memories/clarifications/{clarification_id}/answer",
        json={"external_user_id": external_user_id, "answer": option["answer"]},
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    response.raise_for_status()
    body = response.json()
    return {
        **_request_identity(response, body),
        "latency_ms": latency_ms,
        "selected_option": option,
        "data": body.get("data"),
    }


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    fixture = load_fixture(Path(args.fixture))
    api_key = os.environ.get("MEMORYOS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("MEMORYOS_API_KEY is required with --execute")

    output_path = Path(args.output) if args.output else None
    if args.resume:
        if output_path is None or not output_path.exists():
            raise ValueError("--resume requires an existing --output artifact")
        artifact = json.loads(output_path.read_text(encoding="utf-8"))
        if artifact.get("fixture_version") != fixture["version"]:
            raise ValueError("Resume artifact fixture version does not match")
        if artifact.get("base_url") != args.base_url.rstrip("/"):
            raise ValueError("Resume artifact base URL does not match")
        run_id = str(artifact["run_id"])
    else:
        run_id = uuid.uuid4().hex[:12]
        artifact = {
            "schema_version": 1,
            "fixture_version": fixture["version"],
            "run_id": run_id,
            "started_at": datetime.now(UTC).isoformat(),
            "base_url": args.base_url.rstrip("/"),
            "scenarios": [],
        }
    headers = {"Authorization": f"ApiKey {api_key}"}
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=args.request_timeout,
    ) as client:
        selected_scenarios = select_scenarios(
            fixture,
            scenario_ids=args.scenario,
            max_cases=args.max_cases,
        )
        completed_ids = {
            str(item.get("scenario_id"))
            for item in artifact.get("scenarios", [])
            if isinstance(item, dict)
        }
        selected_scenarios = [
            scenario
            for scenario in selected_scenarios
            if str(scenario["id"]) not in completed_ids
        ]
        for scenario in selected_scenarios:
            scenario_id = scenario["id"]
            external_user_id = f"phase0-{scenario_id}-{run_id}"
            conversation_id = f"phase0:{scenario_id}:{run_id}"
            setup = None
            if "setup_messages" in scenario:
                setup = await add_and_wait(
                    client,
                    build_add_payload(
                        scenario,
                        external_user_id=external_user_id,
                        conversation_id=conversation_id,
                        phase="setup",
                        fixture_version=str(fixture["version"]),
                    ),
                    idempotency_key=f"phase0:{run_id}:{scenario_id}:setup",
                    poll_seconds=args.poll_seconds,
                    timeout_seconds=args.job_timeout,
                )
            initial = await add_and_wait(
                client,
                build_add_payload(
                    scenario,
                    external_user_id=external_user_id,
                    conversation_id=conversation_id,
                    phase="initial",
                    fixture_version=str(fixture["version"]),
                ),
                idempotency_key=f"phase0:{run_id}:{scenario_id}:initial",
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.job_timeout,
            )
            if args.index_settle_seconds:
                await asyncio.sleep(args.index_settle_seconds)
            warm = await retrieve(
                client,
                external_user_id=external_user_id,
                query=scenario["warm_query"],
            )
            update = await add_and_wait(
                client,
                build_add_payload(
                    scenario,
                    external_user_id=external_user_id,
                    conversation_id=conversation_id,
                    phase="update",
                    fixture_version=str(fixture["version"]),
                ),
                idempotency_key=f"phase0:{run_id}:{scenario_id}:update",
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.job_timeout,
                expected_http_status=int(
                    scenario["expected"].get("update_http_status", 200)
                ),
            )
            idempotency_replay = None
            idempotency_evaluation = None
            if scenario["expected"].get("verify_idempotency"):
                idempotency_replay = await add_and_wait(
                    client,
                    build_add_payload(
                        scenario,
                        external_user_id=external_user_id,
                        conversation_id=conversation_id,
                        phase="update",
                        fixture_version=str(fixture["version"]),
                    ),
                    idempotency_key=f"phase0:{run_id}:{scenario_id}:update",
                    poll_seconds=args.poll_seconds,
                    timeout_seconds=args.job_timeout,
                )
                idempotency_evaluation = evaluate_idempotency(
                    update, idempotency_replay
                )
            immediate = await retrieve(
                client,
                external_user_id=external_user_id,
                query=scenario["verification_query"],
            )
            if args.index_settle_seconds:
                await asyncio.sleep(args.index_settle_seconds)
            settled = await retrieve(
                client,
                external_user_id=external_user_id,
                query=scenario["verification_query"],
            )
            initial_memory_ids = created_memory_ids(initial)
            scenario_evaluation = evaluate_scenario(
                immediate,
                settled,
                scenario["expected"],
                initial_memory_ids=initial_memory_ids,
            )
            provenance_source = settled
            resolution = None
            post_resolution = None
            post_resolution_evaluation = None
            resolve_to = scenario["expected"].get("resolve_to_label_contains")
            resolve_answer = scenario["expected"].get("resolve_with_answer")
            resolve_to_initial = bool(
                scenario["expected"].get("resolve_to_initial_memory")
            )
            if resolve_to or resolve_answer or resolve_to_initial:
                clarification = immediate.get("clarification") or settled.get(
                    "clarification"
                )
                if isinstance(clarification, dict):
                    resolution = await answer_clarification(
                        client,
                        external_user_id=external_user_id,
                        clarification=clarification,
                        label_contains=str(resolve_to) if resolve_to else None,
                        answer=str(resolve_answer) if resolve_answer else None,
                        memory_ids=initial_memory_ids if resolve_to_initial else None,
                    )
                    if args.index_settle_seconds:
                        await asyncio.sleep(args.index_settle_seconds)
                    post_resolution = await retrieve(
                        client,
                        external_user_id=external_user_id,
                        query=scenario["verification_query"],
                    )
                    selected_memory_id = str(
                        resolution["selected_option"].get("memory_id") or ""
                    )
                    post_expected = scenario["expected"]["after_resolution"]
                    post_resolution_evaluation = evaluate_retrieval(
                        post_resolution,
                        post_expected,
                        excluded_memory_ids=initial_memory_ids
                        if post_expected.get("exclude_initial_memory")
                        else set(),
                    )
                    post_ids = set(post_resolution_evaluation["observed"]["memory_ids"])
                    if selected_memory_id:
                        post_resolution_evaluation["checks"][
                            "selected_memory_present"
                        ] = selected_memory_id in post_ids
                    if resolve_answer:
                        resolution_data = resolution.get("data")
                        post_resolution_evaluation["checks"]["resolution_matches"] = (
                            isinstance(resolution_data, dict)
                            and resolution_data.get("resolution") == resolve_answer
                        )
                    post_resolution_evaluation["passed"] = all(
                        post_resolution_evaluation["checks"].values()
                    )
                    provenance_source = post_resolution
                else:
                    post_resolution_evaluation = {
                        "passed": False,
                        "checks": {"clarification_available_for_resolution": False},
                    }
            provenance_evaluation = evaluate_provenance(
                provenance_source,
                expected_conversation_id=conversation_id,
                expected_authority_label=scenario["expected"].get("authority_label"),
                expected_authority_level=scenario["expected"].get("authority_level"),
                allow_empty=bool(scenario["expected"].get("allow_empty_provenance")),
            )
            isolation_retrieval = None
            isolation_evaluation = None
            if scenario["expected"].get("verify_foreign_user_isolation"):
                isolation_retrieval = await retrieve(
                    client,
                    external_user_id=f"phase0-foreign-{scenario_id}-{run_id}",
                    query=scenario["verification_query"],
                )
                isolation_evaluation = evaluate_isolation(isolation_retrieval)
            restatement_evaluation = None
            if scenario.get("restatement"):
                restatement_evaluation = await replay_restatement(
                    client,
                    scenario=scenario,
                    warm=warm,
                    settled=settled,
                    initial=initial,
                    update=update,
                    external_user_id=external_user_id,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    fixture_version=str(fixture["version"]),
                    args=args,
                    setup=setup,
                )
            journey_checks = {
                "setup": setup is None
                or (
                    (setup.get("terminal") or {}).get("job", {}).get("status")
                    == "completed"
                    and bool(created_memory_ids(setup))
                ),
                "memory_policy": scenario_evaluation["passed"],
                "provenance": provenance_evaluation["passed"],
                "resolution": (
                    post_resolution_evaluation["passed"]
                    if post_resolution_evaluation is not None
                    else True
                ),
                "idempotency": (
                    idempotency_evaluation["passed"]
                    if idempotency_evaluation is not None
                    else True
                ),
                "isolation": (
                    isolation_evaluation["passed"]
                    if isolation_evaluation is not None
                    else True
                ),
                "update_rejection": (
                    update.get("http_status")
                    == scenario["expected"].get("update_http_status", 200)
                    and (
                        scenario["expected"].get("update_error_code") is None
                        or update.get("error_code")
                        == scenario["expected"].get("update_error_code")
                    )
                ),
                "restatement": restatement_evaluation["passed"]
                if restatement_evaluation is not None
                else True,
            }
            journey_evaluation = {
                "passed": all(journey_checks.values()),
                "checks": journey_checks,
            }
            record = {
                "scenario_id": scenario_id,
                "language": scenario.get("language", "legacy"),
                "family": scenario.get("family", "legacy"),
                "safety_critical": bool(scenario.get("safety_critical")),
                "external_user_id": external_user_id,
                "conversation_id": conversation_id,
                "initial_add": initial,
                "setup_add": setup,
                "warm_retrieval": warm,
                "update_add": update,
                "idempotency_replay": idempotency_replay,
                "idempotency_evaluation": idempotency_evaluation,
                "immediate_retrieval": immediate,
                "settled_retrieval": settled,
                "resolution": resolution,
                "post_resolution_retrieval": post_resolution,
                "post_resolution_evaluation": post_resolution_evaluation,
                "provenance_evaluation": provenance_evaluation,
                "isolation_retrieval": isolation_retrieval,
                "isolation_evaluation": isolation_evaluation,
                "restatement_evaluation": restatement_evaluation,
                "immediate_evaluation": evaluate_retrieval(
                    immediate, scenario["expected"]
                ),
                "settled_evaluation": evaluate_retrieval(settled, scenario["expected"]),
                "scenario_evaluation": scenario_evaluation,
                "journey_evaluation": journey_evaluation,
            }
            artifact["scenarios"].append(record)
            if args.output:
                artifact["checkpointed_at"] = datetime.now(UTC).isoformat()
                write_artifact(artifact, Path(args.output))
            print(
                f"{scenario_id}: scenario={scenario_evaluation['passed']} "
                f"immediate_state={scenario_evaluation['checks']['immediate_memory_state']} "
                f"settled_state={scenario_evaluation['checks']['settled_memory_state']} "
                f"journey={journey_evaluation['passed']}"
            )

    artifact["completed_at"] = datetime.now(UTC).isoformat()
    artifact["summary"] = {
        "scenario_count": len(artifact["scenarios"]),
        "immediate_passed": sum(
            1
            for item in artifact["scenarios"]
            if item["scenario_evaluation"]["checks"]["immediate_memory_state"]
        ),
        "settled_passed": sum(
            1
            for item in artifact["scenarios"]
            if item["scenario_evaluation"]["checks"]["settled_memory_state"]
        ),
        "scenarios_passed": sum(
            1 for item in artifact["scenarios"] if item["scenario_evaluation"]["passed"]
        ),
        "journeys_passed": sum(
            1 for item in artifact["scenarios"] if item["journey_evaluation"]["passed"]
        ),
        "provenance_passed": sum(
            1
            for item in artifact["scenarios"]
            if item["provenance_evaluation"]["passed"]
        ),
        "safety_critical_failures": [
            item["scenario_id"]
            for item in artifact["scenarios"]
            if item["safety_critical"] and not item["journey_evaluation"]["passed"]
        ],
        "by_language": _slice_summary(artifact["scenarios"], "language"),
        "by_family": _slice_summary(artifact["scenarios"], "family"),
        "timing_ms": _timing_summary(artifact["scenarios"]),
    }
    return artifact


def _slice_summary(
    scenarios: list[dict[str, Any]], field: str
) -> dict[str, dict[str, int]]:
    values = sorted({str(item.get(field) or "unknown") for item in scenarios})
    return {
        value: {
            "total": sum(
                1 for item in scenarios if str(item.get(field) or "unknown") == value
            ),
            "passed": sum(
                1
                for item in scenarios
                if str(item.get(field) or "unknown") == value
                and item["journey_evaluation"]["passed"]
            ),
        }
        for value in values
    }


def _timing_stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    p95_index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1))
    return {
        "count": len(ordered),
        "p50": round(statistics.median(ordered), 2),
        "p95": round(ordered[p95_index], 2),
        "max": round(ordered[-1], 2),
    }


def _timing_summary(
    scenarios: list[dict[str, Any]],
) -> dict[str, dict[str, float | int | None]]:
    acknowledgements: list[float] = []
    job_polling: list[float] = []
    retrievals: list[float] = []
    clarifications: list[float] = []
    reviews: list[float] = []
    for scenario in scenarios:
        restatement = scenario.get("restatement_evaluation") or {}
        for field in ("setup_add", "initial_add", "update_add", "idempotency_replay"):
            operation = scenario.get(field)
            if not isinstance(operation, dict):
                continue
            acknowledgement = operation.get("acknowledgement_ms")
            if isinstance(acknowledgement, (int, float)):
                acknowledgements.append(float(acknowledgement))
            terminal = operation.get("terminal")
            if isinstance(terminal, dict) and isinstance(
                terminal.get("latency_ms"), (int, float)
            ):
                job_polling.append(float(terminal["latency_ms"]))
        for field in (
            "warm_retrieval",
            "immediate_retrieval",
            "settled_retrieval",
            "post_resolution_retrieval",
            "isolation_retrieval",
        ):
            operation = scenario.get(field)
            if isinstance(operation, dict) and isinstance(
                operation.get("latency_ms"), (int, float)
            ):
                retrievals.append(float(operation["latency_ms"]))
        resolution = scenario.get("resolution")
        if isinstance(resolution, dict) and isinstance(
            resolution.get("latency_ms"), (int, float)
        ):
            clarifications.append(float(resolution["latency_ms"]))
        for field in ("review_answer", "stale_review_probe"):
            operation = restatement.get(field) or {}
            if isinstance(operation.get("latency_ms"), (int, float)):
                reviews.append(float(operation["latency_ms"]))
        final_add = restatement.get("restatement_add") or {}
        if isinstance(final_add.get("acknowledgement_ms"), (int, float)):
            acknowledgements.append(float(final_add["acknowledgement_ms"]))
        terminal = final_add.get("terminal") or {}
        if isinstance(terminal.get("latency_ms"), (int, float)):
            job_polling.append(float(terminal["latency_ms"]))
        for field in (
            "after_click_retrieval",
            "final_retrieval",
            "final_isolation_retrieval",
        ):
            operation = restatement.get(field) or {}
            if isinstance(operation.get("latency_ms"), (int, float)):
                retrievals.append(float(operation["latency_ms"]))
    return {
        "add_acknowledgement": _timing_stats(acknowledgements),
        "job_polling": _timing_stats(job_polling),
        "retrieval": _timing_stats(retrievals),
        "clarification_answer": _timing_stats(clarifications),
        "source_review_answer": _timing_stats(reviews),
    }


def select_scenarios(
    fixture: dict[str, Any],
    *,
    scenario_ids: list[str] | None,
    max_cases: int | None,
) -> list[dict[str, Any]]:
    scenarios = list(fixture["scenarios"])
    if scenario_ids:
        by_id = {str(item["id"]): item for item in scenarios}
        missing = [
            scenario_id for scenario_id in scenario_ids if scenario_id not in by_id
        ]
        if missing:
            raise ValueError(f"Unknown scenario IDs: {', '.join(missing)}")
        scenarios = [by_id[scenario_id] for scenario_id in scenario_ids]
    if max_cases is not None:
        if max_cases < 1:
            raise ValueError("max_cases must be at least 1")
        scenarios = scenarios[:max_cases]
    return scenarios


def write_artifact(payload: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MEMORYOS_API_BASE_URL", DEFAULT_BASE_URL),
    )
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--output")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--job-timeout", type=float, default=120.0)
    parser.add_argument("--index-settle-seconds", type=float, default=6.0)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--scenario", action="append")
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fixture = load_fixture(Path(args.fixture))
    if not args.execute:
        selected_scenarios = select_scenarios(
            fixture,
            scenario_ids=args.scenario,
            max_cases=args.max_cases,
        )
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "base_url": args.base_url.rstrip("/"),
                    "fixture_version": fixture["version"],
                    "scenario_ids": [item["id"] for item in selected_scenarios],
                    "scenario_count": len(selected_scenarios),
                    "safety_critical_count": sum(
                        bool(item.get("safety_critical")) for item in selected_scenarios
                    ),
                    "note": "Pass --execute with MEMORYOS_API_KEY set to create fresh synthetic users.",
                },
                indent=2,
            )
        )
        return 0

    artifact = asyncio.run(execute(args))
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = (
        Path(args.output)
        if args.output
        else (
            ROOT
            / "artifacts"
            / "internal-benchmarks"
            / "phase0"
            / f"governed-memory-{timestamp}.json"
        )
    )
    write_artifact(artifact, output)
    print(f"artifact={output}")
    print(json.dumps(artifact["summary"], sort_keys=True))
    return (
        0
        if artifact["summary"]["journeys_passed"]
        == artifact["summary"]["scenario_count"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
