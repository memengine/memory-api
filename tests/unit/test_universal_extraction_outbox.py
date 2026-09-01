from __future__ import annotations

from types import SimpleNamespace
import uuid

from api.db.models import PermissionGrant
from api.db.models import UniversalUser
from api.db.models import VectorSyncOutbox
from api.services.embedding_service import EmbeddingResult
from api.services.extractor import ExtractedMemory
from api.tasks import universal_extraction_tasks


class FakeResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, user: UniversalUser, grant: PermissionGrant) -> None:
        self.user = user
        self.grant = grant
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.grant_checks = 0

    def get(self, model, identifier):
        if model is UniversalUser and identifier == self.user.id:
            return self.user
        return None

    def execute(self, statement):
        if "permission_grants" in str(statement):
            self.grant_checks += 1
        return FakeResult(self.grant)

    def add(self, item) -> None:
        self.added.append(item)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeExtractor:
    def extract(self, *, messages, user_id):
        del messages, user_id
        return [
            ExtractedMemory(
                content="User prefers concise answers",
                category="preference",
                importance_score=7.0,
                confidence=0.95,
                expiry="permanent",
                reasoning="Explicit preference",
            )
        ]


class FakeScorer:
    def score(self, memory, context):
        del context
        return memory.importance_score


class FakeEmbedder:
    def embed_sync(self, content: str) -> EmbeddingResult:
        return EmbeddingResult(
            vector=[0.1, 0.2, 0.3],
            model_id="test-embedding",
            dimensions=3,
            qdrant_collection="universal_memories",
        )


def test_universal_extraction_enqueues_vector_outbox(monkeypatch) -> None:
    user = UniversalUser(
        id=uuid.uuid4(),
        email="user@example.test",
        uui_token="uui_test",
        memory_count=0,
        is_active=True,
    )
    agent_id = uuid.uuid4()
    grant = PermissionGrant(
        id=uuid.uuid4(),
        user_uui_id=user.id,
        agent_id=agent_id,
        categories_allowed=["preference"],
        access_type="read_write",
        is_active=True,
    )
    session = FakeSession(user, grant)

    monkeypatch.setattr(
        universal_extraction_tasks,
        "EmbeddingService",
        lambda sync_session: FakeEmbedder(),
    )
    monkeypatch.setattr(
        universal_extraction_tasks.VersionService,
        "record_universal_version_sync",
        lambda *args, **kwargs: SimpleNamespace(),
    )

    result = universal_extraction_tasks.run_universal_extraction_pipeline(
        {
            "job_id": str(uuid.uuid4()),
            "user_uui_id": str(user.id),
            "agent_id": str(agent_id),
            "messages": [{"role": "user", "content": "Keep answers concise"}],
            "metadata": {"source": "test"},
        },
        session_factory=lambda: session,
        extractor=FakeExtractor(),
        scorer=FakeScorer(),
    )

    outbox_rows = [item for item in session.added if isinstance(item, VectorSyncOutbox)]
    assert result["status"] == "processed"
    assert result["memories_created"] == 1
    assert len(outbox_rows) == 1
    assert outbox_rows[0].payload["qdrant_collection"] == "universal_memories"
    assert outbox_rows[0].payload["source_agent_id"] == str(agent_id)
    assert session.commits == 1
    assert session.rollbacks == 0
    assert session.closed is True
    assert session.grant_checks == 2

def test_midflight_revocation_rolls_back_all_staged_writes(monkeypatch) -> None:
    user = UniversalUser(
        id=uuid.uuid4(), uui_token="uui_revoked_midflight",
        memory_count=0, is_active=True,
    )
    agent_id = uuid.uuid4()
    grant = PermissionGrant(
        id=uuid.uuid4(), user_uui_id=user.id, agent_id=agent_id,
        categories_allowed=["preference"], access_type="read_write", is_active=True,
    )
    session = FakeSession(user, grant)

    def grant_then_revoked(statement):
        if "permission_grants" in str(statement):
            session.grant_checks += 1
            return FakeResult(grant if session.grant_checks == 1 else None)
        return FakeResult(None)

    session.execute = grant_then_revoked
    monkeypatch.setattr(
        universal_extraction_tasks, "EmbeddingService",
        lambda sync_session: FakeEmbedder(),
    )
    monkeypatch.setattr(
        universal_extraction_tasks.VersionService, "record_universal_version_sync",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    result = universal_extraction_tasks.run_universal_extraction_pipeline(
        {
            "job_id": str(uuid.uuid4()), "user_uui_id": str(user.id),
            "agent_id": str(agent_id),
            "messages": [{"role": "user", "content": "Keep answers concise"}],
        },
        session_factory=lambda: session, extractor=FakeExtractor(), scorer=FakeScorer(),
    )
    assert result["status"] == "blocked"
    assert result["blocked_reason"] == "write_not_permitted"
    assert result["memories_created"] == 0
    assert session.rollbacks == 1
    assert session.commits == 0
    assert session.grant_checks == 2
