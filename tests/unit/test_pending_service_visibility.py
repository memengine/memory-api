"""Completed service extraction must not hide its unresolved correction."""
import json
import uuid
from types import SimpleNamespace as Row
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from api.schemas.responses import MemoryVisibility
from api.services.context_builder import ContextBuilder
from api.services.retriever import RetrieverService
from tests.unit.test_retrieval_scope_dedup import result


def service(pending=True, blocked=()):
    instance = object.__new__(RetrieverService)
    instance.session = Row(scalar=AsyncMock(return_value=pending),
        scalars=AsyncMock(return_value=Row(all=lambda: list(blocked))))
    instance.last_memory_visibility = None
    instance.last_cache_hit = True
    instance.last_is_degraded = False
    instance.last_preference_decisions = ["old choice"]
    instance.last_preference_scope_request = "old scope request"
    return instance


def args():
    return dict(owner=str(uuid.uuid4()), tenant_id=str(uuid.uuid4()), categories=[])


@pytest.mark.asyncio
async def test_pending_correction_withholds_selected_scope_and_keeps_unaffected_context():
    affected, other_scope, other_category = [Row(id=str(uuid.uuid4())) for _ in range(3)]
    instance = service(blocked=[uuid.UUID(affected.id)])
    observed = await instance._withhold_pending_source_corrections(
        [affected, other_scope, other_category], **args())
    assert observed == [other_scope, other_category]
    assert instance.last_memory_visibility == MemoryVisibility(state="unavailable", scope="affected_context")
    assert instance.last_is_degraded and not instance.last_cache_hit
    assert instance.last_preference_decisions == []
    assert instance.last_preference_scope_request is None


@pytest.mark.asyncio
async def test_notice_survives_empty_nominations_without_promoting_candidate():
    instance = service()
    assert await instance._withhold_pending_source_corrections([], **args()) == []
    assert instance.last_memory_visibility.state == "unavailable"
    instance.session.scalars.assert_not_called()


@pytest.mark.asyncio
async def test_no_pending_correction_preserves_existing_visibility_and_context():
    instance = service(pending=False)
    instance.last_memory_visibility = MemoryVisibility(state="processing")
    rows = [Row(id=str(uuid.uuid4()))]
    assert await instance._withhold_pending_source_corrections(rows, **args()) == rows
    assert instance.last_memory_visibility.state == "processing"
    instance.session.scalars.assert_not_called()


@pytest.mark.asyncio
async def test_existing_broad_hold_is_not_reduced_to_partial():
    instance = service()
    instance.last_memory_visibility = MemoryVisibility(state="unavailable")
    await instance._withhold_pending_source_corrections([], **args())
    assert instance.last_memory_visibility.scope == "user_learned_context"


@pytest.mark.asyncio
async def test_owner_bound_status_reason_and_exact_scope_queries():
    instance = service()
    binding = args()
    binding["categories"] = ["FACT"]
    await instance._withhold_pending_source_corrections([Row(id=str(uuid.uuid4()))], **binding)
    queries = [instance.session.scalar.call_args.args[0], instance.session.scalars.call_args.args[0]]
    sql, params = [], []
    for query in queries:
        compiled = query.compile(dialect=postgresql.dialect())
        sql.append(str(compiled))
        params.append(compiled.params)
    assert "pending_extraction_candidates.status" in sql[0]
    assert "memory_source_events.writer_id IS NOT NULL" in sql[0]
    assert "pending_extraction_candidates.tenant_id" in sql[0]
    assert "memory_source_events.tenant_id" in sql[0]
    assert "pending_extraction_candidates.proxy_user_id" in sql[0]
    assert "memory_source_events.proxy_user_id" in sql[0]
    assert "pending" in params[0].values()
    assert "source_decision_pending" in params[0].values()
    assert ["source_correction_unresolved"] in params[0].values()
    assert ["fact"] in params[0].values()
    assert uuid.UUID(binding["owner"]) in params[0].values()
    assert uuid.UUID(binding["tenant_id"]) in params[0].values()
    assert "pending_extraction_candidates.category = memories.category" in sql[1]
    assert "memory_source_events.scope = memory_source_events_1.scope" in sql[1]
    assert "JOIN memory_source_events AS memory_source_events_1" in sql[1]
    assert "extraction_jobs" not in " ".join(sql)  # Completion/deletion is not semantic resolution.


@pytest.mark.asyncio
async def test_final_projection_checks_pending_against_delivered_winner_not_stale_nominee():
    instance = service()
    old, winner = Row(id=str(uuid.uuid4())), Row(id=str(uuid.uuid4()))
    instance._project_source_claims = AsyncMock(return_value=[winner])
    instance._withhold_pending_source_corrections = AsyncMock(return_value=[])
    assert await instance._project_current_sources([old], **args()) == []
    assert instance._withhold_pending_source_corrections.call_args.args == ([winner],)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_routed_history", [False, True])
async def test_both_final_snapshot_paths_guard_a_completed_service_correction(monkeypatch, has_routed_history):
    from contextlib import asynccontextmanager
    from api.services import retriever
    from api.services import preference_confirmation_service

    nominee = Row(id=str(uuid.uuid4()), category="fact", previous_version_id=None)
    instance = service(blocked=[uuid.UUID(nominee.id)])
    snapshot = instance.session
    snapshot.run_sync = AsyncMock(return_value=set())
    original = Row(bind=Row(execution_options=lambda **_: "bound engine"))
    instance.session = original
    instance._finish_read_transaction = AsyncMock()
    instance._runtime_visibility_row = AsyncMock(return_value=(False, False, has_routed_history))
    instance._source_index_pending = AsyncMock(return_value=False)
    instance._preference_discovery_active = False
    instance._hydrate_current_results = AsyncMock(return_value=[nominee])
    instance._project_source_claims = AsyncMock(return_value=[nominee])

    @asynccontextmanager
    async def begin():
        yield snapshot

    monkeypatch.setattr(retriever, "async_sessionmaker", lambda *a, **k: Row(begin=begin))
    monkeypatch.setattr(preference_confirmation_service, "pending_fingerprints", lambda *a, **k: set())
    binding = args()
    owner = binding.pop("owner")
    binding.update(query="current fact", preference_context=None, limit=10,
        agent_id=None, time_filter_days=None, external_user_id="synthetic", user_id=None)
    assert await instance._final_current_read([nominee], owner=owner, **binding) == []
    assert instance.last_memory_visibility.state == "unavailable"
    assert instance.session is original


@pytest.mark.parametrize("format", ["json", "xml", "bullets"])
def test_guarded_context_does_not_expose_withheld_value_or_claim_user_clarification(format):
    safe = result({"project": "other"}, text="Unrelated approved context")
    rendered = ContextBuilder().build_guarded([safe],
        visibility=MemoryVisibility(state="unavailable", scope="affected_context"), format=format, max_tokens=500)
    assert "not yet resolved safely" in rendered.system_prompt_addition
    assert "Unrelated approved context" in rendered.system_prompt_addition
    assert "Enterprise" not in rendered.system_prompt_addition
    assert "not a request for user clarification" in rendered.system_prompt_addition
    if format == "json":
        assert json.loads(rendered.system_prompt_addition)["memory_visibility"]["state"] == "unavailable"
