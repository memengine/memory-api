from __future__ import annotations

import uuid
from types import SimpleNamespace

from api.db.models import Conversation
from api.db.models import ConversationProcessingStatus
from api.db.models import ProxyUser
from api.db.models import User
from api.services.extractor import ExtractedMemory
from api.tasks import extraction_tasks


class FakeSession:
    def __init__(self, proxy_user: ProxyUser) -> None:
        self.proxy_user = proxy_user
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def get(self, model, identifier):
        if model is ProxyUser and identifier == self.proxy_user.id:
            return self.proxy_user
        return None

    def add(self, item) -> None:
        self.added.append(item)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeSessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self):
        return self.session


class FakeExtractor:
    def extract(self, messages, user_id):
        return [
            ExtractedMemory(
                content="User prefers Python",
                category="preference",
                importance_score=7.0,
                confidence=0.92,
                expiry="permanent",
                reasoning="Explicit preference",
            )
        ]


class FakeScorer:
    def score(self, memory, user_context):
        return float(memory.importance_score) + 1.0


class FakeConflictResolver:
    def __init__(self) -> None:
        self.calls = []

    def check_and_store(self, memories, **kwargs):
        self.calls.append({"memories": memories, **kwargs})
        return [
            SimpleNamespace(
                id=str(uuid.uuid4()),
                user_id=kwargs["user_id"],
                proxy_user_id=kwargs["proxy_user_id"],
                content=memories[0].content,
                category=memories[0].category,
                importance_score=memories[0].importance_score,
                confidence_score=memories[0].confidence,
                previous_version_id=None,
                resolution="NEW",
            )
        ]


def test_run_extraction_pipeline_persists_via_conflict_resolver(monkeypatch) -> None:
    proxy_user = ProxyUser(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        external_user_id="ext-123",
        external_user_id_hash="hash-123",
        memory_count=2,
        metadata_json={},
        is_blocked=False,
    )
    session = FakeSession(proxy_user)
    session_factory = FakeSessionFactory(session)
    resolver = FakeConflictResolver()
    backing_user = User(
        id=uuid.uuid4(),
        external_id=f"proxy::{proxy_user.id}",
        email="proxy@example.test",
        settings={},
        memory_count=0,
        is_active=True,
    )
    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=backing_user.id,
        message_count=2,
        processing_status=ConversationProcessingStatus.processing,
    )

    invalidated = {}
    refreshed = {}

    monkeypatch.setattr(
        extraction_tasks,
        "_ensure_proxy_backing_user",
        lambda _session, _proxy_user_id: backing_user,
    )
    monkeypatch.setattr(
        extraction_tasks,
        "_create_source_conversation",
        lambda _session, **kwargs: conversation,
    )
    monkeypatch.setattr(
        extraction_tasks,
        "_refresh_proxy_user_memory_count",
        lambda _session, proxy_user_id: refreshed.setdefault("proxy_user_id", str(proxy_user_id)),
    )
    monkeypatch.setattr(
        extraction_tasks,
        "_invalidate_proxy_user_cache",
        lambda proxy_user_id: invalidated.setdefault("proxy_user_id", proxy_user_id),
    )

    result = extraction_tasks.run_extraction_pipeline(
        {
            "job_id": "job-123",
            "tenant_id": str(proxy_user.tenant_id),
            "proxy_user_id": str(proxy_user.id),
            "external_user_id": "ext-123",
            "agent_id": None,
            "messages": [
                {"role": "user", "content": "I prefer Python"},
                {"role": "assistant", "content": "Noted"},
            ],
            "metadata": {"session_id": "sess-1"},
        },
        session_factory=session_factory,
        extractor=FakeExtractor(),
        scorer=FakeScorer(),
        qdrant_service=SimpleNamespace(),
        conflict_resolver=resolver,
        client=SimpleNamespace(),
    )

    assert result["status"] == "processed"
    assert result["memories_created"] == 1
    assert result["stored_memories"][0]["proxy_user_id"] == str(proxy_user.id)
    assert resolver.calls[0]["tenant_id"] == str(proxy_user.tenant_id)
    assert resolver.calls[0]["proxy_user_id"] == str(proxy_user.id)
    assert resolver.calls[0]["source_conversation_id"] == str(conversation.id)
    assert resolver.calls[0]["auto_commit"] is False
    assert resolver.calls[0]["memories"][0].importance_score == 8.0
    assert invalidated["proxy_user_id"] == str(proxy_user.id)
    assert refreshed["proxy_user_id"] == str(proxy_user.id)
    assert conversation.processing_status == ConversationProcessingStatus.done
    assert session.commits == 2
    assert session.rollbacks == 0
    assert session.closed is True
