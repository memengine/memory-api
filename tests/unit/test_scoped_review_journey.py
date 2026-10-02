from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from api.schemas.requests import MemoryAddRequest, MemorySourceReviewAnswerRequest

ROOT = Path(__file__).resolve().parents[2]
DATASET = (
    ROOT
    / "benchmarks/internal/datasets/governed_memory/development/scoped_review_v1.json"
)
SEPARATE_DATASET = DATASET.with_name("scoped_review_separate_writes_v1.json")
SPEC = importlib.util.spec_from_file_location(
    "scoped_review_replay", ROOT / "scripts/governed_memory_phase0_replay.py"
)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_scoped_fixture_uses_public_contracts_and_keeps_existing_dataset_frozen():
    fixture = runner.load_fixture(DATASET)
    assert fixture["split"] == "development" and len(fixture["scenarios"]) == 1
    scenario = fixture["scenarios"][0]
    for phase in ("initial", "update"):
        MemoryAddRequest.model_validate(
            runner.build_add_payload(
                scenario,
                external_user_id="test",
                conversation_id="test",
                phase=phase,
            )
        )
    MemoryAddRequest.model_validate(
        runner.build_add_payload(
            {**scenario, "update_messages": scenario["restatement"]["messages"]},
            external_user_id="test",
            conversation_id="test",
            phase="update",
        )
    )
    assert (
        len(runner.load_fixture(DATASET.with_name("journeys_v1.json"))["scenarios"])
        == 42
    )


@pytest.mark.parametrize(
    "mutation",
    ["missing_selector", "assistant_restatement", "mixed_resolution", "no_attention"],
)
def test_invalid_restatement_fails_before_network(mutation):
    fixture = runner.load_fixture(DATASET)
    scenario = fixture["scenarios"][0]
    if mutation == "missing_selector":
        scenario["restatement"]["general"] = {}
    elif mutation == "assistant_restatement":
        scenario["restatement"]["messages"][0]["role"] = "assistant"
    elif mutation == "mixed_resolution":
        scenario["expected"].update(
            resolve_with_answer="A", after_resolution={"clarification_required": False}
        )
    else:
        scenario["expected"]["governance_attention_required"] = False
    with pytest.raises(ValueError):
        runner.validate_fixture(fixture)


def test_project_selector_cannot_be_satisfied_by_general_cpp_or_multiple_matches():
    selector = runner.load_fixture(DATASET)["scenarios"][0]["restatement"][
        "initial_project"
    ]
    with pytest.raises(ValueError):
        runner.select_memory(
            {"data": [{"id": "general", "content": "My general default is C++."}]},
            selector,
        )
    with pytest.raises(ValueError):
        runner.select_memory(
            {
                "data": [
                    {"id": str(i), "content": "Release Check uses C++."}
                    for i in range(2)
                ]
            },
            selector,
        )


@pytest.mark.parametrize(
    "attention",
    [{"source_reviews": [{"id": "review"}]}, {"clarification": {"id": "choice"}}, {}],
)
def test_attention_accepts_review_or_clarification_but_not_silent_fallback(attention):
    body = {"data": [{"id": "old", "content": "Release Check uses C++."}], **attention}
    expected = runner.load_fixture(DATASET)["scenarios"][0]["expected"]
    result = runner.evaluate_scenario(body, body, expected, initial_memory_ids={"old"})
    assert result["passed"] is bool(attention)


def test_foreign_user_review_is_a_leak_even_without_memories():
    assert not runner.evaluate_isolation(
        {"data": [], "source_reviews": [{"id": "foreign"}]}
    )["passed"]


@pytest.mark.parametrize("passed,exit_status", [(0, 1), (1, 0)])
def test_live_replay_exit_status_matches_evaluation(monkeypatch, passed, exit_status):
    # No actual execution or output writes: failed evaluations must not look green.
    async def controlled_execute(_args):
        return {"summary": {"journeys_passed": passed, "scenario_count": 1}}

    monkeypatch.setattr(runner, "execute", controlled_execute)
    monkeypatch.setattr(
        runner,
        "parse_args",
        lambda: argparse.Namespace(
            fixture=str(DATASET),
            execute=True,
            output="unused-test-artifact.json",
        ),
    )
    monkeypatch.setattr(runner, "write_artifact", lambda *_args: None)
    assert runner.main() == exit_status


class JourneyAPI:
    """Controlled transport, not model-quality or real database evidence."""

    def __init__(self, *, path="source_review", failure=None, separate=False):
        self.attention_path, self.failure = path, failure
        self.separate = separate
        self.calls = []
        self.add_count = 0
        self.review = {
            "id": "review-1",
            "version": "a" * 64,
            "target_memory_id": "project",
            "actions": ["keep_current", "restate", "dismiss"],
        }
        self.memories = {
            "general": {
                "id": "general",
                "content": "My general default language is C++.",
            },
            "project": {"id": "project", "content": "For Release Check only, use C++."},
            "replacement": {
                "id": "replacement",
                "content": "For Release Check only, Python replaces C++.",
            },
            "candidate": {
                "id": "candidate",
                "content": "Python could be used for Release Check.",
            },
        }

    def handle(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == "/v1/memories/add":
            MemoryAddRequest.model_validate(body)
            self.add_count += 1
            self.user_id, self.conversation_id = (
                body["external_user_id"],
                body["conversation_id"],
            )
            return httpx.Response(
                200, json={"status": "queued", "job_id": f"job-{self.add_count}"}
            )
        if "/jobs/" in path:
            phase = int(path.rsplit("-", 1)[1]) - int(self.separate)
            ids = (
                ["general"]
                if phase == 0
                else ["project"]
                if phase == 1 and self.separate
                else ["general", "project"]
                if phase == 1
                else ["candidate"]
                if phase == 2 and self.attention_path == "clarification"
                else ["replacement"]
                if phase == 3
                else []
            )
            if phase == 3 and self.failure == "wrong_created_ids":
                ids = ["different"]
            if phase == 0 and self.failure == "wrong_setup_ids":
                ids = ["different"]
            status = (
                "error"
                if phase == 0 and self.failure == "setup_failed"
                else "completed"
            )
            return httpx.Response(
                200, json={"data": {"status": status, "created_memory_ids": ids}}
            )
        if path == "/v1/memories/retrieve":
            if body["external_user_id"] != self.user_id:
                return httpx.Response(200, json={"data": [], "source_reviews": []})
            ids = (
                ["general", "replacement"]
                if self.logical_phase == 3
                else ["general", "project"]
            )
            if self.logical_phase == 3 and self.failure == "old_project_leak":
                ids.append("project")
            if self.logical_phase == 3 and self.failure == "missing_project":
                ids.remove("replacement")
            if self.failure == "scope_erasing_merge":
                ids = ["project"]
            attention = {}
            if self.logical_phase == 2:
                attention = (
                    {"source_reviews": [self.review]}
                    if self.attention_path == "source_review"
                    else {"clarification": {"id": "choice-1"}}
                )
            return httpx.Response(
                200, json={"data": [self.memory(i) for i in ids], **attention}
            )
        if "/source-reviews/" in path:
            MemorySourceReviewAnswerRequest.model_validate(body)
            assert (
                body["action"] == "restate" and body["external_user_id"] == self.user_id
            )
            if self.logical_phase == 3:
                return httpx.Response(
                    409,
                    json={"code": "REV_409", "error": "source_review_stale_or_closed"},
                )
            if self.failure == "click_changes_general":
                self.memories["general"]["content"] = (
                    "My general default language is Rust."
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "review_id": "review-1",
                        "action": "restate",
                        "resolved": False,
                        "next_step": "add_memory",
                    }
                },
            )
        if path.endswith("/history"):
            memory_id = path.split("/")[-2]
            history = (
                []
                if self.failure == "missing_history"
                else [
                    {
                        "version_number": 1,
                        "content": self.memory(memory_id)["content"],
                        "change_type": "conflict_update"
                        if memory_id == "project"
                        else "created",
                    }
                ]
            )
            return httpx.Response(200, json={"data": history})
        if request.method == "GET" and "/memories/" in path:
            return httpx.Response(
                200, json={"data": self.memory(path.rsplit("/", 1)[1])}
            )
        raise AssertionError(f"Unexpected route {path}")

    @property
    def logical_phase(self):
        return self.add_count - int(self.separate)

    def memory(self, memory_id):
        item = copy.deepcopy(self.memories[memory_id])
        item["is_archived"] = memory_id == "candidate" or (
            memory_id == "project" and self.logical_phase == 3
        )
        item["previous_version_id"] = "project" if memory_id == "replacement" else None
        if self.failure == "scope_erasing_merge":
            if memory_id == "general":
                item["is_archived"] = True
            elif memory_id == "project":
                item["content"] = self.memories["general"]["content"]
                item["previous_version_id"] = "general"
        if (
            self.logical_phase == 3
            and self.failure == "missing_archive"
            and memory_id == "project"
        ):
            item["is_archived"] = False
        if self.failure == "wrong_predecessor" and memory_id == "replacement":
            item["previous_version_id"] = "general"
        if (
            self.logical_phase == 3
            and self.failure == "changed_general"
            and memory_id == "general"
        ):
            item["content"] = "My general default language is C++ and Rust."
        level = (
            60
            if self.failure == "authority_escalation" and memory_id == "replacement"
            else 20
        )
        item["provenance"] = {
            "event_id": f"event-{memory_id}",
            "external_conversation_id": self.conversation_id,
            "extraction_evidence": {
                "authority": {"label": "client_assertion", "level": level},
                "turn_references": [{"role": "user", "turn_id": "source-1"}],
            },
        }
        return item


async def run_journey(monkeypatch, api, fixture=DATASET):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        runner.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            **kwargs, transport=httpx.MockTransport(api.handle)
        ),
    )
    monkeypatch.setenv("MEMORYOS_API_KEY", "test-no-network")
    args = argparse.Namespace(
        fixture=str(fixture),
        output=None,
        resume=False,
        base_url="https://api.invalid",
        request_timeout=1,
        scenario=None,
        max_cases=1,
        poll_seconds=0,
        job_timeout=1,
        index_settle_seconds=0,
    )
    return await runner.execute(args)


def test_separate_fixture_changes_only_write_grouping_and_preserves_failed_batch():
    assert hashlib.sha256(DATASET.read_bytes()).hexdigest() == (
        "9042aa004369ea94de51b16ca72c72bb58c0e2572e1b4eec758d35d893a412f4"
    )
    batch = runner.load_fixture(DATASET)["scenarios"][0]
    separate = runner.load_fixture(SEPARATE_DATASET)["scenarios"][0]
    assert (
        separate["setup_messages"] + separate["initial_messages"]
        == batch["initial_messages"]
    )
    for field in (
        "update_messages",
        "warm_query",
        "verification_query",
        "expected",
        "restatement",
    ):
        assert separate[field] == batch[field]
    for phase in ("setup", "initial", "update"):
        MemoryAddRequest.model_validate(
            runner.build_add_payload(
                separate, external_user_id="test", conversation_id="test", phase=phase
            )
        )


@pytest.mark.parametrize(
    "messages",
    [
        [],
        None,
        [{"role": "tool", "content": "fake"}],
        [{"role": "user", "content": ""}],
        ["invalid"],
    ],
)
def test_invalid_setup_rejected_before_network(messages):
    fixture = runner.load_fixture(SEPARATE_DATASET)
    fixture["scenarios"][0]["setup_messages"] = messages
    with pytest.raises(ValueError):
        runner.validate_fixture(fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["source_review", "clarification"])
async def test_separate_writes_wait_for_each_job_and_verify_individual_ids(
    monkeypatch, path
):
    api = JourneyAPI(path=path, separate=True)
    artifact = await run_journey(monkeypatch, api, SEPARATE_DATASET)
    result = artifact["scenarios"][0]
    assert result["journey_evaluation"]["passed"], result
    assert runner.created_memory_ids(result["setup_add"]) == {"general"}
    assert runner.created_memory_ids(result["initial_add"]) == {"project"}
    writes = [
        (i, body) for i, (_, p, body) in enumerate(api.calls) if p == "/v1/memories/add"
    ]
    assert len(writes) == 4
    assert {body["external_user_id"] for _, body in writes} == {
        result["external_user_id"]
    }
    assert {body["conversation_id"] for _, body in writes} == {
        result["conversation_id"]
    }
    for (i, body), (j, _) in zip(writes, writes[1:]):
        assert len(body["messages"]) == 1
        assert any("/jobs/" in p for _, p, _ in api.calls[i + 1 : j])
    assert artifact["summary"]["timing_ms"]["add_acknowledgement"]["count"] == 4
    assert artifact["summary"]["timing_ms"]["job_polling"]["count"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["wrong_setup_ids", "setup_failed", "scope_erasing_merge"]
)
async def test_setup_failure_cannot_pass_or_trigger_review_or_replacement(
    monkeypatch, failure
):
    api = JourneyAPI(separate=True, failure=failure)
    artifact = await run_journey(monkeypatch, api, SEPARATE_DATASET)
    assert artifact["summary"]["journeys_passed"] == 0
    assert api.add_count == 3
    assert not any("/source-reviews/" in p for _, p, _ in api.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["source_review", "clarification"])
async def test_existing_runner_replays_all_stages_without_browser_history_or_production(
    monkeypatch, path
):
    api = JourneyAPI(path=path)
    artifact = await run_journey(monkeypatch, api)
    result = artifact["scenarios"][0]
    assert result["journey_evaluation"]["passed"], result
    evidence = result["restatement_evaluation"]
    assert evidence["passed"] and evidence["attention_path"] == path
    assert api.add_count == 3
    if path == "source_review":
        assert (
            evidence["review_resolution"]
            == "unavailable_after_target_change_not_proof_of_resolution"
        )
        click_index = next(
            i for i, (_, p, _) in enumerate(api.calls) if p.endswith("review-1/answer")
        )
        next_write = next(
            i
            for i, (_, p, _) in enumerate(api.calls[click_index + 1 :], click_index + 1)
            if p == "/v1/memories/add"
        )
        assert all(
            p != "/v1/memories/add"
            for _, p, _ in api.calls[click_index + 1 : next_write]
        )
        assert (
            evidence["after_click_retrieval"]["source_reviews"][0]["id"] == "review-1"
        )
    assert all(
        "history" not in body for _, p, body in api.calls if p.endswith("/retrieve")
    )
    timings = artifact["summary"]["timing_ms"]
    assert (
        timings["add_acknowledgement"]["count"] == timings["job_polling"]["count"] == 3
    )
    assert timings["source_review_answer"]["count"] == (
        2 if path == "source_review" else 0
    )
    assert "test-no-network" not in json.dumps(artifact)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing_project",
        "old_project_leak",
        "wrong_created_ids",
        "missing_archive",
        "wrong_predecessor",
        "changed_general",
        "authority_escalation",
        "missing_history",
        "click_changes_general",
    ],
)
async def test_journey_rejects_answer_like_success_without_governance_evidence(
    monkeypatch, failure
):
    artifact = await run_journey(monkeypatch, JourneyAPI(failure=failure))
    assert artifact["summary"]["journeys_passed"] == 0
    assert artifact["summary"]["safety_critical_failures"] == [
        "en_scoped_review_restatement"
    ]
