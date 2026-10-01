"""Cache payloads nominate IDs; real SQL remains authoritative across processes."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from api.db.cache import CacheService
from api.db.models import (
    ClarificationQueue,
    Memory,
    MemoryCategory,
    PlanTier,
    ProxyUser,
    Tenant,
)
from api.routers.memories import answer_memory_clarification
from api.schemas.requests import MemoryClarificationAnswerRequest
from api.services.proxy_user_service import ProxyUserService
from api.services.retriever import RetrieverService
from tests.integration.test_source_decision_postgres import (
    _Embedding,
    _decision,
    _run,
    sql_scope as sql_scope,
)


def _cache():
    cache = AsyncMock()
    cache.get_retrieval_results.return_value = None
    cache.get_hot_tier_memories.return_value = []
    return cache


def _retriever(session, cache, *, search=None):
    service = RetrieverService(
        session=session,
        qdrant_service=search or SimpleNamespace(),
        cache_service=cache,
        quota_manager=SimpleNamespace(),
        proxy_user_service=ProxyUserService(session=session, cache_service=cache),
        embedding_service=_Embedding(),
    )
    service._queue_access_update = lambda _: None
    return service


def _args(tenant_id, proxy_id, external_id):
    return dict(
        query="Which language is my default?",
        external_user_id=external_id,
        proxy_user_id=str(proxy_id),
        tenant_id=str(tenant_id),
        quota_mode="full",
    )


def _cached_api_process(connection, tenant_id, proxy_id, external_id):
    """Two independent process-local L1s share a real Redis cache and PostgreSQL."""

    async def run():
        import api.services.retriever as module

        module.L1_CACHE_TTL_SECONDS = (
            3600  # Prove correctness without waiting for expiry.
        )
        module.REDIS_CACHE_READ_ENABLED = True
        module.REDIS_CACHE_WRITE_ENABLED = True
        engine = create_async_engine(os.environ["DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cache = CacheService(use_direct_breaker=True)
        try:
            async with sessions() as session:
                before = await _retriever(session, cache).retrieve(
                    **_args(tenant_id, proxy_id, external_id)
                )
                assert before and "C++" in before[0].content
            await asyncio.sleep(0.05)  # Allow the real Redis cache write to finish.
            connection.send(("ready", [row.id for row in before]))
            assert await asyncio.to_thread(connection.recv) == "read_after_selection"
            assert any(
                "C++" in row.content
                for _, rows in RetrieverService._l1_cache.values()
                for row in rows
            )
            async with sessions() as session:
                after = await _retriever(session, cache).retrieve(
                    **_args(tenant_id, proxy_id, external_id)
                )
                connection.send(("after", [row.id for row in after]))
        finally:
            await cache.client.aclose()
            await engine.dispose()

    try:
        asyncio.run(run())
    except BaseException as exc:
        connection.send(("error", type(exc).__name__))
        raise
    finally:
        connection.close()


def test_two_api_processes_reject_old_l1_after_real_chat_selection(
    sql_scope, monkeypatch
):
    initial, _, _ = _run(
        sql_scope, monkeypatch, "My default programming language is C++.", _decision()
    )
    old_id = initial["stored_memories"][0]["id"]
    result, _, _ = _run(
        sql_scope,
        monkeypatch,
        "Python might be my default, but I have not decided to replace C++.",
        _decision(
            old_id,
            state="tentative",
            relation="ambiguous",
            option={
                "attribute": "default programming language",
                "value": "Python",
                "category": "preference",
            },
        ),
    )
    new_id = result["stored_memories"][0]["id"]
    with sql_scope.factory() as session:
        clarification_id = (
            session.scalars(
                select(ClarificationQueue).where(
                    ClarificationQueue.proxy_user_id == sql_scope.proxy_id
                )
            )
            .one()
            .id
        )
    context = multiprocessing.get_context("spawn")
    workers = []
    try:
        for _ in range(2):
            parent, child = context.Pipe()
            process = context.Process(
                target=_cached_api_process,
                args=(
                    child,
                    str(sql_scope.tenant_id),
                    str(sql_scope.proxy_id),
                    sql_scope.external_id,
                ),
            )
            process.start()
            child.close()
            workers.append((process, parent))
        for _, connection in workers:
            assert connection.poll(30), "API process did not finish initial retrieval"
            assert connection.recv() == ("ready", [old_id])

        async def resolve():
            engine = create_async_engine(os.environ["DATABASE_URL"])
            cache = CacheService(use_direct_breaker=True)
            try:
                async with async_sessionmaker(
                    engine, expire_on_commit=False
                )() as session:
                    request = Request(
                        {"type": "http", "method": "POST", "path": "/", "headers": []}
                    )
                    request.state.request_id = "cross-process-cache-test"
                    answer = await answer_memory_clarification(
                        request,
                        str(clarification_id),
                        MemoryClarificationAnswerRequest(
                            external_user_id=sql_scope.external_id, answer="B"
                        ),
                        ProxyUserService(session=session, cache_service=cache),
                        session,
                        str(sql_scope.tenant_id),
                    )
                    assert answer.data.resolved
            finally:
                await cache.client.aclose()
                await engine.dispose()

        asyncio.run(resolve())
        for _, connection in workers:
            connection.send("read_after_selection")
        for process, connection in workers:
            assert connection.poll(30), (
                "API process did not finish post-selection retrieval"
            )
            assert connection.recv() == ("after", [new_id])
            process.join(10)
            assert process.exitcode == 0
    finally:
        for process, connection in workers:
            if process.is_alive():
                process.terminate()
                process.join(10)
            connection.close()


@pytest.mark.parametrize("tier", ["redis", "hot_redis", "hot_l1", "postgres_fallback"])
@pytest.mark.parametrize(
    "mutation", ["content", "archive", "delete", "expire", "future"]
)
def test_stale_cached_payload_is_not_current_authority(
    sql_scope, monkeypatch, tier, mutation
):
    monkeypatch.setattr("api.services.retriever.REDIS_CACHE_READ_ENABLED", True)
    initial, _, _ = _run(
        sql_scope, monkeypatch, "My default programming language is C++.", _decision()
    )
    memory_id = uuid.UUID(initial["stored_memories"][0]["id"])

    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cache = _cache()
        try:
            async with sessions() as session:
                service = _retriever(session, cache)
                memory = await session.get(Memory, memory_id)
                cached = service._memory_to_result(memory, semantic_score=0.9)
                with sql_scope.factory.begin() as writer:
                    if mutation == "delete":
                        writer.execute(delete(Memory).where(Memory.id == memory_id))
                    else:
                        values = {
                            "content": {
                                "content": "My current language is Python.",
                                "metadata_json": {
                                    "provenance": {"service": "corrected"}
                                },
                            },
                            "archive": {"is_archived": True},
                            "expire": {
                                "expires_at": datetime.now(UTC) - timedelta(seconds=1)
                            },
                            "future": {
                                "effective_from": datetime.now(UTC) + timedelta(days=1)
                            },
                        }[mutation]
                        writer.execute(
                            update(Memory)
                            .where(Memory.id == memory_id)
                            .values(**values)
                        )
                # Preserve the ORM identity map and stale payload to expose both forms of staleness.
                if tier == "redis":
                    cache.get_retrieval_results.return_value = [asdict(cached)]
                elif tier == "hot_redis":
                    cache.get_hot_tier_memories.return_value = [asdict(cached)]
                elif tier == "hot_l1":
                    key = service._hot_tier_cache_key(
                        str(sql_scope.proxy_id), [], None, None
                    )
                    service._hot_tier_cache[key] = (
                        datetime.now(UTC).timestamp() + 3600,
                        [cached],
                    )
                if tier == "postgres_fallback":
                    results = await service._retrieve_postgres_fallback(
                        proxy_user_id=str(sql_scope.proxy_id),
                        user_id=None,
                        limit=10,
                        categories=[],
                        agent_id=None,
                        created_after=None,
                    )
                else:
                    results = await service.retrieve(
                        **_args(
                            sql_scope.tenant_id,
                            sql_scope.proxy_id,
                            sql_scope.external_id,
                        )
                    )
                if mutation == "content":
                    assert (
                        len(results) == 1
                        and results[0].content == "My current language is Python."
                    )
                    assert results[0].provenance == {"service": "corrected"}
                else:
                    assert results == []
                await asyncio.sleep(0)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_complete_vector_payload_cannot_bypass_sql_scope_or_provenance(
    sql_scope, monkeypatch
):
    initial, _, _ = _run(
        sql_scope, monkeypatch, "My default programming language is C++.", _decision()
    )
    memory_id = uuid.UUID(initial["stored_memories"][0]["id"])
    with sql_scope.factory.begin() as session:
        original = session.get(Memory, memory_id)
        for index in range(4):
            session.add(
                Memory(
                    user_id=original.user_id,
                    proxy_user_id=original.proxy_user_id,
                    content=f"Unrelated current fact {index}.",
                    category=MemoryCategory.fact,
                    importance_score=5,
                    confidence_score=0.9,
                    embedding_id=str(uuid.uuid4()),
                    embedding_model_id=original.embedding_model_id,
                    source_conversation_id=original.source_conversation_id,
                    metadata_json={},
                )
            )
        forged_id = uuid.uuid4()
        session.add(
            Memory(
                id=forged_id,
                user_id=original.user_id,
                proxy_user_id=original.proxy_user_id,
                content="Archived old default is Rust.",
                category=MemoryCategory.preference,
                importance_score=9,
                confidence_score=0.9,
                embedding_id=str(forged_id),
                embedding_model_id=original.embedding_model_id,
                source_conversation_id=original.source_conversation_id,
                is_archived=True,
                metadata_json={},
            )
        )

    class Search:
        breaker = SimpleNamespace(current_state=lambda: "CLOSED")

        async def search_memories_async(self, **kwargs):
            return [
                SimpleNamespace(
                    id=str(item),
                    score=0.9,
                    payload={
                        "memory_id": str(item),
                        "content": "Forged payload: default is Rust.",
                        "category": "preference",
                        "importance_score": 9,
                        "provenance": {"authority": 100},
                    },
                )
                for item in (memory_id, forged_id, uuid.uuid4())
            ]

    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                results = await _retriever(session, _cache(), search=Search()).retrieve(
                    **_args(
                        sql_scope.tenant_id, sql_scope.proxy_id, sql_scope.external_id
                    )
                )
                assert [row.id for row in results] == [str(memory_id)]
                assert "C++" in results[0].content
                assert results[0].provenance.get("authority") != 100
                await asyncio.sleep(0)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("other_tenant", [False, True])
@pytest.mark.parametrize("tier", ["redis", "hot_redis"])
def test_cache_cannot_launder_another_users_memory(
    sql_scope, monkeypatch, other_tenant, tier
):
    monkeypatch.setattr("api.services.retriever.REDIS_CACHE_READ_ENABLED", True)
    own, _, _ = _run(
        sql_scope, monkeypatch, "My default programming language is C++.", _decision()
    )
    tenant_id = uuid.uuid4() if other_tenant else sql_scope.tenant_id
    proxy_id = uuid.uuid4()
    external_id = f"foreign-{proxy_id}"
    with sql_scope.factory.begin() as session:
        if other_tenant:
            session.add(
                Tenant(
                    id=tenant_id,
                    company_name="Other cache tenant",
                    region_id="IN1",
                    plan_tier=PlanTier.starter,
                    metadata_json={},
                )
            )
            session.flush()
            sql_scope.tenant_ids.append(tenant_id)
        session.add(
            ProxyUser(
                id=proxy_id,
                tenant_id=tenant_id,
                external_user_id=external_id,
                external_user_id_hash=ProxyUserService.hash_external_user_id(
                    str(tenant_id), external_id
                ),
                metadata_json={},
            )
        )
    sql_scope.proxy_ids.append(proxy_id)
    foreign_scope = SimpleNamespace(
        **{
            **vars(sql_scope),
            "tenant_id": tenant_id,
            "proxy_id": proxy_id,
            "external_id": external_id,
        }
    )
    foreign, _, _ = _run(
        foreign_scope, monkeypatch, "My private default language is Rust.", _decision()
    )
    foreign_id = uuid.UUID(foreign["stored_memories"][0]["id"])

    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                cache = _cache()
                service = _retriever(session, cache)
                foreign_memory = await session.get(Memory, foreign_id)
                stale = asdict(
                    service._memory_to_result(foreign_memory, semantic_score=0.99)
                )
                if tier == "redis":
                    cache.get_retrieval_results.return_value = [stale]
                else:
                    cache.get_hot_tier_memories.return_value = [stale]
                results = await service.retrieve(
                    **_args(
                        sql_scope.tenant_id, sql_scope.proxy_id, sql_scope.external_id
                    )
                )
                assert [row.id for row in results] == [own["stored_memories"][0]["id"]]
                assert all("Rust" not in row.content for row in results)
                await asyncio.sleep(0)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
