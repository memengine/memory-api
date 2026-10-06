"""Owned current SQL scopes, controlled search nominations, no real providers."""
import asyncio
import copy
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import Memory, MemoryClaim, ProxyUser, Tenant
from api.schemas.preferences import PreferenceReadContext
from api.services.preference_discovery import discover_preferences, preference_query
from api.services.retriever import RetrieverService
from api.settings import get_settings
from tests.integration.test_preference_choice_policy import withhold
from tests.integration.test_preference_fact_transaction import fact
from tests.integration.test_preference_snapshots import forbid_real_providers as _forbid
from tests.integration.test_preference_snapshots import offered, seed
from tests.integration.test_runtime_preference_ingestion import ingestion as _ingestion
from tests.integration.test_source_decision_postgres import sql_scope as _sql_scope

sql_scope = _sql_scope
forbid_real_providers = _forbid
ingestion = _ingestion
PROJECT = {"kind": "context", "namespace": "project", "key": "Cedar"}


def enable(scope, monkeypatch):
    monkeypatch.setattr(get_settings(), "runtime_preference_discovery_enabled", True)
    monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", True)
    monkeypatch.setattr(get_settings(), "runtime_preference_tenant_ids", str(scope.tenant_id))
    catalog = offered(scope).model_dump(mode="json")
    catalog.pop("tenant_id")
    with scope.factory.begin() as session:
        session.get(Tenant, scope.tenant_id).metadata_json = {"runtime_preference_catalog": catalog}


def read(scope, nominee_ids=(), *, cold=False, context=None, owner=None, tenant=None, raw=False, held=()):
    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                service = RetrieverService(session=session, qdrant_service=SimpleNamespace(),
                    cache_service=AsyncMock(), quota_manager=AsyncMock(), embedding_service=SimpleNamespace())
                service._queue_access_update = lambda _: None
                if raw:
                    return await discover_preferences(session, tenant_id=tenant or scope.tenant_id,
                        owner=owner or scope.proxy_id, nominee_ids=list(nominee_ids), cold_start=cold,
                        is_current=service._memory_is_current, withheld_fingerprints=held)
                async def candidates(**kwargs):
                    service._resolved_read_proxy_id = str(owner or scope.proxy_id)
                    service._preference_nominee_ids = list(nominee_ids)
                    service._preference_cold_start = cold
                    service.last_quota_mode = "FULL"
                    return []
                service._retrieve_candidates = candidates
                items = await service.retrieve(query="anything, no scope parsing", external_user_id=scope.external_id,
                    proxy_user_id=str(owner or scope.proxy_id), tenant_id=str(tenant or scope.tenant_id),
                    preference_context=context)
                return items, service.last_preference_scope_request, service.last_memory_visibility, service.last_preference_decisions
        finally:
            await engine.dispose()
    return asyncio.run(exercise())


@pytest.mark.parametrize("cold", [False, True])
def test_ordinary_get_selects_owned_current_global_without_explicit_keys(sql_scope, monkeypatch, cold):
    current = seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    items, request, visibility, decisions = read(sql_scope, [current], cold=cold)
    assert [item.id for item in items] == [str(current)]
    assert request is visibility is None and not decisions
    assert items[0].provenance["authority_priority"] == 20


@pytest.mark.parametrize("nominated", ["global", "project", "both"])
def test_missing_scope_never_defaults_global_even_when_search_misses_scoped_value(sql_scope, monkeypatch, nominated):
    global_id = seed(sql_scope, monkeypatch)
    scoped_id = seed(sql_scope, monkeypatch, value="Python", scope=PROJECT)
    enable(sql_scope, monkeypatch)
    ids = {"global": [global_id], "project": [scoped_id], "both": [global_id, scoped_id]}[nominated]
    items, request, visibility, _ = read(sql_scope, ids)
    assert not items and visibility is None
    assert request.property_keys == ("examples.language",)
    assert {scope.kind for scope in request.available_scopes} == {"global", "context"}
    # The customer's explicit scope selection uses the existing governed selector.
    frame = read(sql_scope, ids, raw=True)
    async def chosen():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                service = RetrieverService(session=session, qdrant_service=SimpleNamespace(),
                    cache_service=AsyncMock(), quota_manager=AsyncMock(), embedding_service=SimpleNamespace())
                service._queue_access_update = lambda _: None
                return await service._retrieve_declared_preferences(context=PreferenceReadContext(
                    applicability=PROJECT, property_keys=request.property_keys), query="q", identity=None,
                    proxy_user_id=str(sql_scope.proxy_id), tenant_id=str(sql_scope.tenant_id), limit=10,
                    categories=["preference"], agent_id=None, time_filter_days=None, quota_mode="FULL", include_other=False)
        finally:
            await engine.dispose()
    assert frame.context is None
    assert [item.id for item in asyncio.run(chosen())] == [str(scoped_id)]


def test_unaffected_global_property_survives_missing_scope_question(sql_scope, monkeypatch):
    global_id = seed(sql_scope, monkeypatch)
    seed(sql_scope, monkeypatch, value="Python", scope=PROJECT)
    other = seed(sql_scope, monkeypatch, value="Brief", property_key="answer.detail")
    enable(sql_scope, monkeypatch)
    items, request, _, _ = read(sql_scope, [global_id, other])
    assert [item.id for item in items] == [str(other)] and request.property_keys == ("examples.language",)


@pytest.mark.parametrize("corrupt", ["removed_scope", "binding", "missing_winner", "archived_winner"])
def test_catalog_and_binding_failures_do_not_activate_global_fallback(sql_scope, monkeypatch, corrupt):
    current = seed(sql_scope, monkeypatch)
    scoped = seed(sql_scope, monkeypatch, value="Python", scope=PROJECT)
    enable(sql_scope, monkeypatch)
    with sql_scope.factory.begin() as session:
        claim = session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == scoped)).one()
        if corrupt == "removed_scope":
            tenant = session.get(Tenant, sql_scope.tenant_id)
            raw = tenant.metadata_json["runtime_preference_catalog"]
            tenant.metadata_json = {"runtime_preference_catalog": {**raw, "scopes": raw["scopes"][:1]}}
        elif corrupt == "binding":
            claim.scope = {"kind": "global"}
        elif corrupt == "missing_winner":
            claim.active_memory_id = None
        else:
            session.get(Memory, scoped).is_archived = True
    items, request, visibility, _ = read(sql_scope, [current])
    if corrupt in {"removed_scope", "binding"}:
        assert not items and request is None and visibility.state == "unavailable"
    elif corrupt == "missing_winner":
        assert not items and request is not None and visibility is None
    else:
        assert [item.id for item in items] == [str(current)] and request is visibility is None


def test_unresolved_global_choice_and_scoped_choice_remain_withheld(sql_scope, monkeypatch):
    global_id = seed(sql_scope, monkeypatch)
    withhold(sql_scope, monkeypatch, str(global_id))
    enable(sql_scope, monkeypatch)
    items, request, visibility, decisions = read(sql_scope, [global_id])
    assert not items and request is visibility is None and len(decisions) == 1
    scoped = seed(sql_scope, monkeypatch, value="Rust", scope=PROJECT)
    withhold(sql_scope, monkeypatch, str(scoped), applicability=PROJECT)
    items, request, visibility, _ = read(sql_scope, [global_id])
    assert not items and request is not None and visibility is None


def test_foreign_nominations_blocked_owner_and_absent_catalog(sql_scope, monkeypatch):
    current = seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    assert read(sql_scope, [uuid.uuid4()], raw=True).nominated_keys == ()
    assert read(sql_scope, [current], owner=uuid.uuid4(), raw=True).nominated_keys == ()
    assert read(sql_scope, [current], tenant=uuid.uuid4())[0] == []
    with sql_scope.factory.begin() as session:
        session.get(Tenant, sql_scope.tenant_id).metadata_json = {}
    assert read(sql_scope, [current])[2].state == "unavailable"
    with sql_scope.factory.begin() as session:
        session.get(ProxyUser, sql_scope.proxy_id).is_blocked = True
    assert read(sql_scope, [current])[:2] == ([], None)


def test_scoped_processing_target_alone_requires_scope_not_global(sql_scope, monkeypatch):
    current = seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    from api.schemas.preferences import PreferenceApplicability
    from api.services.preference_claim_service import preference_fingerprint
    frame = read(sql_scope, [current], raw=True, held=[preference_fingerprint("examples.language", PreferenceApplicability(**PROJECT))])
    assert frame.context is None and frame.scope_request is not None


def test_read_flag_rollback_leaves_ordinary_path_unchanged(sql_scope, monkeypatch):
    current = seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    monkeypatch.setattr(get_settings(), "runtime_preference_discovery_enabled", False)
    assert read(sql_scope, [current]) == ([], None, None, [])


def test_residual_hold_has_priority_over_scope_question(sql_scope, ingestion, monkeypatch):
    from tests.integration.test_runtime_fact_visibility import prepare
    from tests.integration.test_runtime_pending_visibility import read as runtime_read
    scoped = seed(sql_scope, monkeypatch, value="Rust", scope=PROJECT)
    monkeypatch.setattr(get_settings(), "runtime_preference_discovery_enabled", True)
    old, unrelated, pref, _, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, mixed=True)
    items, visibility, decisions = runtime_read(sql_scope, [old, unrelated, pref, scoped])
    assert visibility.state == "unavailable" and visibility.scope == "affected_context"
    assert str(old) not in {item.id for item in items} and str(unrelated) in {item.id for item in items}
    assert not any(item.category == "preference" for item in items) and not decisions


def test_overflow_fails_closed_before_missing_catalog_could_be_treated_as_global(sql_scope, monkeypatch):
    current = seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    with sql_scope.factory.begin() as session:
        template = session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == current)).one()
        session.add_all([MemoryClaim(tenant_id=sql_scope.tenant_id, proxy_user_id=sql_scope.proxy_id,
            category="preference", subject_key=template.subject_key, predicate_key=template.predicate_key,
            claim_fingerprint=f"{i:064x}", scope={"kind": "context", "namespace": "project", "key": str(i)},
            status="active") for i in range(33)])
    assert read(sql_scope, [current])[2].state == "unavailable"


def test_real_vector_and_cache_path_rechecks_scopes_and_suppresses_legacy_preferences(sql_scope, monkeypatch):
    current = seed(sql_scope, monkeypatch)
    fact_id = fact(sql_scope, "My city is Pune")
    legacy_id = uuid.uuid4()
    with sql_scope.factory.begin() as session:
        existing = session.get(Memory, current)
        session.add(Memory(id=legacy_id, user_id=existing.user_id, proxy_user_id=sql_scope.proxy_id,
            content="Legacy unknown default is Ruby", category="preference", importance_score=8, confidence_score=0.9,
            embedding_id=str(legacy_id), source_conversation_id=existing.source_conversation_id,
            embedding_model_id=existing.embedding_model_id))
    enable(sql_scope, monkeypatch)
    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        cache_pages = {}
        searches = []
        async def get_page(owner, key):
            return copy.deepcopy(cache_pages.get(key))
        async def set_page(owner, key, page, **kwargs):
            assert key.endswith("|preference-discovery-v1")
            cache_pages[key] = copy.deepcopy(page)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                service = RetrieverService(session=session,
                    qdrant_service=SimpleNamespace(breaker=SimpleNamespace(current_state=lambda: "CLOSED")),
                    cache_service=SimpleNamespace(get_retrieval_results=get_page, set_retrieval_results=set_page,
                        get_hot_tier_memories=AsyncMock(return_value=[])),
                    quota_manager=AsyncMock(), embedding_service=SimpleNamespace(), proxy_user_service=SimpleNamespace())
                service._queue_access_update = lambda _: None
                service._candidate_embedding_model_ids = AsyncMock(return_value=["controlled"])
                service._embed_query = AsyncMock(return_value=SimpleNamespace(vector=[0.1], qdrant_collection="test"))
                service._count_proxy_user_memories = AsyncMock(return_value=10)
                async def search(**kwargs):
                    searches.append(kwargs)
                    return [SimpleNamespace(id=str(mid), score=0.99, payload={"memory_id": str(mid)}) for mid in (current, fact_id, legacy_id)]
                service._search_qdrant = search
                args = dict(query="code language", external_user_id=sql_scope.external_id,
                    tenant_id=str(sql_scope.tenant_id), proxy_user_id=str(sql_scope.proxy_id), quota_mode="FULL")
                first = await service.retrieve(**args)
                await asyncio.sleep(0)  # Flush only this request's existing asynchronous cache delivery.
                assert {m.id for m in first} == {str(current), str(fact_id)}
                assert len(searches) == 1 and cache_pages
                await asyncio.to_thread(seed, sql_scope, monkeypatch, value="Python", scope=PROJECT)
                second = await service.retrieve(**args)
                assert [m.id for m in second] == [str(fact_id)]
                assert service.last_preference_scope_request is not None
                assert len(searches) == 1  # Existing cache IDs nominate; fresh SQL found the new scoped binding.
                assert "Ruby" not in str(second)
                assert service._embed_query.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(exercise())


@pytest.mark.parametrize("rows", [1000, 100000])
def test_nominated_property_scope_plan_is_indexed_with_bounded_coverage(sql_scope, rows):
    # No customer rows or schema changed; use a connection-local temporary table.
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex
    with sql_scope.factory() as session:
        session.execute(text("CREATE TEMP TABLE discovery_plan (LIKE memory_claims INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP"))
        session.execute(text("INSERT INTO discovery_plan (tenant_id,proxy_user_id,category,claim_fingerprint,subject_key,predicate_key) "
            "SELECT :tenant,:owner,'preference',md5(i::text),'user',CASE WHEN i <= 2 THEN 'explicit_preference_v1.examples.language' "
            "ELSE 'explicit_preference_v1.other.' || i END FROM generate_series(1,:rows) i"),
            {"tenant": sql_scope.tenant_id, "owner": sql_scope.proxy_id, "rows": rows})
        index = next(i for i in MemoryClaim.__table__.indexes if i.name == "ix_memory_claims_preference_scopes")
        ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect())).replace("memory_claims", "discovery_plan")
        session.execute(text(ddl))
        session.execute(text("ANALYZE discovery_plan"))
        compiled = preference_query(tenant_id=sql_scope.tenant_id, owner=sql_scope.proxy_id,
            predicates=["explicit_preference_v1.examples.language"]).compile(
                dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True})
        sql = str(compiled).replace("memory_claims", "discovery_plan")
        arguments = {}
        for index, (key, value) in enumerate(compiled.params.items(), 1):
            sql = sql.replace(f"%({key})s", f"${index}")
            arguments[f"p{index}"] = value
        session.execute(text("PREPARE discovery_scopes AS " + sql))
        session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
        execution = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) EXECUTE discovery_scopes(" + ",".join(f":{key}" for key in arguments) + ")"
        plan = session.execute(text(execution), arguments).scalar_one()[0]
        assert plan["Plan"]["Actual Rows"] == 2
        assert "ix_discovery_plan_preference_scopes" in str(plan)
        # No statement about production p99/load follows from a local query-plan test.
