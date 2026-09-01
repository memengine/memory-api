from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from api.db.models import AuditAction
from api.db.models import AuditLog
from api.db.models import ClarificationQueue
from api.db.models import CrossUserConflict
from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import VectorSyncOperation
from api.db.models import VectorSyncOutbox
from api.settings import get_settings
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID
from api.services.conflict_resolver import ConflictResolver
from api.services.extractor import ExtractedMemory


class FakeSession:
    def __init__(self, existing_memory: Memory | None = None) -> None:
        self.memories: dict[str, Memory] = {}
        if existing_memory is not None:
            self.memories[str(existing_memory.id)] = existing_memory
        self.added: list[object] = []
        self.commits = 0
        self.flushes = 0

    def get(self, _model, _memory_id):
        return self.memories.get(str(_memory_id))

    def add(self, item) -> None:
        self.added.append(item)
        if isinstance(item, Memory):
            self.memories[str(item.id)] = item

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1


def make_existing_memory() -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        content="User builds backend APIs using Python",
        category=MemoryCategory.expertise,
        importance_score=7.0,
        confidence_score=0.9,
        embedding_id="existing-memory-id",
        embedding_model_id=DEFAULT_ACTIVE_MODEL_ID,
        source_conversation_id=uuid.uuid4(),
        previous_version_id=None,
        expires_at=None,
        metadata_json={},
        is_archived=False,
    )


def make_new_memory(content: str = "User switched backend work from Python to Go") -> ExtractedMemory:
    return ExtractedMemory(
        content=content,
        category="expertise",
        importance_score=8.0,
        confidence=0.92,
        expiry="permanent",
        reasoning="New conflict candidate",
    )


def make_memory(
    *,
    content: str,
    category: MemoryCategory,
    importance_score: float = 7.0,
    confidence_score: float = 0.9,
) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        content=content,
        category=category,
        importance_score=importance_score,
        confidence_score=confidence_score,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id=DEFAULT_ACTIVE_MODEL_ID,
        source_conversation_id=uuid.uuid4(),
        previous_version_id=None,
        expires_at=None,
        metadata_json={},
        is_archived=False,
    )


def make_qdrant_point(existing_memory: Memory, score: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(
        id=str(existing_memory.id),
        score=score,
        payload={"memory_id": str(existing_memory.id)},
    )


def make_llm_client(action: str, merged_memory: dict | None = None) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text=json.dumps(
            {
                "action": action,
                "reasoning": f"{action} resolution",
                "merged_memory": merged_memory,
            }
        )
    )
    return client


def test_conflict_resolver_uses_extraction_model_from_settings(monkeypatch) -> None:
    monkeypatch.setenv("EXTRACTION_MODEL", "gemini-2.0-flash")
    get_settings.cache_clear()
    try:
        resolver = ConflictResolver(
            session=FakeSession(),
            qdrant_service=MagicMock(),
            embedder=lambda _text: [0.1] * 3,
            client=MagicMock(),
            default_source_conversation_id=uuid.uuid4(),
        )
        assert resolver.model == "gemini-2.0-flash"
    finally:
        get_settings.cache_clear()


def test_update_resolution_archives_old_and_links_new_version() -> None:
    existing = make_existing_memory()
    session = FakeSession(existing_memory=existing)
    qdrant = MagicMock()
    qdrant.search_memories.return_value = [make_qdrant_point(existing)]
    resolver = ConflictResolver(
        session=session,
        qdrant_service=qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=make_llm_client("UPDATE"),
        default_source_conversation_id=uuid.uuid4(),
    )

    stored = resolver.check_and_store([make_new_memory()], user_id=str(uuid.uuid4()))

    assert len(stored) == 1
    assert stored[0].resolution == "UPDATE"
    assert stored[0].previous_version_id == str(existing.id)
    assert existing.is_archived is True
    outbox_rows = [item for item in session.added if isinstance(item, VectorSyncOutbox)]
    assert [row.operation for row in outbox_rows] == [VectorSyncOperation.archive, VectorSyncOperation.upsert]
    assert any(isinstance(item, AuditLog) and item.action == AuditAction.updated for item in session.added)


def test_merge_resolution_stores_merged_memory_and_archives_old() -> None:
    existing = make_existing_memory()
    session = FakeSession(existing_memory=existing)
    qdrant = MagicMock()
    qdrant.search_memories.return_value = [make_qdrant_point(existing)]
    merged_memory = {
        "content": "User has backend expertise in both Python and Go across different systems",
        "category": "expertise",
        "importance_score": 9,
        "confidence": 0.95,
        "expiry": "permanent",
        "reasoning": "Merged technical history",
    }
    resolver = ConflictResolver(
        session=session,
        qdrant_service=qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=make_llm_client("MERGE", merged_memory=merged_memory),
        default_source_conversation_id=uuid.uuid4(),
    )

    stored = resolver.check_and_store([make_new_memory()], user_id=str(uuid.uuid4()))

    assert len(stored) == 1
    assert stored[0].resolution == "MERGE"
    assert "Python and Go" in stored[0].content
    assert existing.is_archived is True
    assert any(isinstance(item, AuditLog) and item.action == AuditAction.updated for item in session.added)


def test_keep_both_resolution_stores_new_memory_independently() -> None:
    existing = make_existing_memory()
    session = FakeSession(existing_memory=existing)
    qdrant = MagicMock()
    qdrant.search_memories.return_value = [make_qdrant_point(existing)]
    resolver = ConflictResolver(
        session=session,
        qdrant_service=qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=make_llm_client("KEEP_BOTH"),
        default_source_conversation_id=uuid.uuid4(),
    )

    stored = resolver.check_and_store(
        [make_new_memory(content="User used Python heavily in 2024 but switched to Go in 2026")],
        user_id=str(uuid.uuid4()),
    )

    assert len(stored) == 1
    assert stored[0].resolution == "KEEP_BOTH"
    assert existing.is_archived is False
    outbox_rows = [item for item in session.added if isinstance(item, VectorSyncOutbox)]
    assert len(outbox_rows) == 1
    assert outbox_rows[0].operation == VectorSyncOperation.upsert
    assert any(isinstance(item, AuditLog) and item.action == AuditAction.memory_created for item in session.added)


def test_reject_resolution_discards_new_memory_and_logs_reason() -> None:
    existing = make_existing_memory()
    session = FakeSession(existing_memory=existing)
    qdrant = MagicMock()
    qdrant.search_memories.return_value = [make_qdrant_point(existing)]
    resolver = ConflictResolver(
        session=session,
        qdrant_service=qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=make_llm_client("REJECT"),
        default_source_conversation_id=uuid.uuid4(),
    )

    stored = resolver.check_and_store([make_new_memory()], user_id=str(uuid.uuid4()))

    assert stored == []
    outbox_rows = [item for item in session.added if isinstance(item, VectorSyncOutbox)]
    assert outbox_rows == []
    assert any(isinstance(item, AuditLog) and item.action == AuditAction.deleted for item in session.added)


def test_equal_authority_cross_writer_conflict_queues_human_resolution() -> None:
    existing = make_existing_memory()
    existing.content = "Customer's current subscription plan is Starter."
    existing.category = MemoryCategory.fact
    existing.metadata_json = {
        "provenance": {
            "writer_id": "11111111-1111-1111-1111-111111111111",
            "service": "support-service",
            "authority_rules": {"categories": {"fact": 50}},
            "observed_at": "2026-06-14T08:00:00+00:00",
        }
    }
    session = FakeSession(existing_memory=existing)
    qdrant = MagicMock()
    qdrant.search_memories.return_value = [make_qdrant_point(existing)]
    resolver = ConflictResolver(
        session=session,
        qdrant_service=qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=make_llm_client("UPDATE"),
        default_source_conversation_id=uuid.uuid4(),
        provenance_snapshot={
            "writer_id": "22222222-2222-2222-2222-222222222222",
            "service": "billing-service",
            "authority_rules": {"categories": {"fact": 50}},
            "observed_at": "2026-06-14T10:00:00+00:00",
        },
    )

    stored = resolver.check_and_store(
        [
            ExtractedMemory(
                content="Customer's current subscription plan is Growth.",
                category="fact",
                importance_score=8.0,
                confidence=1.0,
                expiry="permanent",
                reasoning="Subscription record",
            )
        ],
        user_id=str(existing.user_id),
        tenant_id=str(uuid.uuid4()),
        proxy_user_id=str(existing.proxy_user_id),
    )

    assert len(stored) == 1
    assert stored[0].resolution == "CLARIFICATION_PENDING"
    pending = session.memories[stored[0].id]
    assert existing.is_archived is False
    assert pending.is_archived is True
    conflicts = [item for item in session.added if isinstance(item, CrossUserConflict)]
    clarifications = [item for item in session.added if isinstance(item, ClarificationQueue)]
    assert len(conflicts) == 1
    assert conflicts[0].resolution_path == "tenant_review"
    assert conflicts[0].requires_attention is True
    assert clarifications == []
    outbox_rows = [item for item in session.added if isinstance(item, VectorSyncOutbox)]
    assert outbox_rows == []


def test_temporal_conflicts_keep_both_without_llm_classification() -> None:
    existing = make_existing_memory()
    existing.content = "User used Python heavily in 2024"
    session = FakeSession(existing_memory=existing)
    qdrant = MagicMock()
    qdrant.search_memories.return_value = [make_qdrant_point(existing)]
    client = make_llm_client("UPDATE")
    resolver = ConflictResolver(
        session=session,
        qdrant_service=qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=client,
        default_source_conversation_id=uuid.uuid4(),
    )

    stored = resolver.check_and_store(
        [make_new_memory(content="User switched backend work from Python to Go in 2026")],
        user_id=str(uuid.uuid4()),
    )

    assert len(stored) == 1
    assert stored[0].resolution == "KEEP_BOTH"
    client.models.generate_content.assert_not_called()


def test_conflict_prompt_contains_required_resolution_rules() -> None:
    prompt_path = Path("api/services/prompts/conflict_prompt.txt")
    prompt = prompt_path.read_text(encoding="utf-8")

    assert "importance_score range is 1.0 to 10.0" in prompt
    assert "set importance_score to the higher of the two input scores plus 0.5, capped at 10.0" in prompt
    assert "INPUT FORMAT:" in prompt
    assert '"existing"' in prompt
    assert '"new"' in prompt
    assert "confidence is below 0.5" in prompt
    assert "do not reject simply because the new memory is less specific" in prompt
    assert "specificity priority for overlapping subject matter is: expertise > fact > preference" in prompt


def test_update_flow_archives_old_memory_and_sets_previous_version() -> None:
    session = FakeSession()
    user_id = str(uuid.uuid4())
    qdrant = MagicMock()

    def search_memories(*, query_embedding, user_id, limit, include_archived, category_filter=None):
        active_memories = [memory for memory in session.memories.values() if not memory.is_archived]
        if not active_memories:
            return []
        return [make_qdrant_point(active_memories[0])]

    qdrant.search_memories.side_effect = search_memories
    resolver = ConflictResolver(
        session=session,
        qdrant_service=qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=make_llm_client("UPDATE"),
        default_source_conversation_id=uuid.uuid4(),
    )

    first_result = resolver.check_and_store(
        [
            ExtractedMemory(
                content="User prefers Python",
                category="preference",
                importance_score=7.0,
                confidence=0.95,
                expiry="permanent",
                reasoning="Initial language preference",
            )
        ],
        user_id=user_id,
    )
    second_result = resolver.check_and_store(
        [
            ExtractedMemory(
                content="User switched to Go",
                category="preference",
                importance_score=8.0,
                confidence=0.95,
                expiry="permanent",
                reasoning="Updated language preference",
            )
        ],
        user_id=user_id,
    )

    old_memory = session.memories[first_result[0].id]
    new_memory = session.memories[second_result[0].id]
    audit_logs = [item for item in session.added if isinstance(item, AuditLog)]

    assert old_memory.is_archived is True
    assert new_memory.previous_version_id == old_memory.id
    assert second_result[0].previous_version_id == str(old_memory.id)
    assert any(log.action == AuditAction.updated for log in audit_logs)


def test_keep_both_flow_stores_unrelated_memories_independently() -> None:
    session = FakeSession()
    user_id = str(uuid.uuid4())
    qdrant = MagicMock()
    qdrant.search_memories.return_value = []
    resolver = ConflictResolver(
        session=session,
        qdrant_service=qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=make_llm_client("KEEP_BOTH"),
        default_source_conversation_id=uuid.uuid4(),
    )

    first_result = resolver.check_and_store(
        [
            ExtractedMemory(
                content="User works in healthcare",
                category="fact",
                importance_score=6.0,
                confidence=0.95,
                expiry="permanent",
                reasoning="Industry context",
            )
        ],
        user_id=user_id,
    )
    second_result = resolver.check_and_store(
        [
            ExtractedMemory(
                content="User is an engineer",
                category="fact",
                importance_score=7.0,
                confidence=0.95,
                expiry="permanent",
                reasoning="Professional role",
            )
        ],
        user_id=user_id,
    )

    stored_memories = [memory for memory in session.memories.values() if not memory.is_archived]

    assert len(first_result) == 1
    assert len(second_result) == 1
    assert len(stored_memories) == 2
    assert {memory.content for memory in stored_memories} == {
        "User works in healthcare",
        "User is an engineer",
    }
    assert all(memory.previous_version_id is None for memory in stored_memories)


def test_reject_duplicate_memory_keeps_single_memory_and_logs_audit_entry() -> None:
    session = FakeSession()
    user_id = str(uuid.uuid4())
    qdrant = MagicMock()

    def search_memories(*, query_embedding, user_id, limit, include_archived, category_filter=None):
        active_memories = [memory for memory in session.memories.values() if not memory.is_archived]
        if not active_memories:
            return []
        return [make_qdrant_point(active_memories[0])]

    qdrant.search_memories.side_effect = search_memories
    resolver = ConflictResolver(
        session=session,
        qdrant_service=qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=make_llm_client("REJECT"),
        default_source_conversation_id=uuid.uuid4(),
    )

    resolver.check_and_store(
        [
            ExtractedMemory(
                content="User prefers concise answers",
                category="preference",
                importance_score=8.0,
                confidence=0.95,
                expiry="permanent",
                reasoning="Initial preference",
            )
        ],
        user_id=user_id,
    )
    rejected_result = resolver.check_and_store(
        [
            ExtractedMemory(
                content="User prefers concise answers",
                category="preference",
                importance_score=8.0,
                confidence=0.95,
                expiry="permanent",
                reasoning="Duplicate preference",
            )
        ],
        user_id=user_id,
    )

    active_memories = [memory for memory in session.memories.values() if not memory.is_archived]
    audit_logs = [item for item in session.added if isinstance(item, AuditLog)]

    assert rejected_result == []
    assert len(active_memories) == 1
    assert any(log.action == AuditAction.deleted for log in audit_logs)


def test_merge_flow_stores_single_merged_memory_archives_original_and_boosts_importance() -> None:
    session = FakeSession()
    user_id = str(uuid.uuid4())
    qdrant = MagicMock()

    def search_memories(*, query_embedding, user_id, limit, include_archived, category_filter=None):
        active_memories = [memory for memory in session.memories.values() if not memory.is_archived]
        if not active_memories:
            return []
        return [make_qdrant_point(active_memories[0])]

    qdrant.search_memories.side_effect = search_memories
    resolver = ConflictResolver(
        session=session,
        qdrant_service=qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=make_llm_client(
            "MERGE",
            merged_memory={
                "content": "User prefers Python for backend APIs built with FastAPI",
                "category": "expertise",
                "importance_score": 1.0,
                "confidence": 0.97,
                "expiry": "permanent",
                "reasoning": "Merged broader backend preference with the more specific FastAPI usage detail.",
            },
        ),
        default_source_conversation_id=uuid.uuid4(),
    )

    first_result = resolver.check_and_store(
        [
            ExtractedMemory(
                content="User prefers Python for backend work",
                category="expertise",
                importance_score=9.8,
                confidence=0.95,
                expiry="permanent",
                reasoning="Broader backend language preference",
            )
        ],
        user_id=user_id,
    )
    merged_result = resolver.check_and_store(
        [
            ExtractedMemory(
                content="User prefers Python for backend APIs built with FastAPI",
                category="expertise",
                importance_score=9.4,
                confidence=0.96,
                expiry="permanent",
                reasoning="More specific version of the same underlying fact",
            )
        ],
        user_id=user_id,
    )

    original_memory = session.memories[first_result[0].id]
    stored_merged_memory = session.memories[merged_result[0].id]
    active_memories = [memory for memory in session.memories.values() if not memory.is_archived]
    audit_logs = [item for item in session.added if isinstance(item, AuditLog)]

    assert len(merged_result) == 1
    assert merged_result[0].resolution == "MERGE"
    assert original_memory.is_archived is True
    assert len(active_memories) == 1
    assert active_memories[0].id == stored_merged_memory.id
    assert stored_merged_memory.content == "User prefers Python for backend APIs built with FastAPI"
    assert stored_merged_memory.importance_score == 10.0
    assert merged_result[0].importance_score == 10.0
    assert stored_merged_memory.previous_version_id == original_memory.id
    assert any(log.action == AuditAction.updated for log in audit_logs)


def test_audit_log_exists_for_each_resolution_path() -> None:
    update_session = FakeSession(make_memory(content="User prefers Python", category=MemoryCategory.preference))
    keep_both_session = FakeSession()
    reject_session = FakeSession(make_memory(content="User prefers concise answers", category=MemoryCategory.preference))

    update_qdrant = MagicMock()
    update_qdrant.search_memories.return_value = [make_qdrant_point(next(iter(update_session.memories.values())))]
    keep_both_qdrant = MagicMock()
    keep_both_qdrant.search_memories.return_value = []
    reject_qdrant = MagicMock()
    reject_qdrant.search_memories.return_value = [make_qdrant_point(next(iter(reject_session.memories.values())))]

    ConflictResolver(
        session=update_session,
        qdrant_service=update_qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=make_llm_client("UPDATE"),
        default_source_conversation_id=uuid.uuid4(),
    ).check_and_store([make_new_memory(content="User switched to Go")], user_id=str(uuid.uuid4()))

    ConflictResolver(
        session=keep_both_session,
        qdrant_service=keep_both_qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=make_llm_client("KEEP_BOTH"),
        default_source_conversation_id=uuid.uuid4(),
    ).check_and_store([make_new_memory(content="User works in healthcare")], user_id=str(uuid.uuid4()))

    ConflictResolver(
        session=reject_session,
        qdrant_service=reject_qdrant,
        embedder=lambda _text: [0.1] * 3,
        client=make_llm_client("REJECT"),
        default_source_conversation_id=uuid.uuid4(),
    ).check_and_store([make_new_memory(content="User prefers concise answers")], user_id=str(uuid.uuid4()))

    assert any(isinstance(item, AuditLog) and item.action == AuditAction.updated for item in update_session.added)
    assert any(isinstance(item, AuditLog) and item.action == AuditAction.memory_created for item in keep_both_session.added)
    assert any(isinstance(item, AuditLog) and item.action == AuditAction.deleted for item in reject_session.added)
