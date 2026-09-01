from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from api.db.models import Conversation, ConversationProcessingStatus, Memory, ProxyUser, User
from api.services.extractor import ExtractedMemory
from api.tasks import extraction_tasks


class FailureSession:
    def __init__(self, proxy: ProxyUser) -> None:
        self.proxy = proxy
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    def get(self, model, identifier):
        return self.proxy if model is ProxyUser and identifier == self.proxy.id else None

    def add(self, value: object) -> None:
        self.added.append(value)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


class OneMemoryExtractor:
    def extract(self, messages, user_id):
        return [ExtractedMemory(
            content="User prefers concise answers", category="preference",
            importance_score=5.0, confidence=0.95, expiry="permanent", reasoning="explicit",
        )]


class FailingResolver:
    def check_and_store(self, *_args, **_kwargs):
        raise RuntimeError("injected persistence failure")


def test_pipeline_failure_rolls_back_memory_transaction_and_marks_conversation_failed(monkeypatch) -> None:
    proxy = ProxyUser(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), external_user_id="failure-user",
        external_user_id_hash="failure-hash", memory_count=0, metadata_json={}, is_blocked=False,
    )
    user = User(
        id=uuid.uuid4(), external_id=f"proxy::{proxy.id}", email="failure@example.test",
        settings={}, memory_count=0, is_active=True,
    )
    conversation = Conversation(
        id=uuid.uuid4(), user_id=user.id, message_count=1,
        processing_status=ConversationProcessingStatus.processing,
    )
    session = FailureSession(proxy)
    monkeypatch.setattr(extraction_tasks, "_ensure_proxy_backing_user", lambda *_args: user)
    monkeypatch.setattr(extraction_tasks, "_create_source_conversation", lambda *_args, **_kwargs: conversation)
    monkeypatch.setattr(extraction_tasks, "_persist_pending_extraction_candidates", lambda *_args, **_kwargs: (0, []))

    with pytest.raises(RuntimeError, match="injected persistence failure"):
        extraction_tasks.run_extraction_pipeline(
            {
                "job_id": str(uuid.uuid4()), "tenant_id": str(proxy.tenant_id),
                "proxy_user_id": str(proxy.id), "messages": [{"role": "user", "content": "Be concise"}],
                "metadata": {},
            },
            session_factory=lambda: session, extractor=OneMemoryExtractor(),
            scorer=SimpleNamespace(score=lambda memory, _context: memory.importance_score),
            qdrant_service=SimpleNamespace(), conflict_resolver=FailingResolver(), client=SimpleNamespace(),
        )

    assert session.rollbacks == 1
    assert session.commits == 2
    assert conversation.processing_status == ConversationProcessingStatus.failed
    assert not any(isinstance(row, Memory) for row in session.added)
