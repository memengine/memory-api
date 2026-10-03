"""Isolated PostgreSQL checks for declared-preference lifecycle and review bounds."""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import ExtractionJob, Memory, MemoryClaim, PendingExtractionCandidate
from api.routers.memories import _memory_to_data
from api.services.memory_service import MemoryService
from api.services.source_review_service import view_review
from api.settings import get_settings
from api.tasks import extraction_tasks
from api.tasks.source_review_tasks import run_source_review_maintenance
from tests.integration.test_source_decision_postgres import (
    _declare,
    _preference,
    _read_preferences,
)
from tests.integration.test_source_decision_postgres import (
    sql_scope as _sql_scope_fixture,
)

sql_scope = _sql_scope_fixture


def manage(scope, memory_id, *, hard_delete=None):
    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                service = MemoryService(session=session, cache_service=AsyncMock(), qdrant_service=AsyncMock(), quota_manager=AsyncMock())
                service._embedding_collection_for_memory = AsyncMock(return_value="memories")
                service._invalidate_retrieval_caches = AsyncMock()
                args = {"authenticated_user_id": None, "memory_id": memory_id,
                        "tenant_id": str(scope.tenant_id), "external_user_id": scope.external_id}
                if hard_delete is None:
                    result = await service.update_memory(content=None, importance_score=None, is_archived=True, **args)
                    return _memory_to_data(result)
                return await service.delete_memory(hard_delete=hard_delete, **args)
        finally:
            await engine.dispose()
    return asyncio.run(exercise())


@pytest.mark.parametrize("same_value", [False, True])
def test_archived_preference_requires_versioned_renewal_and_preserves_global(sql_scope, monkeypatch, same_value):
    global_result, _ = _declare(sql_scope, monkeypatch, _preference())
    context = {"kind": "context", "namespace": "project", "key": "Release"}
    project, _ = _declare(sql_scope, monkeypatch, _preference("Python", scope=context))
    old_id = project["stored_memories"][0]["id"]
    archived = manage(sql_scope, old_id)
    assert archived.is_archived and len(archived.provenance["preference_version"]) == 64
    assert [m.id for m in _read_preferences(sql_scope, monkeypatch, context)] == [global_result["stored_memories"][0]["id"]]
    with sql_scope.factory() as session:
        claim = session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == uuid.UUID(old_id))).one()
        assert claim.status == "archived" and claim.authority_priority == 20
    value = "Python" if same_value else "Rust"
    blocked, _ = _declare(sql_scope, monkeypatch, _preference(value, scope=context))
    assert blocked["memories_created"] == 0
    replacement = _preference(value, scope=context, commitment="replacement",
        replaces_memory_id=old_id, replaces_version=archived.provenance["preference_version"])
    renewed, _ = _declare(sql_scope, monkeypatch, replacement)
    assert renewed["memories_created"] == 1
    new_id = renewed["stored_memories"][0]["id"]
    assert new_id != old_id and [m.id for m in _read_preferences(sql_scope, monkeypatch, context)] == [new_id]
    assert [m.id for m in _read_preferences(sql_scope, monkeypatch, {"kind": "global"})] == [global_result["stored_memories"][0]["id"]]
    stale, _ = _declare(sql_scope, monkeypatch, replacement)
    assert stale["memories_created"] == 0
    # Archiving a superseded revision must not archive the new winner's ledger.
    manage(sql_scope, old_id)
    assert [m.id for m in _read_preferences(sql_scope, monkeypatch, context)] == [new_id]
    with sql_scope.factory() as session:
        assert session.get(Memory, uuid.UUID(new_id)).previous_version_id == uuid.UUID(old_id)
        assert session.get(Memory, uuid.UUID(old_id)).is_archived


def test_hard_delete_never_resurrects_superseded_preference(sql_scope, monkeypatch):
    initial, _ = _declare(sql_scope, monkeypatch, _preference())
    old_id = initial["stored_memories"][0]["id"]
    archived = manage(sql_scope, old_id, hard_delete=False)
    assert archived is True
    with sql_scope.factory() as session:
        version = _memory_to_data(session.get(Memory, uuid.UUID(old_id))).provenance["preference_version"]
    renewed, _ = _declare(sql_scope, monkeypatch, _preference("Python", commitment="replacement", replaces_memory_id=old_id, replaces_version=version))
    assert manage(sql_scope, renewed["stored_memories"][0]["id"], hard_delete=True)
    assert _read_preferences(sql_scope, monkeypatch, {"kind": "global"}) == []
    fresh, _ = _declare(sql_scope, monkeypatch, _preference("Rust"))
    assert fresh["memories_created"] == 1


def test_closed_review_replay_new_generation_and_fixed_lifetime(sql_scope, monkeypatch):
    result, job_id = _declare(sql_scope, monkeypatch, _preference("Python", commitment="uncertain"))
    assert result["pending_candidates_buffered"] == 1
    with sql_scope.factory() as session:
        candidate_id = session.scalars(select(PendingExtractionCandidate).where(PendingExtractionCandidate.extraction_job_id == job_id)).one().id
    _, reinforced_job = _declare(sql_scope, monkeypatch, _preference("Python", commitment="uncertain"))
    with sql_scope.factory.begin() as session:
        candidate = session.get(PendingExtractionCandidate, candidate_id)
        candidate.status = "dismissed"
        candidate.metadata_json = {**candidate.metadata_json, "source_review_resolution": {"action": "dismiss"}}
        candidate_id = candidate.id
    with sql_scope.factory() as session:
        payload = dict(session.get(ExtractionJob, job_id).payload)
    replay = extraction_tasks.run_extraction_pipeline(payload, session_factory=sql_scope.factory, qdrant_service=AsyncMock())
    assert replay["pending_candidates_buffered"] == 0
    assert replay["extraction_metadata"]["source_reviews_closed_replays"] == 1
    again, new_job = _declare(sql_scope, monkeypatch, _preference("Python", commitment="uncertain"))
    assert again["pending_candidates_buffered"] == 1 and new_job != job_id
    with sql_scope.factory() as session:
        old_reinforcement = dict(session.get(ExtractionJob, reinforced_job).payload)
    stale_replay = extraction_tasks.run_extraction_pipeline(old_reinforcement, session_factory=sql_scope.factory, qdrant_service=AsyncMock())
    assert stale_replay["pending_candidates_buffered"] == 0
    with sql_scope.factory.begin() as session:
        assert session.get(PendingExtractionCandidate, candidate_id).status == "dismissed"
        live = session.scalars(select(PendingExtractionCandidate).where(PendingExtractionCandidate.extraction_job_id == new_job)).one()
        assert live.status == "pending"
        live.created_at = datetime.now(UTC) - timedelta(days=8)
        live.last_seen_at = datetime.now(UTC)
        assert view_review(live, None, now=datetime.now(UTC)) is None
    report = run_source_review_maintenance(session_factory=sql_scope.factory, tenant_id=sql_scope.tenant_id)
    assert report["expired"] >= 1


@pytest.mark.parametrize("setting", ["source_review_profile_row_limit", "source_review_tenant_row_limit"])
def test_review_capacity_is_reported_not_activated(sql_scope, monkeypatch, setting):
    monkeypatch.setattr(get_settings(), setting, 1)
    first, _ = _declare(sql_scope, monkeypatch, _preference("Python", commitment="uncertain"))
    assert first["pending_candidates_buffered"] == 1
    second, _ = _declare(sql_scope, monkeypatch, _preference("Rust", commitment="uncertain"))
    assert second["pending_candidates_buffered"] == second["memories_created"] == 0
    assert second["extraction_metadata"]["source_reviews_capacity_rejected"] == 1
    with sql_scope.factory() as session:
        assert len(session.scalars(select(PendingExtractionCandidate).where(PendingExtractionCandidate.proxy_user_id == sql_scope.proxy_id)).all()) == 1


def test_review_cleanup_batches_and_old_job_cannot_recreate_purged_review(sql_scope, monkeypatch):
    _, job_id = _declare(sql_scope, monkeypatch, _preference("Python", commitment="uncertain"))
    old = datetime.now(UTC) - timedelta(days=40)
    with sql_scope.factory.begin() as session:
        session.execute(update(PendingExtractionCandidate).where(PendingExtractionCandidate.extraction_job_id == job_id).values(created_at=old))
        job = session.get(ExtractionJob, job_id)
        job.created_at = old
        payload = dict(job.payload)
    report = run_source_review_maintenance(session_factory=sql_scope.factory, batch_size=1, tenant_id=sql_scope.tenant_id)
    assert report["expired"] <= 1 and report["purged"] <= 1
    for _ in range(3):
        run_source_review_maintenance(session_factory=sql_scope.factory, batch_size=1, tenant_id=sql_scope.tenant_id)
    replay = extraction_tasks.run_extraction_pipeline(payload, session_factory=sql_scope.factory, qdrant_service=AsyncMock())
    assert replay["pending_candidates_buffered"] == 0
    assert replay["extraction_metadata"]["source_reviews_closed_replays"] == 1
    with sql_scope.factory() as session:
        assert session.scalars(select(PendingExtractionCandidate).where(PendingExtractionCandidate.extraction_job_id == job_id)).all() == []


def test_archive_during_preference_embedding_invalidates_replacement(sql_scope, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    from api.services.conflict_resolver import ConflictResolver
    from api.services.preference_claim_service import declared_preferences
    from tests.integration.test_source_decision_postgres import _embedding
    initial, _ = _declare(sql_scope, monkeypatch, _preference())
    old_id = initial["stored_memories"][0]["id"]
    with sql_scope.factory() as session:
        old = session.get(Memory, uuid.UUID(old_id))
        user_id, conversation_id = str(old.user_id), str(old.source_conversation_id)
        version = _memory_to_data(old).provenance["preference_version"]
    ready, release = Event(), Event()
    declaration = _preference("Python", commitment="replacement", replaces_memory_id=old_id, replaces_version=version)
    messages = [{"role": "user", "content": declaration["quote"]}]
    def embedding(text):
        ready.set()
        assert release.wait(timeout=10)
        return _embedding(text)
    def replace():
        with sql_scope.factory.begin() as session:
            resolver = ConflictResolver(session=session, qdrant_service=SimpleNamespace(),
                embedder=embedding, llm_service=SimpleNamespace(), source_messages=messages)
            stored = resolver.check_and_store(declared_preferences([declaration], messages),
                user_id=user_id, tenant_id=str(sql_scope.tenant_id), proxy_user_id=str(sql_scope.proxy_id),
                source_conversation_id=conversation_id, auto_commit=False)
            return len(stored), len(resolver.last_pending_candidates)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(replace)
        try:
            assert ready.wait(timeout=10)
            manage(sql_scope, old_id)
        finally:
            release.set()
        assert future.result(timeout=10) == (0, 1)
    assert _read_preferences(sql_scope, monkeypatch, {"kind": "global"}) == []


def test_concurrent_review_admission_cannot_exceed_capacity(sql_scope, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from api.schemas.extraction_schemas import PendingExtractedMemory
    from api.services.preference_claim_service import declared_preferences
    from api.services.source_review_service import review_intent
    monkeypatch.setattr(get_settings(), "source_review_profile_row_limit", 1)
    barrier = Barrier(2)
    jobs = [uuid.uuid4(), uuid.uuid4()]
    with sql_scope.factory.begin() as session:
        for job_id in jobs:
            session.add(ExtractionJob(id=job_id, tenant_id=sql_scope.tenant_id,
                proxy_user_id=sql_scope.proxy_id, external_user_id=sql_scope.external_id, payload={}))
    def admit(index):
        declaration = _preference(["Python", "Rust"][index], commitment="uncertain")
        evidence = declared_preferences([declaration], [{"role": "user", "content": declaration["quote"]}])[0].validated_evidence
        candidate = PendingExtractedMemory(content=declaration["quote"], category="preference",
            confidence=0.5, importance_score=5, reasoning="synthetic", candidate_reason="source_decision_pending",
            validated_evidence={**evidence, "source_review": review_intent(None)})
        barrier.wait(timeout=10)
        diagnostics = {}
        with sql_scope.factory.begin() as session:
            count = extraction_tasks._persist_pending_extraction_candidates(session, candidates=[candidate],
                tenant_id=str(sql_scope.tenant_id), proxy_user_id=str(sql_scope.proxy_id),
                extraction_job_id=str(jobs[index]), source_event_id=None, diagnostics=diagnostics)
        return count, diagnostics.get("source_reviews_capacity_rejected", 0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(admit, [0, 1])) == [(0, 1), (1, 0)]
