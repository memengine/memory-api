from __future__ import annotations

import sys
from pathlib import Path

import httpx


SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from memoryos.client import Memory  # noqa: E402
from memoryos.types import AddResult  # noqa: E402
from memoryos.types import EdTechMemoryProfile  # noqa: E402
from memoryos.types import MemoryRecord  # noqa: E402
from memoryos.types import MemoryResult  # noqa: E402
from memoryos.types import RetrieveResult  # noqa: E402


def test_memory_importance_trend_properties() -> None:
    rising = MemoryResult(
        id="mem_1",
        content="User likes Python",
        category="preference",
        importance_score=8.2,
        original_importance_score=7.0,
        relevance_score=0.9,
        context_snippet="- User likes Python",
    )
    decaying = MemoryRecord(
        id="mem_2",
        content="User used Angular",
        category="fact",
        importance_score=3.0,
        original_importance_score=5.0,
        confidence_score=0.8,
    )

    assert rising.importance_delta == 1.2
    assert rising.importance_trend == "rising"
    assert decaying.importance_delta == -2.0
    assert decaying.importance_trend == "decaying"


def test_add_and_retrieve_convenience_properties() -> None:
    queued = AddResult(status="queued")
    empty = AddResult(status="queued", nothing_to_extract=True)
    blocked = AddResult(status="blocked")
    retrieve = RetrieveResult(system_prompt_addition="What you know about this user:\n- Uses Python")
    passthrough = RetrieveResult(system_prompt_addition="ignored", is_passthrough=True)

    assert queued.was_stored is True
    assert empty.was_stored is False
    assert blocked.was_stored is False
    assert retrieve.has_context is True
    assert passthrough.has_context is False


def test_sync_get_sends_new_retrieve_parameters_and_omits_null_time_filter() -> None:
    captured_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_body
        captured_body = dict(__import__("json").loads(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "request_id": "req_1",
                "timestamp": "2026-05-10T00:00:00Z",
                "data": [
                    {
                        "id": "mem_1",
                        "content": "User prefers Python",
                        "category": "preference",
                        "importance_score": 8.0,
                        "original_importance_score": 7.5,
                        "last_accessed": None,
                        "relevance_score": 0.95,
                        "context_snippet": "- User prefers Python",
                        "access_count": 4,
                        "is_hot": True,
                        "system_archived": False,
                    }
                ],
                "cached": False,
                "system_prompt_addition": '{"preference":["User prefers Python"]}',
                "context_token_count": 12,
                "memories_from_hot_tier": 1,
                "quota_mode": "FULL",
            },
        )

    client = Memory(api_key="mem_test", base_url="https://api.example.com")
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.example.com",
        headers={"Authorization": "ApiKey mem_test"},
    )

    result = client.get(
        query="preferences",
        external_user_id="user_1",
        format="json",
        context_max_tokens=300,
    )

    assert captured_body["format"] == "json"
    assert captured_body["context_max_tokens"] == 300
    assert "time_filter_days" not in captured_body
    assert result.context_token_count == 12
    assert result.memories_from_hot_tier == 1
    assert result.items[0].is_hot is True
    assert result.items[0].access_count == 4


def test_sync_add_parses_nothing_to_extract() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "req_1",
                "timestamp": "2026-05-10T00:00:00Z",
                "job_id": "job_1",
                "status": "queued",
                "nothing_to_extract": True,
            },
        )

    client = Memory(api_key="mem_test", base_url="https://api.example.com")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.example.com")

    result = client.add(
        messages=[{"role": "user", "content": "hi"}],
        external_user_id="user_1",
    )

    assert result.nothing_to_extract is True
    assert result.was_stored is False


def test_sync_get_edtech_profile_parses_domain_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/memories/edtech-profile"
        assert request.url.params["external_user_id"] == "student_1"
        return httpx.Response(
            200,
            json={
                "request_id": "req_1",
                "timestamp": "2026-05-10T00:00:00Z",
                "data": {
                    "id": "edtech_1",
                    "proxy_user_id": "proxy_1",
                    "tenant_id": "tenant_1",
                    "grade_level": "Class 10",
                    "board_or_curriculum": "CBSE",
                    "subjects": [{"subject": "Math", "confidence": 4}],
                    "syllabus_stage": {"Math": 0.4},
                    "strong_topics": [{"topic": "Algebra"}],
                    "weak_topics": [{"topic": "Thermodynamics", "severity": "moderate"}],
                    "concept_gaps": [],
                    "misconceptions": [],
                    "explanation_style": {"primary": "worked examples"},
                    "language_profile": {"explanation_preference": "Hinglish"},
                    "exam_name": "Boards",
                    "exam_date": "2026-03-01",
                    "mock_scores": [],
                    "forgetting_stages": {},
                    "improvement_velocity": {},
                    "schema_version": 1,
                    "extraction_source_job_ids": [],
                },
            },
        )

    client = Memory(api_key="mem_test", base_url="https://api.example.com")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.example.com")

    profile = client.get_edtech_profile("student_1")

    assert isinstance(profile, EdTechMemoryProfile)
    assert profile.grade_level == "Class 10"
    assert profile.subjects[0]["subject"] == "Math"
    assert profile.has_exam_context is True
    assert profile.has_learning_profile is True
