"""Real PostgreSQL policy checks, controlled providers and synthetic owners only."""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    AuditLog,
    ExtractionJob,
    Memory,
    MemoryClaim,
    PendingExtractionCandidate,
    ProxyUser,
)
from api.errors import APIError
from api.routers.memories import _memory_to_data, answer_source_review
from api.schemas.preferences import PreferenceReadContext
from api.schemas.requests import MemorySourceReviewAnswerRequest
from api.services.preference_claim_service import CHOICE_STATE_MARKER
from api.services.proxy_user_service import ProxyUserService
from api.services.retriever import RetrieverService
from api.services.source_review_service import review_version
from api.settings import get_settings
from api.tasks import extraction_tasks
from api.tasks.source_review_tasks import run_source_review_maintenance
from tests.integration.test_preference_lifecycle_reviews import manage
from tests.integration.test_source_decision_postgres import (
    _declare,
    _preference,
    _read_preferences,
)
from tests.integration.test_source_decision_postgres import (
    sql_scope as _sql_scope_fixture,
)
from tests.unit.test_tenant_clarification_answer import _request

sql_scope = _sql_scope_fixture
PROJECT = {"kind": "context", "namespace": "project", "key": "Release"}


def snapshot(scope, memory_id):
    with scope.factory() as session:
        memory = session.get(Memory, uuid.UUID(memory_id))
        claim = session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == memory.id)).one()
        return _memory_to_data(memory), claim.status, claim.authority_priority


def withhold(scope, monkeypatch, memory_id, *, applicability=None, **kwargs):
    version = snapshot(scope, memory_id)[0].provenance["preference_version"]
    declaration = _preference("Python", scope=applicability, commitment="uncertain",
        choice_state="current_choice_unresolved", replaces_memory_id=memory_id,
        replaces_version=version, **kwargs)
    return _declare(scope, monkeypatch, declaration), declaration


def read_decisions(scope, applicability, *, properties=None, categories=None):
    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                service = RetrieverService(session=session, qdrant_service=SimpleNamespace(),
                    cache_service=AsyncMock(), quota_manager=AsyncMock(), proxy_user_service=SimpleNamespace(),
                    embedding_service=SimpleNamespace())
                service._queue_access_update = lambda _: None
                # Other categories are controlled here; preference selection uses real SQL.
                original = service.retrieve
                async def retrieve(**kwargs):
                    if kwargs.get("preference_context") is None:
                        service.last_preference_decisions = []  # Recursive reads must not erase outer outcomes.
                        return []
                    return await original(**kwargs)
                service.retrieve = retrieve
                items = await service.retrieve(query="language?", external_user_id=scope.external_id,
                    tenant_id=str(scope.tenant_id), proxy_user_id=str(scope.proxy_id),
                    categories=categories or ["preference"], quota_mode="DEGRADED",
                    preference_context=PreferenceReadContext(applicability=applicability,
                        property_keys=properties or ["examples.language"]))
                return items, service.last_preference_decisions
        finally:
            await engine.dispose()
    return asyncio.run(exercise())


def answer(scope, candidate_id, action, *, version=None):
    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                candidate = await session.get(PendingExtractionCandidate, candidate_id)
                proxy = await session.get(ProxyUser, scope.proxy_id)
                proxies = SimpleNamespace(find_existing=AsyncMock(return_value=proxy))
                return await answer_source_review(_request(), str(candidate_id), MemorySourceReviewAnswerRequest(
                    external_user_id=scope.external_id, version=version or review_version(candidate), action=action,
                ), proxies, session, str(scope.tenant_id))
        finally:
            await engine.dispose()
    return asyncio.run(exercise())


def review_id(scope, job_id):
    with scope.factory() as session:
        return session.scalars(select(PendingExtractionCandidate).where(
            PendingExtractionCandidate.extraction_job_id == job_id)).one().id


def test_interest_is_pending_but_does_not_suspend_default(sql_scope, monkeypatch):
    initial, _ = _declare(sql_scope, monkeypatch, _preference())
    memory_id = initial["stored_memories"][0]["id"]
    version = snapshot(sql_scope, memory_id)[0].provenance["preference_version"]
    pending, _ = _declare(sql_scope, monkeypatch, _preference("Python", commitment="uncertain"))
    assert pending["pending_candidates_buffered"] == 1 and pending["memories_created"] == 0
    assert snapshot(sql_scope, memory_id)[0].provenance["preference_version"] == version
    items, decisions = read_decisions(sql_scope, {"kind": "global"})
    assert [m.id for m in items] == [memory_id] and decisions == []


@pytest.mark.parametrize("close", ["dismiss", "expire", "purge"])
def test_review_lifecycle_cannot_restore_unresolved_default(sql_scope, monkeypatch, close):
    global_result, _ = _declare(sql_scope, monkeypatch, _preference())
    project, _ = _declare(sql_scope, monkeypatch, _preference("Rust", scope=PROJECT))
    project_id = project["stored_memories"][0]["id"]
    old_version = snapshot(sql_scope, project_id)[0].provenance["preference_version"]
    (result, job_id), _ = withhold(sql_scope, monkeypatch, project_id, applicability=PROJECT)
    assert result["extraction_metadata"]["preference_choices"]["withheld"] == 1
    held, status, priority = snapshot(sql_scope, project_id)
    assert held.provenance["preference_version"] != old_version and status == "disputed" and priority == 20
    assert not held.is_archived and "Rust" in held.content
    candidate_id = review_id(sql_scope, job_id)
    if close == "dismiss":
        assert answer(sql_scope, candidate_id, "dismiss").data.resolved
    else:
        with sql_scope.factory.begin() as session:
            session.get(PendingExtractionCandidate, candidate_id).created_at = datetime.now(UTC) - timedelta(days=40 if close == "purge" else 8)
        run_source_review_maintenance(session_factory=sql_scope.factory, tenant_id=sql_scope.tenant_id)
    items, decisions = read_decisions(sql_scope, PROJECT)
    assert items == [] and len(decisions) == 1
    assert decisions[0].property_key == "examples.language" and decisions[0].applicability.key == "Release"
    assert [m.id for m in _read_preferences(sql_scope, monkeypatch, {"kind": "global"})] == [global_result["stored_memories"][0]["id"]]
    assert [m.id for m in _read_preferences(sql_scope, monkeypatch, {**PROJECT, "key": "Other"})] == [global_result["stored_memories"][0]["id"]]


@pytest.mark.parametrize("setting", ["source_review_profile_row_limit", "source_review_tenant_row_limit"])
def test_cap_rejection_does_not_remove_durable_choice_barrier(sql_scope, monkeypatch, setting):
    initial, _ = _declare(sql_scope, monkeypatch, _preference())
    memory_id = initial["stored_memories"][0]["id"]
    monkeypatch.setattr(get_settings(), setting, 1)
    _declare(sql_scope, monkeypatch, _preference("Rust", commitment="uncertain", property_key="other.preference"))
    (result, _), _ = withhold(sql_scope, monkeypatch, memory_id)
    assert result["memories_created"] == result["pending_candidates_buffered"] == 0
    assert result["extraction_metadata"]["source_reviews_capacity_rejected"] == 1
    assert result["extraction_metadata"]["preference_choices"]["withheld"] == 1
    items, decisions = read_decisions(sql_scope, {"kind": "global"})
    assert items == [] and len(decisions) == 1


@pytest.mark.parametrize("method", ["review", "replacement"])
def test_reaffirmation_keeps_same_memory_authority_and_invalidates_old_jobs(sql_scope, monkeypatch, method):
    initial, _ = _declare(sql_scope, monkeypatch, _preference())
    memory_id = initial["stored_memories"][0]["id"]
    (_, job_id), declaration = withhold(sql_scope, monkeypatch, memory_id)
    held_version = snapshot(sql_scope, memory_id)[0].provenance["preference_version"]
    if method == "review":
        assert answer(sql_scope, review_id(sql_scope, job_id), "keep_current").data.resolved
    else:
        result, _ = _declare(sql_scope, monkeypatch, _preference(commitment="replacement",
            replaces_memory_id=memory_id, replaces_version=held_version))
        assert result["memories_created"] == 0
        assert result["extraction_metadata"]["preference_choices"]["reaffirmed"] == 1
    current, status, priority = snapshot(sql_scope, memory_id)
    assert status == "active" and priority == 20 and not current.is_archived
    assert current.provenance["preference_version"] != held_version
    assert [m.id for m in _read_preferences(sql_scope, monkeypatch, {"kind": "global"})] == [memory_id]
    with sql_scope.factory() as session:
        payload = dict(session.get(ExtractionJob, job_id).payload)
        assert len(session.scalars(select(AuditLog).where(AuditLog.memory_id == uuid.UUID(memory_id))).all()) >= 3
    replay = extraction_tasks.run_extraction_pipeline(payload, session_factory=sql_scope.factory, qdrant_service=AsyncMock())
    assert replay["extraction_metadata"]["preference_choices"]["withheld"] == 0
    assert snapshot(sql_scope, memory_id)[1] == "active"
    fresh, _ = _declare(sql_scope, monkeypatch, declaration)
    assert fresh["extraction_metadata"]["preference_choices"]["withheld"] == 0


def test_version_bound_new_value_resolves_without_touching_other_properties(sql_scope, monkeypatch):
    first, _ = _declare(sql_scope, monkeypatch, _preference(scope=PROJECT))
    other, _ = _declare(sql_scope, monkeypatch, _preference("Brief", property_key="answers.style"))
    memory_id = first["stored_memories"][0]["id"]
    withhold(sql_scope, monkeypatch, memory_id, applicability=PROJECT)
    # Ordinary statements and same-value duplicates cannot clear an unresolved decision.
    pending, _ = _declare(sql_scope, monkeypatch, _preference(scope=PROJECT))
    assert pending["memories_created"] == 0 and snapshot(sql_scope, memory_id)[1] == "disputed"
    rows, decisions = read_decisions(sql_scope, PROJECT, properties=["examples.language", "answers.style"], categories=["preference", "fact"])
    assert [m.id for m in rows] == [other["stored_memories"][0]["id"]] and len(decisions) == 1
    version = snapshot(sql_scope, memory_id)[0].provenance["preference_version"]
    result, _ = _declare(sql_scope, monkeypatch, _preference("Python", scope=PROJECT,
        commitment="replacement", replaces_memory_id=memory_id, replaces_version=version))
    assert result["memories_created"] == 1
    rows, decisions = read_decisions(sql_scope, PROJECT)
    assert [m.id for m in rows] == [result["stored_memories"][0]["id"]] and decisions == []


@pytest.mark.parametrize("kind", ["ledger_authority", "provenance_authority", "wrong_scope", "stale", "archived", "expired", "foreign"])
def test_invalid_or_insufficient_authority_cannot_withhold(sql_scope, monkeypatch, kind):
    initial, _ = _declare(sql_scope, monkeypatch, _preference())
    memory_id = initial["stored_memories"][0]["id"]
    declaration = _preference("Python", commitment="uncertain", choice_state="current_choice_unresolved",
        replaces_memory_id=memory_id, replaces_version=snapshot(sql_scope, memory_id)[0].provenance["preference_version"])
    with sql_scope.factory.begin() as session:
        memory = session.get(Memory, uuid.UUID(memory_id))
        if kind == "ledger_authority":
            session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == memory.id)).one().authority_priority = 100
        if kind == "provenance_authority":
            prov = dict(memory.metadata_json["provenance"])
            prov["authority_rules"] = {"default_priority": 20, "categories": {"preference": 100}}
            memory.metadata_json = {**memory.metadata_json, "provenance": prov}
        if kind == "expired":
            memory.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if kind in {"provenance_authority", "expired"}:
        declaration["replaces_version"] = snapshot(sql_scope, memory_id)[0].provenance["preference_version"]
    if kind == "archived":
        manage(sql_scope, memory_id)
        declaration["replaces_version"] = snapshot(sql_scope, memory_id)[0].provenance["preference_version"]
    if kind == "wrong_scope":
        declaration["applicability"] = PROJECT
    if kind == "stale":
        declaration["replaces_version"] = "f" * 64
    if kind == "foreign":
        declaration["replaces_memory_id"] = str(uuid.uuid4())
    result, _ = _declare(sql_scope, monkeypatch, declaration)
    assert result["memories_created"] == 0
    assert result["extraction_metadata"]["preference_choices"]["withheld"] == 0
    assert result["pending_candidates_buffered"] == 0
    assert sum(result["extraction_metadata"]["preference_choices"]["rejected"].values()) == 1
    assert CHOICE_STATE_MARKER not in snapshot(sql_scope, memory_id)[0].metadata


def test_stale_review_cannot_clear_a_newer_unresolved_generation(sql_scope, monkeypatch):
    initial, _ = _declare(sql_scope, monkeypatch, _preference())
    memory_id = initial["stored_memories"][0]["id"]
    (_, old_job), _ = withhold(sql_scope, monkeypatch, memory_id)
    old_review = review_id(sql_scope, old_job)
    (_, new_job), _ = withhold(sql_scope, monkeypatch, memory_id)
    with pytest.raises(APIError) as error:
        answer(sql_scope, old_review, "keep_current")
    assert error.value.status_code == 409
    assert read_decisions(sql_scope, {"kind": "global"})[0] == []
    assert answer(sql_scope, review_id(sql_scope, new_job), "keep_current").data.resolved
    assert snapshot(sql_scope, memory_id)[1] == "active"


def test_concurrent_withholding_uses_one_version_transition(sql_scope, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from api.services.conflict_resolver import ConflictResolver
    from api.services.preference_claim_service import declared_preferences

    initial, _ = _declare(sql_scope, monkeypatch, _preference())
    memory_id = initial["stored_memories"][0]["id"]
    declaration = _preference("Python", commitment="uncertain", choice_state="current_choice_unresolved",
        replaces_memory_id=memory_id, replaces_version=snapshot(sql_scope, memory_id)[0].provenance["preference_version"])
    messages = [{"role": "user", "content": declaration["quote"]}]
    with sql_scope.factory() as session:
        old = session.get(Memory, uuid.UUID(memory_id))
        user_id, conversation_id = str(old.user_id), str(old.source_conversation_id)
    barrier = Barrier(2)

    def hold():
        with sql_scope.factory.begin() as session:
            resolver = ConflictResolver(session=session, qdrant_service=SimpleNamespace(),
                embedder=lambda _: pytest.fail("withholding must not call an embedding provider"),
                llm_service=SimpleNamespace(), source_messages=messages)
            barrier.wait(timeout=10)
            result = resolver.check_and_store(declared_preferences([declaration], messages), user_id=user_id,
                tenant_id=str(sql_scope.tenant_id), proxy_user_id=str(sql_scope.proxy_id),
                source_conversation_id=conversation_id, auto_commit=False)
            assert result == []
            return resolver.last_preference_choices_withheld
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: hold(), range(2)))
    assert sorted(outcomes) == [0, 1]
    assert snapshot(sql_scope, memory_id)[1] == "disputed"


def test_review_ledger_lock_contention_is_retryable_not_deadlocking(sql_scope, monkeypatch):
    initial, _ = _declare(sql_scope, monkeypatch, _preference())
    memory_id = initial["stored_memories"][0]["id"]
    (_, job_id), _ = withhold(sql_scope, monkeypatch, memory_id)
    candidate_id = review_id(sql_scope, job_id)
    with sql_scope.factory.begin() as session:
        session.execute(select(MemoryClaim).where(MemoryClaim.active_memory_id == uuid.UUID(memory_id)).with_for_update()).one()
        with pytest.raises(APIError) as error:
            answer(sql_scope, candidate_id, "keep_current")
        assert error.value.error == "source_review_target_busy_retry"
    assert answer(sql_scope, candidate_id, "keep_current").data.resolved


def test_existing_foreign_memory_cannot_be_targeted(sql_scope, monkeypatch):
    foreign_id = uuid.uuid4()
    foreign_external = f"choice-foreign-{foreign_id}"
    with sql_scope.factory.begin() as session:
        session.add(ProxyUser(id=foreign_id, tenant_id=sql_scope.tenant_id,
            external_user_id=foreign_external,
            external_user_id_hash=ProxyUserService.hash_external_user_id(str(sql_scope.tenant_id), foreign_external),
            metadata_json={}))
    sql_scope.proxy_ids.append(foreign_id)
    foreign = SimpleNamespace(factory=sql_scope.factory, tenant_id=sql_scope.tenant_id,
        proxy_id=foreign_id, external_id=foreign_external)
    initial, _ = _declare(foreign, monkeypatch, _preference())
    memory_id = initial["stored_memories"][0]["id"]
    before = snapshot(foreign, memory_id)
    result, _ = _declare(sql_scope, monkeypatch, _preference("Python", commitment="uncertain",
        choice_state="current_choice_unresolved", replaces_memory_id=memory_id,
        replaces_version=before[0].provenance["preference_version"]))
    assert result["extraction_metadata"]["preference_choices"]["rejected"] == {"preference_target_stale": 1}
    assert snapshot(foreign, memory_id)[0].provenance == before[0].provenance
    assert snapshot(foreign, memory_id)[1] == "active"


def test_choice_and_audit_roll_back_when_review_persistence_fails(sql_scope, monkeypatch):
    from api.tasks.extraction_tasks import ExtractionPipelineError

    initial, _ = _declare(sql_scope, monkeypatch, _preference())
    memory_id = initial["stored_memories"][0]["id"]
    before = snapshot(sql_scope, memory_id)
    with sql_scope.factory() as session:
        audits = len(session.scalars(select(AuditLog).where(AuditLog.memory_id == uuid.UUID(memory_id))).all())
    original = extraction_tasks._persist_pending_extraction_candidates
    def fail_after_hold(*args, **kwargs):
        if kwargs["candidates"]:
            raise RuntimeError("controlled persistence failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(extraction_tasks, "_persist_pending_extraction_candidates", fail_after_hold)
    with pytest.raises(ExtractionPipelineError):
        withhold(sql_scope, monkeypatch, memory_id)
    after = snapshot(sql_scope, memory_id)
    assert after[0].provenance == before[0].provenance and after[1] == "active"
    assert CHOICE_STATE_MARKER not in after[0].metadata
    with sql_scope.factory() as session:
        assert len(session.scalars(select(AuditLog).where(AuditLog.memory_id == uuid.UUID(memory_id))).all()) == audits


@pytest.mark.parametrize("action", ["restate", "keep_current"])
def test_tentative_review_cannot_clear_choice_barrier(sql_scope, monkeypatch, action):
    initial, _ = _declare(sql_scope, monkeypatch, _preference())
    memory_id = initial["stored_memories"][0]["id"]
    withhold(sql_scope, monkeypatch, memory_id)
    _, job_id = _declare(sql_scope, monkeypatch, _preference("Rust", commitment="uncertain"))
    answer(sql_scope, review_id(sql_scope, job_id), action)
    assert snapshot(sql_scope, memory_id)[1] == "disputed"
    assert read_decisions(sql_scope, {"kind": "global"})[0] == []


@pytest.mark.parametrize("kind", ["expired", "archived", "future", "blocked", "disabled"])
def test_review_cannot_restore_an_ineligible_preference(sql_scope, monkeypatch, kind):
    initial, _ = _declare(sql_scope, monkeypatch, _preference())
    memory_id = initial["stored_memories"][0]["id"]
    (_, job_id), _ = withhold(sql_scope, monkeypatch, memory_id)
    with sql_scope.factory.begin() as session:
        memory = session.get(Memory, uuid.UUID(memory_id))
        if kind == "expired":
            memory.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif kind == "future":
            memory.effective_from = datetime.now(UTC) + timedelta(days=1)
        elif kind == "blocked":
            session.get(ProxyUser, sql_scope.proxy_id).is_blocked = True
    if kind == "archived":
        manage(sql_scope, memory_id)
    if kind == "disabled":
        monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", False)
    with pytest.raises(APIError) as error:
        answer(sql_scope, review_id(sql_scope, job_id), "keep_current")
    assert error.value.status_code in {404, 409}
    assert snapshot(sql_scope, memory_id)[1] != "active"


@pytest.mark.parametrize("kind", ["archived", "expired"])
@pytest.mark.parametrize("value", ["C++", "Python"])
def test_invalid_held_winner_blocks_fallback_until_explicit_renewal(sql_scope, monkeypatch, kind, value):
    _declare(sql_scope, monkeypatch, _preference("Rust"))
    initial, _ = _declare(sql_scope, monkeypatch, _preference(scope=PROJECT))
    memory_id = initial["stored_memories"][0]["id"]
    withhold(sql_scope, monkeypatch, memory_id, applicability=PROJECT)
    if kind == "archived":
        manage(sql_scope, memory_id)
    else:
        with sql_scope.factory.begin() as session:
            session.get(Memory, uuid.UUID(memory_id)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert read_decisions(sql_scope, PROJECT)[0] == []
    result, _ = _declare(sql_scope, monkeypatch, _preference(value, scope=PROJECT,
        commitment="replacement", replaces_memory_id=memory_id,
        replaces_version=snapshot(sql_scope, memory_id)[0].provenance["preference_version"]))
    assert result["memories_created"] == 1
    successor = result["stored_memories"][0]["id"]
    assert successor != memory_id
    items, decisions = read_decisions(sql_scope, PROJECT)
    assert [item.id for item in items] == [successor] and decisions == []
    with sql_scope.factory() as session:
        assert session.get(Memory, uuid.UUID(memory_id)).is_archived
