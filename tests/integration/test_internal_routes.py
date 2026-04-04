from __future__ import annotations

import os
import uuid
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

import fakeredis.aioredis
from fastapi.testclient import TestClient

from api.db.cache import CacheService
from api.db.database import get_db_session
from api.db.models import ExtractionJobStatus
from api.main import create_app


VALID_ADMIN_SECRET = os.environ["ADMIN_SECRET"]


class FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return FakeScalarResult(self._rows)


class FakeAsyncSession:
    def __init__(self, job) -> None:
        self.job = job

    async def execute(self, _statement):
        return FakeExecuteResult([self.job] if self.job.status == ExtractionJobStatus.dead else [])

    async def get(self, _model, identifier):
        if str(identifier) == str(self.job.id):
            return self.job
        return None

    async def commit(self):
        return None

    async def flush(self):
        return None


def build_dead_letter_app(monkeypatch, job):
    sent_tasks = []
    monkeypatch.setattr("api.routers.internal.celery_app.send_task", lambda *args, **kwargs: sent_tasks.append((args, kwargs)))

    cache_service = CacheService(client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    import asyncio
    asyncio.run(cache_service.client.set("tenant:11111111-1111-1111-1111-111111111111:plan", "starter", ex=300))

    app = create_app()
    app.state.cache_service = cache_service
    app.state.qdrant_service = object()

    async def override_db_session():
        yield FakeAsyncSession(job)

    app.dependency_overrides[get_db_session] = override_db_session
    return app, sent_tasks


def make_dead_job():
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        proxy_user_id=uuid.uuid4(),
        external_user_id="ext_user_1",
        status=ExtractionJobStatus.dead,
        attempts=3,
        queue_name="starter-extraction",
        error="boom",
        payload={"job_id": "job_1", "tenant_id": "11111111-1111-1111-1111-111111111111"},
        queued_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        processing_started_at=datetime.now(UTC),
        completed_at=None,
        dead_lettered_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        celery_task_id=None,
        memories_created=0,
        stale_after=None,
    )


def test_dead_letter_list_uses_admin_secret_only(monkeypatch) -> None:
    job = make_dead_job()
    app, _sent_tasks = build_dead_letter_app(monkeypatch, job)

    with TestClient(app) as client:
        response = client.get(
            "/v1/internal/dead-letter-jobs",
            headers={"X-Admin-Secret": VALID_ADMIN_SECRET},
        )

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == str(job.id)


def test_dead_letter_retry_uses_admin_secret_only(monkeypatch) -> None:
    job = make_dead_job()
    app, sent_tasks = build_dead_letter_app(monkeypatch, job)

    with TestClient(app) as client:
        response = client.post(
            f"/v1/internal/dead-letter-jobs/{job.id}/retry",
            headers={"X-Admin-Secret": VALID_ADMIN_SECRET},
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "queued"
    assert job.status == ExtractionJobStatus.queued
    assert job.attempts == 0
    assert sent_tasks
