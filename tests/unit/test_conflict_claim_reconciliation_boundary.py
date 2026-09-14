from __future__ import annotations

import uuid
from types import SimpleNamespace

from api.services.conflict_resolver import ConflictResolver
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID, EmbeddingResult
from api.services.extractor import ExtractedMemory


class Session:
    def add(self, _item) -> None:
        return None

    def flush(self) -> None:
        return None


class CapturingSession(Session):
    def __init__(self) -> None:
        self.items = []

    def add(self, item) -> None:
        self.items.append(item)


def test_resolver_threads_predecessor_and_decision_evidence(monkeypatch) -> None:
    captured = {}

    def record_memory(_service, _memory, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "api.services.conflict_resolver.ClaimLedgerService.record_memory",
        record_memory,
    )
    resolver = ConflictResolver(
        session=Session(),
        qdrant_service=SimpleNamespace(search_memories=lambda **_kwargs: []),
        embedder=lambda _content: [0.1, 0.2, 0.3],
        default_source_conversation_id=uuid.uuid4(),
    )
    predecessor = uuid.uuid4()
    evidence = {"action": "UPDATE", "reason_codes": ["incoming_source_wins"]}

    resolver._store_new_memory(
        extracted_memory=ExtractedMemory(
            content="User now lives in Bengaluru.", category="fact",
            importance_score=5.0, confidence=0.95, expiry="permanent",
            reasoning="Correction",
        ),
        user_id=str(uuid.uuid4()), proxy_user_id=str(uuid.uuid4()),
        tenant_id=str(uuid.uuid4()),
        embedding=EmbeddingResult(
            vector=[0.1, 0.2, 0.3], model_id=DEFAULT_ACTIVE_MODEL_ID,
            dimensions=3, qdrant_collection="memories",
        ),
        previous_version_id=str(predecessor), resolution="UPDATE",
        source_conversation_id=str(uuid.uuid4()), agent_id=None,
        decision_evidence=evidence,
    )

    assert captured["predecessor_memory_id"] == str(predecessor)
    assert captured["decision_evidence"] == evidence


def test_resolver_persists_server_validated_extraction_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.services.conflict_resolver.ClaimLedgerService.record_memory",
        lambda *_args, **_kwargs: None,
    )
    session = CapturingSession()
    resolver = ConflictResolver(
        session=session,
        qdrant_service=SimpleNamespace(search_memories=lambda **_kwargs: []),
        embedder=lambda _content: [0.1, 0.2, 0.3],
        default_source_conversation_id=uuid.uuid4(),
        provenance_snapshot={"attestation": "legacy_conversation"},
    )
    source_conversation_id = uuid.uuid4()
    resolver._store_new_memory(
        extracted_memory=ExtractedMemory(
            content="User prefers concise code examples.", category="preference",
            importance_score=6.0, confidence=0.95, expiry="permanent",
            reasoning="Direct user statement",
            validated_evidence={
                "schema_version": 1,
                "turn_indexes": [0],
                "user_turn_indexes": [0],
                "relation": "direct_user_statement",
                "authority": {"level": 20, "label": "client_assertion"},
                "validation": {"accepted": True, "reason": "direct_user_statement"},
                "extraction": {"provider": "test", "model": "fake"},
            },
        ),
        user_id=str(uuid.uuid4()), proxy_user_id=str(uuid.uuid4()),
        tenant_id=str(uuid.uuid4()),
        embedding=EmbeddingResult(
            vector=[0.1, 0.2, 0.3], model_id=DEFAULT_ACTIVE_MODEL_ID,
            dimensions=3, qdrant_collection="memories",
        ),
        previous_version_id=None, resolution="NEW",
        source_conversation_id=str(source_conversation_id), agent_id=None,
    )

    memory = next(item for item in session.items if hasattr(item, "metadata_json"))
    evidence = memory.metadata_json["provenance"]["extraction_evidence"]
    assert evidence["memory_id"] == str(memory.id)
    assert evidence["source_conversation_id"] == str(source_conversation_id)
    assert evidence["turn_indexes"] == [0]
    assert evidence["validation"]["accepted"] is True
    assert evidence["authority"]["level"] == 20
