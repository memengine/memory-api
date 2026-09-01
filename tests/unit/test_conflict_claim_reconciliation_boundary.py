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
