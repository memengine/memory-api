"""Real SQL lifecycle checks; model responses and vector nominations are controlled.

These are not model-quality or deployed HTTP tests. They exercise the production
parser, worker, resolver, tenant selection endpoint, claim ledger and retriever.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from api.db.database import get_sync_database_url
from api.db.models import (
    ClarificationQueue,
    ClarificationQueueStatus,
    CrossUserConflict,
    ExtractionJob,
    Memory,
    MemoryClaim,
    MemoryClaimRevision,
    MemoryVersion,
    PendingExtractionCandidate,
    PlanTier,
    ProxyUser,
    Tenant,
    User,
    VectorSyncOutbox,
)
from api.errors import APIError
from api.routers.memories import answer_memory_clarification
from api.schemas.requests import MemoryClarificationAnswerRequest
from api.services.conflict_resolver import ConflictResolver
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID, EmbeddingResult
from api.services.extraction_service import ExtractionService
from api.services.llm_service import LLMResponse
from api.services.memory_service import MemoryService
from api.services.proxy_user_service import ProxyUserService
from api.services.retriever import RetrieverService
from api.tasks import extraction_tasks


def _embedding(_text, **_kwargs):
    return EmbeddingResult([0.01] * 1536, DEFAULT_ACTIVE_MODEL_ID, 1536, "memories")


class _Embedding:
    def __init__(self, **_kwargs):
        pass

    embed_sync = staticmethod(_embedding)
    embed = AsyncMock(side_effect=_embedding)


class _Model:
    def __init__(self, content, decision, before_decision=None, *, claim_state="asserted"):
        self.content = content
        self.decision = decision
        self.before_decision = before_decision
        self.source_prompts = []
        self.claim_state = claim_state

    async def complete(self, **kwargs):
        assert kwargs["response_format"].name == "memory_extraction_v2"
        return self._response(
            {
                "memories": [
                    {
                        "content": self.content,
                        "category": "preference",
                        "importance_score": 6,
                        "confidence": 0.95,
                        "claim_state": self.claim_state,
                        "reasoning": "Controlled model response for SQL contract test.",
                        "evidence_turns": [0],
                        "evidence_relation": "direct_user_statement",
                        "evidence_spans": [{"turn_index": 0, "quote": self.content}],
                        "proposal_turn": None,
                    }
                ],
                "nothing_to_extract": False,
            }
        )

    def complete_sync(self, **kwargs):
        assert kwargs["response_format"].name == "memory_source_relation_v1"
        self.source_prompts.append(json.loads(kwargs["user_message"]))
        if self.before_decision:
            self.before_decision()
        return self._response(self.decision)

    @staticmethod
    def _response(payload):
        return LLMResponse(
            json.dumps(payload), "controlled", "sql-test", 10, 10, 20, 0, True
        )


def _decision(target=None, *, state="committed_current", relation="novel", option=None):
    return {
        "selected_memory_id": str(target) if target else None,
        "relation": relation,
        "commitment_status": state,
        "requires_user_choice": state == "tentative" and target is not None,
        "clarification_option_memory": option,
        "merged_memory": None,
        "reasoning": "Controlled full-source decision.",
    }


@pytest.fixture
def sql_scope(monkeypatch):
    engine = create_engine(get_sync_database_url())
    factory = sessionmaker(engine, expire_on_commit=False)
    tenant_id, proxy_id = uuid.uuid4(), uuid.uuid4()
    external_id = f"source-sql-{proxy_id}"
    with factory.begin() as session:
        session.add(
            Tenant(
                id=tenant_id,
                company_name="Source SQL test",
                region_id="IN1",
                plan_tier=PlanTier.starter,
                metadata_json={},
            )
        )
        session.flush()
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
    monkeypatch.setattr(extraction_tasks, "EmbeddingService", _Embedding)
    monkeypatch.setattr(
        "api.services.conflict_resolution_service.EmbeddingService", _Embedding
    )
    # Isolate side-effect delivery, not SQL persistence or governance decisions.
    monkeypatch.setattr(
        extraction_tasks, "_invalidate_proxy_user_cache", lambda _: None
    )
    scope = SimpleNamespace(
        factory=factory,
        tenant_id=tenant_id,
        proxy_id=proxy_id,
        external_id=external_id,
        proxy_ids=[proxy_id],
        tenant_ids=[tenant_id],
    )
    try:
        yield scope
    finally:
        with factory.begin() as session:
            session.execute(delete(Tenant).where(Tenant.id.in_(scope.tenant_ids)))
            session.execute(
                delete(User).where(
                    User.external_id.in_([f"proxy::{item}" for item in scope.proxy_ids])
                )
            )
        for item in scope.proxy_ids:
            RetrieverService.invalidate_local_user_cache(str(item))
        engine.dispose()


def _run(scope, monkeypatch, content, decision, *, points=None, before_decision=None,
         claim_state="asserted", extracted_content=None):
    model = _Model(extracted_content or content, decision, before_decision, claim_state=claim_state)
    cache = AsyncMock()
    extractor = ExtractionService(
        llm_service=model,
        cache_service=cache,
        proposal_confirmation_enabled=False,
        importance_shadow_enabled=False,
        app_env="test",
    )

    class Search:
        def search_memories(self, **kwargs):
            if points is not None:
                return points
            with scope.factory() as session:
                rows = session.scalars(
                    select(Memory).where(
                        Memory.proxy_user_id == scope.proxy_id,
                        Memory.is_archived.is_(False),
                    )
                ).all()
                # Deliberately below the old similarity conflict trigger.
                return [
                    SimpleNamespace(
                        id=str(row.id), score=0.7, payload={"memory_id": str(row.id)}
                    )
                    for row in rows
                ]

    def resolver_factory(**kwargs):
        return ConflictResolver(**kwargs, llm_service=model)

    monkeypatch.setattr(extraction_tasks, "ConflictResolver", resolver_factory)
    job_id = uuid.uuid4()
    payload = {
        "job_id": str(job_id),
        "tenant_id": str(scope.tenant_id),
        "proxy_user_id": str(scope.proxy_id),
        "external_user_id": scope.external_id,
        "external_conversation_id": "source-sql-chat",
        "messages": [{"role": "user", "content": content, "turn_id": str(job_id)}],
    }
    with scope.factory.begin() as session:
        session.add(
            ExtractionJob(
                id=job_id,
                tenant_id=scope.tenant_id,
                proxy_user_id=scope.proxy_id,
                external_user_id=scope.external_id,
                payload=payload,
            )
        )
    result = extraction_tasks.run_extraction_pipeline(
        payload,
        session_factory=scope.factory,
        extractor=extractor,
        qdrant_service=Search(),
    )
    return result, model, job_id


@pytest.mark.parametrize(
    "decision", [_decision(state="tentative", relation="ambiguous"), {}]
)
def test_pending_decision_survives_worker_processing_without_active_memory(
    sql_scope, monkeypatch, decision
):
    content = "Python might be my default, but I have not decided."
    result, model, job_id = _run(sql_scope, monkeypatch, content, decision)
    assert result["memories_created"] == 0
    assert result["pending_candidates_buffered"] == 1
    assert result["tokens_used"] == 40
    with sql_scope.factory() as session:
        pending = session.scalars(
            select(PendingExtractionCandidate).where(
                PendingExtractionCandidate.proxy_user_id == sql_scope.proxy_id
            )
        ).one()
        assert pending.extraction_job_id == job_id
        assert pending.status == "pending"
        assert pending.candidate_reason == "source_decision_pending"
        assert pending.metadata_json["extraction_evidence"]["source_spans"][0][
            "turn_id"
        ] == str(job_id)
        assert not session.scalars(
            select(Memory).where(Memory.proxy_user_id == sql_scope.proxy_id)
        ).all()
    assert model.source_prompts[0]["supporting_user_turns"][0]["content"] == content


@pytest.mark.parametrize("state", ["tentative", "committed_current"])
@pytest.mark.parametrize("update_text", [
    "Python could be my default, but I have not decided whether to replace C++.",
    "Python might be my default, but I have not decided between C++ and Python.",
    "Ab Python bhi default rakhne ka soch raha hoon, lekin abhi decide nahi kiya ki C++ ya Python mein se kaunsa current rahe.",
    "मेरी डिफ़ॉल्ट भाषा Python हो सकती है, लेकिन C++ और Python में अभी निर्णय नहीं लिया है।",
])
def test_chat_choice_persists_provenance_ledger_and_retrieves_only_winner(
    sql_scope, monkeypatch, update_text, state
):
    initial, _, _ = _run(
        sql_scope, monkeypatch, "My default programming language is C++.", _decision()
    )
    old_id = uuid.UUID(initial["stored_memories"][0]["id"])
    result, model, _ = _run(
        sql_scope,
        monkeypatch,
        update_text,
        _decision(
            old_id,
            state=state,
            relation="supersedes" if state == "committed_current" else "ambiguous",
            option={
                "attribute": "default programming language",
                "value": "Python",
                "category": "preference",
            },
        ),
        claim_state="uncertain_change",
    )
    assert result["clarification_queued"] is True
    assert model.source_prompts[0]["admission_policy"]["requires_user_selection"] is True
    new_id = uuid.UUID(result["stored_memories"][0]["id"])
    with sql_scope.factory() as session:
        assert not session.get(Memory, old_id).is_archived
        pending_memory = session.get(Memory, new_id)
        assert pending_memory.is_archived
        value_span = pending_memory.metadata_json["provenance"]["extraction_evidence"]["clarification_value_span"]
        assert value_span["start_char"] == update_text.index("Python")
        assert update_text[value_span["start_char"]:value_span["end_char"]] == "Python"
        clarification = session.scalars(
            select(ClarificationQueue).where(
                ClarificationQueue.proxy_user_id == sql_scope.proxy_id
            )
        ).one()
        clarification_id = clarification.id

    async def resolve_and_read():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cache = AsyncMock()
        cache.client.get.return_value = None
        cache.get_retrieval_results.return_value = None
        cache.breaker = None
        try:
            async with sessions() as session:
                proxy_service = ProxyUserService(session=session, cache_service=cache)
                request = Request(
                    {"type": "http", "method": "POST", "path": "/", "headers": []}
                )
                request.state.request_id = "source-sql-test"
                with pytest.raises(APIError) as denied:
                    await answer_memory_clarification(
                        request,
                        str(clarification_id),
                        MemoryClarificationAnswerRequest(
                            external_user_id="other-user", answer="B"
                        ),
                        proxy_service,
                        session,
                        str(sql_scope.tenant_id),
                    )
                assert denied.value.status_code == 404
                await session.rollback()
                answer = await answer_memory_clarification(
                    request,
                    str(clarification_id),
                    MemoryClarificationAnswerRequest(
                        external_user_id=sql_scope.external_id, answer="B"
                    ),
                    proxy_service,
                    session,
                    str(sql_scope.tenant_id),
                )
                assert answer.data.resolved
            async with sessions() as session:
                retriever = RetrieverService(
                    session=session,
                    qdrant_service=SimpleNamespace(),
                    cache_service=cache,
                    quota_manager=SimpleNamespace(),
                    proxy_user_service=ProxyUserService(
                        session=session, cache_service=cache
                    ),
                    embedding_service=_Embedding(),
                )
                monkeypatch.setattr(retriever, "_queue_access_update", lambda _: None)
                results = await retriever.retrieve(
                    "Which language is my default?",
                    external_user_id=sql_scope.external_id,
                    proxy_user_id=str(sql_scope.proxy_id),
                    tenant_id=str(sql_scope.tenant_id),
                    quota_mode="full",
                )
                assert [row.id for row in results] == [str(new_id)]
                assert "Python" in results[0].content
                provenance = results[0].provenance
                assert provenance["external_conversation_id"] == "source-sql-chat"
                assert provenance["extraction_evidence"]["clarification_value_span"][
                    "quote_sha256"
                ]
                memories, _, total = await MemoryService(
                    session=session,
                    cache_service=cache,
                    qdrant_service=SimpleNamespace(),
                    quota_manager=SimpleNamespace(),
                    embedding_service=_Embedding(),
                ).list_memories(
                    requested_user_id=None,
                    authenticated_user_id=None,
                    tenant_id=str(sql_scope.tenant_id),
                    external_user_id=sql_scope.external_id,
                    cursor=None,
                    limit=10,
                    categories=[],
                    agent_id=None,
                )
                assert total == 2
                selected = next(row for row in memories if row.id == new_id)
                assert selected.metadata_json["provenance"] == provenance
                await asyncio.sleep(
                    0
                )  # Finish the in-memory cache adapter's scheduled write.
        finally:
            await engine.dispose()

    asyncio.run(resolve_and_read())
    with sql_scope.factory() as session:
        assert session.get(Memory, old_id).is_archived
        assert not session.get(Memory, new_id).is_archived
        assert (
            session.get(ClarificationQueue, clarification_id).status
            == ClarificationQueueStatus.resolved
        )
        conflict = session.get(CrossUserConflict, clarification.conflict_id)
        assert conflict.resolution == "B"
        claims = session.scalars(
            select(MemoryClaim).where(MemoryClaim.proxy_user_id == sql_scope.proxy_id)
        ).all()
        assert claims and {
            row.active_memory_id for row in claims if row.active_memory_id
        } == {new_id}
        for claim in claims:
            if claim.active_memory_id:
                revision = session.get(MemoryClaimRevision, claim.winning_revision_id)
                assert revision.memory_id == new_id and revision.status == "activated"
        assert session.scalars(
            select(MemoryVersion).where(MemoryVersion.memory_id == old_id)
        ).all()
        assert session.scalars(
            select(VectorSyncOutbox).where(VectorSyncOutbox.memory_id == new_id)
        ).all()


@pytest.mark.parametrize("relation", ["supersedes", "mergeable", "coexists", "novel"])
def test_commitment_disagreement_is_persisted_pending_without_changing_current(sql_scope, monkeypatch, relation):
    initial, _, _ = _run(sql_scope, monkeypatch, "My default programming language is C++.", _decision())
    old_id = uuid.UUID(initial["stored_memories"][0]["id"])
    prefix = "My default language for every programming example is Python."
    full_turn = prefix + " This conflicts with my earlier C++ default, and I have not decided which should remain current."
    decision = _decision(None if relation == "novel" else old_id, relation=relation)
    if relation == "mergeable":
        decision["merged_memory"] = {
            "content": prefix, "category": "preference", "importance_score": 7,
            "confidence": 0.99, "expiry": "permanent", "reasoning": "Controlled committed merge.",
        }
    result, model, job_id = _run(
        sql_scope, monkeypatch, full_turn, decision, claim_state="uncertain_change",
        extracted_content=prefix, points=[] if relation == "novel" else None,
    )
    assert result["memories_created"] == 0 and result["pending_candidates_buffered"] == 1
    assert not result["stored_memories"] and result["clarification_queued"] is False
    assert len(model.source_prompts) == 1
    assert model.source_prompts[0]["supporting_user_turns"][0]["content"] == full_turn
    with sql_scope.factory() as session:
        old = session.get(Memory, old_id)
        assert not old.is_archived and old.content.endswith("C++.")
        assert [row.id for row in session.scalars(select(Memory).where(
            Memory.proxy_user_id == sql_scope.proxy_id,
        ))] == [old_id]
        pending = session.scalars(select(PendingExtractionCandidate).where(
            PendingExtractionCandidate.proxy_user_id == sql_scope.proxy_id,
            PendingExtractionCandidate.extraction_job_id == job_id,
        )).one()
        assert pending.status == "pending" and pending.candidate_reason == "source_decision_pending"
        evidence = pending.metadata_json["extraction_evidence"]
        assert evidence["claim_state"] == "uncertain_change"
        assert "source_commitment_disagreement" in evidence["source_decision"]["reason_codes"]
        assert evidence["source_spans"][0]["turn_id"] == str(job_id)
        claims = session.scalars(select(MemoryClaim).where(MemoryClaim.proxy_user_id == sql_scope.proxy_id)).all()
        assert claims and {row.active_memory_id for row in claims} == {old_id}
        revisions = session.scalars(select(MemoryClaimRevision).where(MemoryClaimRevision.memory_id == old_id)).all()
        assert revisions and all(row.status == "activated" for row in revisions)
        assert not session.scalars(select(ClarificationQueue).where(
            ClarificationQueue.proxy_user_id == sql_scope.proxy_id,
        )).all()


def test_explicit_correction_replaces_current_memory_and_preserves_source(
    sql_scope, monkeypatch
):
    initial, _, _ = _run(
        sql_scope, monkeypatch, "My default programming language is C++.", _decision()
    )
    old_id = uuid.UUID(initial["stored_memories"][0]["id"])
    result, _, job_id = _run(
        sql_scope,
        monkeypatch,
        "Correction: my default is Python instead of C++.",
        _decision(old_id, relation="supersedes"),
        claim_state="correction",
    )
    assert result["conflicts_resolved"] == 1
    new_id = uuid.UUID(result["stored_memories"][0]["id"])
    with sql_scope.factory() as session:
        assert session.get(Memory, old_id).is_archived
        current = session.get(Memory, new_id)
        assert not current.is_archived
        assert current.previous_version_id == old_id
        assert current.metadata_json["provenance"]["extraction_evidence"][
            "source_spans"
        ][0]["turn_id"] == str(job_id)
        winners = session.scalars(
            select(MemoryClaim).where(
                MemoryClaim.proxy_user_id == sql_scope.proxy_id,
                MemoryClaim.active_memory_id.is_not(None),
            )
        ).all()
        assert winners and {row.active_memory_id for row in winners} == {new_id}


def test_foreign_vector_target_cannot_be_selected_or_overwritten(
    sql_scope, monkeypatch
):
    initial, _, _ = _run(
        sql_scope, monkeypatch, "My default programming language is C++.", _decision()
    )
    foreign_id = uuid.UUID(initial["stored_memories"][0]["id"])
    other_id = uuid.uuid4()
    other_external = f"other-source-sql-{other_id}"
    with sql_scope.factory.begin() as session:
        session.add(
            ProxyUser(
                id=other_id,
                tenant_id=sql_scope.tenant_id,
                external_user_id=other_external,
                external_user_id_hash=ProxyUserService.hash_external_user_id(
                    str(sql_scope.tenant_id), other_external
                ),
                metadata_json={},
            )
        )
    sql_scope.proxy_ids.append(other_id)
    other_scope = SimpleNamespace(
        **{**vars(sql_scope), "proxy_id": other_id, "external_id": other_external}
    )
    result, model, _ = _run(
        other_scope,
        monkeypatch,
        "Correction: my default is Python instead of C++.",
        _decision(foreign_id, relation="supersedes"),
        points=[
            SimpleNamespace(
                id=str(foreign_id), score=0.99, payload={"memory_id": str(foreign_id)}
            )
        ],
    )
    assert model.source_prompts[0]["existing_candidates"] == []
    assert (
        result["memories_created"] == 0 and result["pending_candidates_buffered"] == 1
    )
    with sql_scope.factory() as session:
        old = session.get(Memory, foreign_id)
        assert old.content.endswith("C++.") and not old.is_archived
        assert not session.scalars(
            select(Memory).where(Memory.proxy_user_id == other_id)
        ).all()


@pytest.mark.parametrize("review_required", [False, True])
def test_source_decision_rechecks_target_changed_by_concurrent_connection(
    sql_scope, monkeypatch, review_required
):
    initial, _, _ = _run(
        sql_scope, monkeypatch, "My default programming language is C++.", _decision()
    )
    target_id = uuid.UUID(initial["stored_memories"][0]["id"])

    def concurrent_change():
        with sql_scope.factory.begin() as other_session:
            other_session.execute(
                update(Memory)
                .where(Memory.id == target_id)
                .values(content="My default programming language is Rust.")
            )

    result, _, _ = _run(
        sql_scope,
        monkeypatch,
        "Correction: use Python instead of C++.",
        _decision(
            target_id,
            relation="supersedes",
            option={
                "attribute": "default programming language",
                "value": "Python",
                "category": "preference",
            } if review_required else None,
        ),
        before_decision=concurrent_change,
        claim_state="uncertain_change" if review_required else "correction",
    )
    assert result["memories_created"] == 0
    assert result["pending_candidates_buffered"] == 1
    with sql_scope.factory() as session:
        target = session.get(Memory, target_id)
        assert target.content.endswith("Rust.") and not target.is_archived
