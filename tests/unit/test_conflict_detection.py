from __future__ import annotations

import uuid
from types import SimpleNamespace

from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import SharedContextEntityType
from api.services.conflict_detection import ConflictDetector
from api.services.conflict_detection import ConflictType
from api.services.conflict_detection import classify_conflict_type
from api.services.conflict_detection import extract_entities
from api.services.extractor import ExtractedMemory
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID


def make_existing_memory(content: str, category: MemoryCategory = MemoryCategory.fact) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        content=content,
        category=category,
        importance_score=7.0,
        confidence_score=0.9,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id=DEFAULT_ACTIVE_MODEL_ID,
        source_conversation_id=uuid.uuid4(),
        metadata_json={},
    )


def make_new_memory(content: str, category: str = "fact") -> ExtractedMemory:
    return ExtractedMemory(
        content=content,
        category=category,
        importance_score=8.0,
        confidence=0.95,
        expiry="permanent",
        reasoning="Test memory",
    )


def test_semantic_strategy_uses_lower_082_threshold() -> None:
    existing = make_existing_memory("User prefers concise explanations")
    setattr(existing, "_conflict_similarity_score", 0.83)

    candidates = ConflictDetector().detect_candidates(
        make_new_memory("User likes brief explanations"),
        [existing],
    )

    assert len(candidates) == 1
    assert candidates[0].detection_strategy == "semantic"


def test_entity_strategy_catches_date_conflict_below_semantic_threshold() -> None:
    existing = make_existing_memory("User's certification exam is in March")
    setattr(existing, "_conflict_similarity_score", 0.70)

    candidates = ConflictDetector().detect_candidates(
        make_new_memory("User's certification exam is in April"),
        [existing],
    )

    assert len(candidates) == 1
    assert candidates[0].detection_strategy == "entity"
    assert set(candidates[0].detected_entities) >= {"march", "april"}


def test_topic_overlap_strategy_catches_same_category_same_topic() -> None:
    existing = make_existing_memory(
        "User prefers concise Python examples",
        category=MemoryCategory.preference,
    )
    setattr(existing, "_conflict_similarity_score", 0.40)

    candidates = ConflictDetector().detect_candidates(
        make_new_memory("User prefers detailed Python examples", category="preference"),
        [existing],
    )

    assert len(candidates) == 1
    assert candidates[0].detection_strategy == "topic_overlap"


def test_rule_based_conflict_type_classifier() -> None:
    assert (
        classify_conflict_type(
            "User now prefers detailed answers",
            "User prefers concise answers",
            "preference",
            [],
        )
        == ConflictType.PREFERENCE_CHANGE
    )
    assert (
        classify_conflict_type(
            "User no longer uses JavaScript",
            "User uses JavaScript",
            "fact",
            ["javascript"],
        )
        == ConflictType.NEGATION
    )
    assert (
        classify_conflict_type(
            "User scored 91%",
            "User scored 87%",
            "fact",
            ["91%", "87%"],
        )
        == ConflictType.NUMERIC_UPDATE
    )
    assert (
        classify_conflict_type(
            "User's certification exam got postponed to April",
            "User's certification exam is in March",
            "fact",
            ["march", "april"],
        )
        == ConflictType.FACT_UPDATE
    )


def test_entity_extraction_covers_required_entity_types() -> None:
    entities = extract_entities(
        "Aditya scored 87% in March using Python, FastAPI, React, and English."
    )

    assert {"aditya", "87%", "march", "python", "fastapi", "react", "english"} <= entities


def test_shared_context_entities_focus_on_team_level_signals() -> None:
    entities = ConflictDetector().extract_shared_context_entities(
        make_new_memory(
            "Our team stack uses Python, FastAPI, React, and daily standup.",
            category="expertise",
        )
    )

    pairs = {(entity.entity_type, entity.entity_value) for entity in entities}

    assert (SharedContextEntityType.tech_stack, "python") in pairs
    assert (SharedContextEntityType.tech_stack, "fastapi") in pairs
    assert (SharedContextEntityType.tech_stack, "react") in pairs
    assert (SharedContextEntityType.team_process, "daily standup") in pairs


def test_personal_goal_does_not_create_shared_goal_signal() -> None:
    entities = ConflictDetector().extract_shared_context_entities(
        make_new_memory(
            "User wants to become an actor in the South Indian film industry.",
            category="goal",
        )
    )

    assert all(entity.entity_type != SharedContextEntityType.shared_goal for entity in entities)


def test_shared_context_conflict_detects_other_user_signal_only() -> None:
    tenant_id = str(uuid.uuid4())
    current_proxy_user_id = str(uuid.uuid4())
    other_proxy_user_id = uuid.uuid4()
    signal = SimpleNamespace(
        entity_type=SharedContextEntityType.tech_stack,
        entity_value="python",
        source_proxy_user_id=other_proxy_user_id,
        source_memory_id=uuid.uuid4(),
    )

    class FakeScalars:
        def all(self):
            return [signal]

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        def execute(self, _stmt):
            return FakeResult()

    conflicts = ConflictDetector().detect_shared_context_conflict(
        session=FakeSession(),
        new_memory=make_new_memory("Our team stack now uses Go", category="expertise"),
        proxy_user_id=current_proxy_user_id,
        tenant_id=tenant_id,
    )

    assert len(conflicts) == 1
    assert conflicts[0].conflict_entity == "tech_stack"
    assert conflicts[0].entity_value_a == "python"
    assert conflicts[0].entity_value_b == "go"
