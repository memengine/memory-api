from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from api.db.models import CrossUserConflict
from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import SharedContextEntityType
from api.db.models import SharedContextSignal
from api.services.conflict_resolver import resolve_cross_user_conflict_automatically
from api.services.conflict_resolver import ConflictResolver
from api.services.conflict_detection import ConflictType
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID
from api.services.extractor import ExtractedMemory


class FakeScalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return FakeScalars(self.rows)


class FakeSession:
    def __init__(self, shared_signals=None):
        self.shared_signals = shared_signals or []
        self.added = []
        self.memories = {}
        self.flushes = 0
        self.commits = 0

    def execute(self, _stmt):
        return FakeResult(self.shared_signals)

    def add(self, item):
        self.added.append(item)
        if isinstance(item, Memory):
            self.memories[str(item.id)] = item

    def flush(self):
        self.flushes += 1

    def commit(self):
        self.commits += 1


def test_resolver_flags_cross_user_shared_context_without_blocking_storage(monkeypatch) -> None:
    tenant_id = str(uuid.uuid4())
    current_proxy_user_id = str(uuid.uuid4())
    other_proxy_user_id = uuid.uuid4()
    source_memory_id = uuid.uuid4()
    existing_signal = SimpleNamespace(
        entity_type=SharedContextEntityType.tech_stack,
        entity_value="python",
        source_proxy_user_id=other_proxy_user_id,
        source_memory_id=source_memory_id,
    )
    session = FakeSession(shared_signals=[existing_signal])
    qdrant = MagicMock()
    qdrant.search_memories.return_value = []

    from api.tasks import quota_tasks

    monkeypatch.setattr(quota_tasks.send_webhook_event, "delay", lambda *args, **kwargs: None)

    resolver = ConflictResolver(
        session=session,
        qdrant_service=qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=MagicMock(),
        default_source_conversation_id=uuid.uuid4(),
    )

    stored = resolver.check_and_store(
        [
            ExtractedMemory(
                content="Our team stack now uses Go",
                category="expertise",
                importance_score=8.0,
                confidence=0.95,
                expiry="permanent",
                reasoning="Updated shared team stack signal",
            )
        ],
        user_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        proxy_user_id=current_proxy_user_id,
    )

    assert len(stored) == 1
    assert resolver.last_cross_user_conflicts_flagged == 1
    assert any(isinstance(item, SharedContextSignal) for item in session.added)
    conflict_rows = [item for item in session.added if isinstance(item, CrossUserConflict)]
    assert len(conflict_rows) == 1
    assert conflict_rows[0].entity_value_a == "python"
    assert conflict_rows[0].entity_value_b == "go"
    assert conflict_rows[0].user_a_memory_id == source_memory_id
    assert conflict_rows[0].user_b_memory_id == uuid.UUID(stored[0].id)


def test_personal_goal_cross_user_conflict_is_ignored() -> None:
    tenant_id = uuid.uuid4()
    memory_a = Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        content="User wants to learn quantum computing.",
        category=MemoryCategory.goal,
        importance_score=7.0,
        confidence_score=0.9,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id=DEFAULT_ACTIVE_MODEL_ID,
        source_conversation_id=uuid.uuid4(),
        metadata_json={},
    )
    memory_b = Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        content="User wants to become an actor in the South Indian film industry.",
        category=MemoryCategory.goal,
        importance_score=7.0,
        confidence_score=0.9,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id=DEFAULT_ACTIVE_MODEL_ID,
        source_conversation_id=uuid.uuid4(),
        metadata_json={},
    )
    conflict = CrossUserConflict(
        tenant_id=tenant_id,
        user_a_memory_id=memory_a.id,
        user_b_memory_id=memory_b.id,
        entity_type=SharedContextEntityType.shared_goal,
        entity_value_a="quantum computing",
        entity_value_b="actor",
    )
    conflict.user_a_memory = memory_a
    conflict.user_b_memory = memory_b

    result = resolve_cross_user_conflict_automatically(conflict, FakeSession())

    assert result.strategy_used == "per_user_scoped"
    assert conflict.status == "ignored"
    assert conflict.requires_attention is False


def test_resolver_uses_type_specific_prompts_for_fact_and_preference() -> None:
    resolver = ConflictResolver(
        session=FakeSession(),
        qdrant_service=MagicMock(),
        embedder=lambda _text: [0.1] * 3,
        client=MagicMock(),
        default_source_conversation_id=uuid.uuid4(),
    )
    existing_fact = Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        content="User's certification exam is in March",
        category=MemoryCategory.fact,
        importance_score=7.0,
        confidence_score=0.9,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id=DEFAULT_ACTIVE_MODEL_ID,
        source_conversation_id=uuid.uuid4(),
        metadata_json={},
    )
    existing_preference = Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        content="User prefers concise explanations",
        category=MemoryCategory.preference,
        importance_score=7.0,
        confidence_score=0.9,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id=DEFAULT_ACTIVE_MODEL_ID,
        source_conversation_id=uuid.uuid4(),
        metadata_json={},
    )

    fact_prompt = resolver._build_conflict_user_prompt(
        new_memory=ExtractedMemory(
            content="User's certification exam got postponed to April",
            category="fact",
            importance_score=8.0,
            confidence=0.95,
            expiry="permanent",
            reasoning="Updated date",
        ),
        existing_memory=existing_fact,
        conflict_type=ConflictType.FACT_UPDATE,
    )
    preference_prompt = resolver._build_conflict_user_prompt(
        new_memory=ExtractedMemory(
            content="User now prefers detailed explanations",
            category="preference",
            importance_score=8.0,
            confidence=0.95,
            expiry="permanent",
            reasoning="Updated preference",
        ),
        existing_memory=existing_preference,
        conflict_type=ConflictType.PREFERENCE_CHANGE,
    )

    assert "same fact at different times" in fact_prompt
    assert "preference changed" in preference_prompt
    assert fact_prompt != preference_prompt
