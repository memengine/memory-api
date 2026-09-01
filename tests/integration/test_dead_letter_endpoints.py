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
from api.middleware.auth import AuthMiddleware


ADMIN_HEADERS = {"X-Admin-Secret": os.environ["ADMIN_SECRET"]}


async def bypass_auth(self, request, call_next):
    request.state.tenant_id = "11111111-1111-1111-1111-111111111111"
    request.state.auth_scheme = "apikey"
    return await call_next(request)


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

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeAsyncSession:
    def __init__(self, job) -> None:
        self.job = job
        self.commit_calls = 0

    async def execute(self, _statement):
        return FakeExecuteResult([self.job] if self.job.status == ExtractionJobStatus.dead else [])

    async def get(self, _model, identifier):
        if str(identifier) == str(self.job.id):
            return self.job
        return None

    async def commit(self):
        self.commit_calls += 1


def test_dead_letter_list_and_retry(monkeypatch) -> None:
    monkeypatch.setattr(AuthMiddleware, "dispatch", bypass_auth)
    sent_tasks = []
    monkeypatch.setattr("api.routers.internal.celery_app.send_task", lambda *args, **kwargs: sent_tasks.append((args, kwargs)))

    job = SimpleNamespace(
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
        completed_at=None,
        dead_lettered_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        celery_task_id=None,
        memories_created=0,
    )

    cache_service = CacheService(client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    import asyncio
    asyncio.run(cache_service.client.set("tenant:11111111-1111-1111-1111-111111111111:plan", "starter", ex=300))

    app = create_app()
    app.state.cache_service = cache_service
    app.state.qdrant_service = object()

    async def override_db_session():
        yield FakeAsyncSession(job)

    app.dependency_overrides[get_db_session] = override_db_session

    with TestClient(app) as client:
        list_response = client.get("/v1/internal/dead-letter-jobs", headers=ADMIN_HEADERS)
        retry_response = client.post(f"/v1/internal/dead-letter-jobs/{job.id}/retry", headers=ADMIN_HEADERS)

    assert list_response.status_code == 200
    assert list_response.json()["data"][0]["id"] == str(job.id)
    assert retry_response.status_code == 200
    assert retry_response.json()["data"]["status"] == "queued"
    assert job.status == ExtractionJobStatus.queued
    assert job.attempts == 0
    assert sent_tasks
