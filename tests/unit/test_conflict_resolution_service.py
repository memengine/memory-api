from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from api.db.models import CrossUserConflict
from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import SharedContextEntityType
from api.db.models import VectorSyncOperation
from api.db.models import VectorSyncOutbox
from api.services.conflict_resolution_service import apply_conflict_selection
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID
from api.services.embedding_service import EmbeddingResult


class FakeAsyncSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, item: object) -> None:
        self.added.append(item)


def make_memory(*, content: str, archived: bool) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        content=content,
        category=MemoryCategory.fact,
        importance_score=7.0,
        confidence_score=1.0,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id=DEFAULT_ACTIVE_MODEL_ID,
        source_conversation_id=uuid.uuid4(),
        metadata_json={},
        is_archived=archived,
    )


def make_conflict(memory_a: Memory, memory_b: Memory) -> CrossUserConflict:
    conflict = CrossUserConflict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_a_memory_id=memory_a.id,
        user_b_memory_id=memory_b.id,
        entity_type=SharedContextEntityType.personal_fact,
        entity_value_a=memory_a.content,
        entity_value_b=memory_b.content,
    )
    conflict.user_a_memory = memory_a
    conflict.user_b_memory = memory_b
    return conflict


@pytest.mark.asyncio
async def test_selecting_pending_memory_updates_database_and_vector_outbox(
    monkeypatch,
) -> None:
    memory_a = make_memory(content="Plan is Starter.", archived=False)
    memory_b = make_memory(content="Plan is Growth.", archived=True)
    conflict = make_conflict(memory_a, memory_b)
    session = FakeAsyncSession()

    async def fake_record_version(*_args, **_kwargs) -> None:
        return None

    async def fake_embed(*_args, **_kwargs) -> EmbeddingResult:
        return EmbeddingResult(
            vector=[0.1, 0.2, 0.3],
            model_id=DEFAULT_ACTIVE_MODEL_ID,
            dimensions=3,
            qdrant_collection="memories",
        )

    monkeypatch.setattr(
        "api.services.conflict_resolution_service.VersionService.asafe_record_version",
        fake_record_version,
    )
    monkeypatch.setattr(
        "api.services.conflict_resolution_service.EmbeddingService",
        lambda **_kwargs: SimpleNamespace(embed=fake_embed),
    )

    action = await apply_conflict_selection(
        session,  # type: ignore[arg-type]
        conflict=conflict,
        selection="B",
        changed_by="operator",
        reason="Billing service is authoritative for this record.",
    )

    assert memory_a.is_archived is True
    assert memory_b.is_archived is False
    outbox = [item for item in session.added if isinstance(item, VectorSyncOutbox)]
    assert [item.operation for item in outbox] == [
        VectorSyncOperation.delete,
        VectorSyncOperation.upsert,
    ]
    assert conflict.decision_evidence["decision_level"] == "manual"
    assert conflict.decision_evidence["action"] == "UPDATE"
    assert "selection:B" in conflict.decision_evidence["reason_codes"]
    assert action == "archived_memory_a_and_activated_memory_b"


@pytest.mark.asyncio
async def test_selecting_neither_archives_both_claims(monkeypatch) -> None:
    memory_a = make_memory(content="Plan is Starter.", archived=False)
    memory_b = make_memory(content="Plan is Growth.", archived=True)
    conflict = make_conflict(memory_a, memory_b)
    session = FakeAsyncSession()

    async def fake_record_version(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        "api.services.conflict_resolution_service.VersionService.asafe_record_version",
        fake_record_version,
    )

    action = await apply_conflict_selection(
        session,  # type: ignore[arg-type]
        conflict=conflict,
        selection="neither",
        changed_by="user",
        reason="Neither claim is current.",
    )

    assert memory_a.is_archived is True
    assert memory_b.is_archived is True
    outbox = [item for item in session.added if isinstance(item, VectorSyncOutbox)]
    assert [item.operation for item in outbox] == [VectorSyncOperation.delete]
    assert action == "archived_memory_a"
