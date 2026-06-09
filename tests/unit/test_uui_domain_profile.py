from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime

from api.db.models import UniversalMemory
from api.routers.uui import _edtech_profile_from_memories
from api.routers.uui import _memory_domain
from api.routers.uui import _memory_field
from api.schemas.uui_schemas import ClarificationItem
from api.schemas.uui_schemas import DomainProfileData


def _memory(content: str, *, field: str, metadata: dict | None = None) -> UniversalMemory:
    return UniversalMemory(
        id=uuid.uuid4(),
        user_uui_id=uuid.uuid4(),
        source_agent_id=uuid.uuid4(),
        content=content,
        category="fact",
        importance_score=7.0,
        confidence=0.8,
        created_at=datetime.now(UTC),
        last_accessed_at=datetime.now(UTC),
        is_archived=False,
        metadata_json={
            "source_domain": "edtech",
            "source_field": field,
            **(metadata or {}),
        },
    )


def test_edtech_domain_profile_assembles_from_universal_memory_metadata() -> None:
    profile = _edtech_profile_from_memories(
        [
            _memory("Student is in Class 11.", field="academic_profile.grade_level"),
            _memory("Student follows CBSE.", field="academic_profile.board_or_curriculum"),
            _memory("Student is preparing for UPSC.", field="exam_context.exam_name"),
            _memory(
                "Student is working on improving integration.",
                field="knowledge_state.weak_topic.integration",
                metadata={"severity": "severe", "attempts": 3},
            ),
            _memory(
                "Student is strong in biology.",
                field="knowledge_state.strong_topic.biology",
                metadata={"confidence": 0.9},
            ),
        ]
    )

    assert profile.grade_level == "Class 11"
    assert profile.board == "CBSE"
    assert profile.exam_name == "UPSC"
    assert profile.weak_topics[0].topic == "integration"
    assert profile.weak_topics[0].severity == "severe"
    assert profile.weak_topics[0].attempts == 3
    assert profile.strong_topics[0].topic == "biology"
    assert profile.total_edtech_memories == 5


def test_domain_profile_schema_supports_no_detected_domain() -> None:
    data = DomainProfileData(detected_domain=None, edtech_profile=None)

    assert data.detected_domain is None
    assert data.edtech_profile is None


def test_clarification_item_exposes_domain_fields_and_value_ages() -> None:
    item = ClarificationItem(
        id=uuid.uuid4(),
        question_context="Class 10 vs Class 11",
        status="pending",
        entity_type="grade_level",
        domain="edtech",
        field="grade_level",
        value_a="Class 10",
        value_b="Class 11",
        value_a_age_days=45,
        value_b_age_days=3,
    )

    assert item.domain == "edtech"
    assert item.field == "grade_level"
    assert item.value_a_age_days == 45
    assert item.value_b_age_days == 3


def test_memory_domain_helpers_accept_projection_metadata_names() -> None:
    memory = _memory("Student is in Class 11.", field="academic_profile.grade_level")

    assert _memory_domain(memory) == "edtech"
    assert _memory_field(memory) == "academic_profile.grade_level"
