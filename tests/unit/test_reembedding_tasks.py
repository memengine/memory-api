from __future__ import annotations

from types import SimpleNamespace

from api.tasks import reembedding_tasks


class FakeRedis:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.values = dict(initial or {})

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: str):
        self.values[key] = value
        return True

    def delete(self, key: str):
        self.values.pop(key, None)
        return 1


class FakeSession:
    def __init__(self, memory: object | None = None) -> None:
        self.memory = memory
        self.commit_count = 0

    def get(self, model, memory_id):
        _ = model
        _ = memory_id
        return self.memory

    def add(self, obj):
        self.memory = obj

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        return None

    def close(self):
        return None


def test_reembedding_clears_cursor_after_success(monkeypatch) -> None:
    cursor_key = "reembed:tenant-1:old-model:cursor"
    redis_client = FakeRedis({cursor_key: "existing-cursor"})
    memory = SimpleNamespace(
        id="mem-1",
        content="User prefers Python.",
        proxy_user_id="proxy-1",
        user_id="user-1",
        embedding_model_id="old-model",
        agent_id=None,
        category=SimpleNamespace(value="preference"),
        importance_score=8.0,
        is_archived=False,
        created_at=None,
    )
    session = FakeSession(memory=memory)
    update_calls: list[dict[str, object]] = []
    batches = [
        [
            {
                "id": "mem-1",
                "content": "User prefers Python.",
                "user_id": "user-1",
                "proxy_user_id": "proxy-1",
                "tenant_id": "tenant-1",
            }
        ],
        [],
    ]

    monkeypatch.setattr(reembedding_tasks, "_latest_incomplete_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reembedding_tasks, "_remaining_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        reembedding_tasks,
        "_select_batch",
        lambda *_args, **_kwargs: batches.pop(0),
    )
    monkeypatch.setattr(reembedding_tasks, "_create_job", lambda *_args, **_kwargs: "job-1")
    monkeypatch.setattr(
        reembedding_tasks,
        "_update_job",
        lambda _session, **kwargs: update_calls.append(kwargs),
    )

    embedding_service = SimpleNamespace(
        get_model_sync=lambda model_id: SimpleNamespace(id=model_id, qdrant_collection=f"{model_id}-collection"),
        embed_sync=lambda *_args, **_kwargs: SimpleNamespace(
            vector=[0.1, 0.2],
            model_id="new-model",
            dimensions=2,
            qdrant_collection="new-collection",
        ),
    )
    qdrant_service = SimpleNamespace(
        upsert_memory=lambda *args, **kwargs: None,
        delete_memory=lambda *args, **kwargs: None,
    )

    result = reembedding_tasks.run_reembedding_cycle(
        "tenant-1",
        "old-model",
        "new-model",
        batch_size=1,
        session_factory=lambda: session,
        redis_client=redis_client,
        qdrant_service=qdrant_service,
        embedding_service=embedding_service,
    )

    assert result["status"] == "complete"
    assert result["processed_rows"] == 1
    assert cursor_key not in redis_client.values
    assert memory.embedding_model_id == "new-model"
    assert update_calls[-1]["status"] == "complete"


def test_reembedding_resumes_only_from_incomplete_job(monkeypatch) -> None:
    cursor_key = "reembed:tenant-1:old-model:cursor"
    redis_client = FakeRedis({cursor_key: "mem-5"})
    session = FakeSession()
    update_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        reembedding_tasks,
        "_latest_incomplete_job",
        lambda *_args, **_kwargs: {
            "id": "job-existing",
            "status": "running",
            "processed_rows": 2,
            "total_rows": 5,
        },
    )
    monkeypatch.setattr(reembedding_tasks, "_remaining_count", lambda *_args, **_kwargs: 3)
    monkeypatch.setattr(reembedding_tasks, "_select_batch", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        reembedding_tasks,
        "_create_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should reuse incomplete job")),
    )
    monkeypatch.setattr(
        reembedding_tasks,
        "_update_job",
        lambda _session, **kwargs: update_calls.append(kwargs),
    )

    result = reembedding_tasks.run_reembedding_cycle(
        "tenant-1",
        "old-model",
        "new-model",
        batch_size=1,
        session_factory=lambda: session,
        redis_client=redis_client,
        qdrant_service=SimpleNamespace(),
        embedding_service=SimpleNamespace(
            get_model_sync=lambda model_id: SimpleNamespace(id=model_id, qdrant_collection=f"{model_id}-collection"),
        ),
    )

    assert result["status"] == "complete"
    assert result["processed_rows"] == 2
    assert result["total_rows"] == 5
    assert update_calls[0]["job_id"] == "job-existing"
    assert update_calls[-1]["status"] == "complete"
    assert cursor_key not in redis_client.values
