"""Real HTTP/capture/ledger/outbox, controlled provider output, synthetic owners."""
import asyncio
from datetime import UTC, datetime, timedelta
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.models import AuditLog, Memory, MemoryClaim, PendingExtractionCandidate
from api.services import preference_confirmation_service as reviews
from api.services.runtime_preference_worker import RuntimePreferenceWorker
from tests.integration.test_source_decision_postgres import sql_scope as _scope, _embedding
from tests.integration.test_turn_preparation import turn_http as _http, body
from tests.integration.test_runtime_preference_worker import Cache, Embeddings, Model
from tests.integration.test_runtime_preference_worker import forbid_real_providers as _forbid
from tests.integration.test_runtime_pending_visibility import read, preference_context
from tests.integration.test_preference_snapshots import identity
from tests.integration.test_preference_choice_operations import read as snapshot_read
from tests.unit.test_preference_intent_contract import reply, intent

sql_scope = _scope
turn_http = _http
forbid_real_providers = _forbid
TEXT = "Could you make Python my default for every programming example?"


async def stage(scope, client, *, message=TEXT, value="Python", scope_ref="global", action="set_current"):
    accepted = await client.post("/v1/memories/prepare-turn", json=body(scope, message=message))
    assert accepted.status_code == 200, accepted.text
    model = Model([reply([intent(quote=message, value=value, scope_ref=scope_ref, action=action)])])
    worker = RuntimePreferenceWorker(scope.factory, llm_service=model, embedding_service=Embeddings(),
        cache_service=Cache(), contract_version=2)
    result = await asyncio.to_thread(worker.process, uuid.UUID(accepted.json()["admission"]["job_id"]))
    assert result["memories_created"] == 0 and len(model.calls) == 1
    listed = await client.get("/v1/memories/preference-reviews", params={"external_user_id":scope.external_id})
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["data"]) == 1
    return listed.json()["data"][0]


@pytest.mark.parametrize("message", [
    TEXT,
    "क्या आगे के हर प्रोग्रामिंग उदाहरण के लिए मेरी डिफ़ॉल्ट भाषा Python कर सकते हो?",
    "Kya aage ke har programming example ke liye Python meri default language kar sakte ho?",
    "Use Python instead of C++ as my default for every programming example.",
    "अब से प्रोग्रामिंग उदाहरणों की मेरी सामान्य भाषा C++ की जगह Python रखो।",
    "Ab se programming examples ke liye C++ ki jagah Python meri usual language rakho.",
])
def test_question_is_pending_then_explicit_confirmation_updates_atomically(sql_scope, turn_http, monkeypatch, message):
    embeddings = SimpleNamespace(get_active_model=AsyncMock(return_value=SimpleNamespace(id="test-model")),
        embed=AsyncMock(side_effect=lambda text, **kw: _embedding(text)))
    async def exercise():
        async with turn_http(embeddings=embeddings) as (client, _, sessions, _):
            row = await stage(sql_scope, client, message=message)
            assert row["proposed_value"] == "Python" and row["current_value"] == "C++"
            assert identity(snapshot_read(sql_scope)).target.value == "C++"
            before, visibility, _ = await asyncio.to_thread(read, sql_scope,
                [identity(snapshot_read(sql_scope)).target.memory_id], context=preference_context())
            assert before == [] and visibility is not None
            # Existing source-review endpoint must not accept the new confirmation action.
            old = await client.post(f"/v1/memories/source-reviews/{row['id']}/answer", json={
                "external_user_id":sql_scope.external_id,"version":row["version"],"action":"confirm"})
            assert old.status_code == 422
            # Request shape is closed; clients cannot send a different proposed value.
            forged = await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json={
                "external_user_id":sql_scope.external_id,"version":row["version"],"action":"confirm","value":"Rust"})
            assert forged.status_code == 422
            payload={"external_user_id":sql_scope.external_id,"version":row["version"],"action":"confirm"}
            done = await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json=payload)
            assert done.status_code == 200, done.text
            assert done.json()["data"]["action"] == "confirm"
            assert identity(snapshot_read(sql_scope)).target.value == "Python"
            current_id = identity(snapshot_read(sql_scope)).target.memory_id
            after, visibility, _ = await asyncio.to_thread(read, sql_scope, [current_id], context=preference_context())
            assert len(after) == 1 and after[0].id == str(current_id) and visibility is None
            again = await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json=payload)
            assert again.status_code == 200 and again.json()["data"]["replayed"]
            async with sessions() as session:
                memory = await session.get(Memory, uuid.UUID(done.json()["data"]["memory_id"]))
                assert memory.metadata_json["provenance"]["authority_priority"] == 20
                assert memory.metadata_json["provenance"]["attestation"] == "client_asserted"
                candidate = await session.get(PendingExtractionCandidate, uuid.UUID(row["id"]))
                assert candidate.status == "promoted"
    asyncio.run(exercise())


def test_concurrent_confirmations_have_one_memory_and_one_receipt(sql_scope, turn_http):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def embed(text, **kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return _embedding(text)
        embeddings = SimpleNamespace(get_active_model=AsyncMock(return_value=SimpleNamespace(id="test-model")), embed=embed)
        async with turn_http(embeddings=embeddings) as (client, _, sessions, _):
            row = await stage(sql_scope, client)
            payload={"external_user_id":sql_scope.external_id,"version":row["version"],"action":"confirm"}
            first=asyncio.create_task(client.post(f"/v1/memories/preference-reviews/{row['id']}/answer",json=payload))
            await asyncio.wait_for(entered.wait(),timeout=5)
            second=asyncio.create_task(client.post(f"/v1/memories/preference-reviews/{row['id']}/answer",json=payload))
            release.set()
            results=await asyncio.gather(first,second)
            assert any(r.status_code==200 for r in results)
            assert all(r.status_code in {200,409} for r in results)
            replay=await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer",json=payload)
            assert replay.status_code==200 and replay.json()["data"]["replayed"]
            assert identity(snapshot_read(sql_scope)).target.value == "Python"
            async with sessions() as session:
                rows=list(await session.scalars(select(Memory).where(Memory.proxy_user_id==sql_scope.proxy_id,
                    Memory.is_archived.is_(False))))
                assert len(rows)==1
    asyncio.run(exercise())


def test_permission_revoked_during_embedding_prevents_confirmation(sql_scope, turn_http):
    from api.db.models import ApiKey
    async def embed(text, **kwargs):
        with sql_scope.factory.begin() as session:
            key=session.scalars(select(ApiKey).where(ApiKey.tenant_id==sql_scope.tenant_id)).one()
            key.is_active=False
        return _embedding(text)
    async def exercise():
        async with turn_http(embeddings=SimpleNamespace(get_active_model=AsyncMock(return_value=SimpleNamespace(id="test-model")),embed=embed)) as (client, _, _, _):
            row=await stage(sql_scope,client)
            result=await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer",json={
                "external_user_id":sql_scope.external_id,"version":row["version"],"action":"confirm"})
            assert result.status_code==403, result.text
            assert identity(snapshot_read(sql_scope)).target.value=="C++"
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["target_changed", "source_redacted", "audit_failure", "stronger_authority"])
def test_mutations_and_write_failure_cannot_bypass_confirmation(sql_scope, turn_http, monkeypatch, failure):
    from api.db.models import ExtractionJob
    embeddings = SimpleNamespace(get_active_model=AsyncMock(return_value=SimpleNamespace(id="test-model")),
        embed=AsyncMock(side_effect=lambda text, **kw: _embedding(text)))
    async def exercise():
        async with turn_http(embeddings=embeddings) as (client, _, sessions, _):
            row = await stage(sql_scope, client)
            with sql_scope.factory.begin() as session:
                target = session.get(Memory, identity(snapshot_read(sql_scope)).target.memory_id)
                candidate = session.get(PendingExtractionCandidate, uuid.UUID(row["id"]))
                if failure == "target_changed":
                    target.metadata_json = {**target.metadata_json,"new_external_change":True}
                elif failure == "source_redacted":
                    session.get(ExtractionJob, candidate.extraction_job_id).payload_redacted_at = datetime.now(UTC)
                elif failure == "stronger_authority":
                    ledger = session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == target.id)).one()
                    ledger.authority_priority = 80
            if failure == "audit_failure":
                original_add = Session.add
                def fail_audit(session, instance, *args, **kwargs):
                    if isinstance(instance, AuditLog) and (instance.metadata_json or {}).get("event") == "preference_confirmation_answered":
                        raise ValueError("controlled confirmation audit failure")
                    return original_add(session, instance, *args, **kwargs)
                monkeypatch.setattr("sqlalchemy.orm.Session.add", fail_audit)
            response = await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json={
                "external_user_id":sql_scope.external_id,"version":row["version"],"action":"confirm"})
            assert response.status_code in {403,409}, response.text
            assert identity(snapshot_read(sql_scope)).target.value == "C++"
            async with sessions() as session:
                candidate = await session.get(PendingExtractionCandidate, uuid.UUID(row["id"]))
                assert candidate.status == "pending"
    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["keep_current", "dismiss"])
def test_nonconfirm_actions_never_change_or_promote_preference(sql_scope, turn_http, action):
    async def exercise():
        async with turn_http() as (client, _, _, _):
            row = await stage(sql_scope, client)
            done=await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json={
                "external_user_id":sql_scope.external_id,"version":row["version"],"action":action})
            assert done.status_code == 200, done.text
            assert done.json()["data"]["memory_id"] is None
            assert identity(snapshot_read(sql_scope)).target.value == "C++"
    asyncio.run(exercise())


def test_foreign_user_and_changed_version_do_not_confirm(sql_scope, turn_http):
    async def exercise():
        async with turn_http() as (client, _, _, _):
            row = await stage(sql_scope, client)
            for user,version,expected in [("foreign",row["version"],404),(sql_scope.external_id,"0"*64,409)]:
                result=await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json={
                    "external_user_id":user,"version":version,"action":"confirm"})
                assert result.status_code == expected
            assert identity(snapshot_read(sql_scope)).target.value == "C++"
    asyncio.run(exercise())


def test_same_value_confirmation_reuses_current_memory(sql_scope, turn_http):
    embeddings = SimpleNamespace(get_active_model=AsyncMock(return_value=SimpleNamespace(id="test-model")),
        embed=AsyncMock(side_effect=lambda text, **kw: _embedding(text)))
    async def exercise():
        async with turn_http(embeddings=embeddings) as (client, _, sessions, _):
            row = await stage(sql_scope, client, message="Could you make C++ my default for every programming example?", value="C++")
            original_id = identity(snapshot_read(sql_scope)).target.memory_id
            result = await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json={
                "external_user_id":sql_scope.external_id,"version":row["version"],"action":"confirm"})
            assert result.status_code == 200, result.text
            assert result.json()["data"]["memory_id"] == str(original_id)
            async with sessions() as session:
                rows = list(await session.scalars(select(Memory).where(Memory.proxy_user_id == sql_scope.proxy_id,
                    Memory.is_archived.is_(False))))
                assert len(rows) == 1
    asyncio.run(exercise())


def test_expired_review_still_holds_and_can_only_be_dismissed(sql_scope, turn_http):
    async def exercise():
        async with turn_http() as (client, _, _, _):
            row = await stage(sql_scope, client)
            with sql_scope.factory.begin() as session:
                candidate = session.get(PendingExtractionCandidate, uuid.UUID(row["id"]))
                candidate.created_at = datetime.now(UTC) - timedelta(days=8)
                version = reviews.review_version(candidate)
                assert reviews.view(session, candidate) is None
                assert reviews.pending_fingerprints(session, tenant_id=sql_scope.tenant_id,
                    proxy_user_id=sql_scope.proxy_id) == {identity(snapshot_read(sql_scope)).fingerprint}
            payload = {"external_user_id":sql_scope.external_id,"version":version,"action":"confirm"}
            denied = await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json=payload)
            assert denied.status_code == 409, denied.text
            payload["action"] = "dismiss"
            done = await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json=payload)
            assert done.status_code == 200, done.text
            assert identity(snapshot_read(sql_scope)).target.value == "C++"
            with sql_scope.factory() as session:
                assert not reviews.pending_fingerprints(session, tenant_id=sql_scope.tenant_id, proxy_user_id=sql_scope.proxy_id)
    asyncio.run(exercise())


def test_pending_backlog_is_bounded_and_never_drops_a_hold(sql_scope, turn_http):
    async def exercise():
        async with turn_http() as (client, _, _, _):
            row = await stage(sql_scope, client)
            with sql_scope.factory.begin() as session:
                original = session.get(PendingExtractionCandidate, uuid.UUID(row["id"]))
                for i in range(reviews.MAX_READ_HOLDS):
                    session.add(PendingExtractionCandidate(id=uuid.uuid4(), tenant_id=original.tenant_id,
                        proxy_user_id=original.proxy_user_id, extraction_job_id=original.extraction_job_id,
                        source_event_id=original.source_event_id, content=original.content, category=original.category,
                        importance_score=5, confidence_score=.5, candidate_reason=reviews.REASON,
                        candidate_fingerprint=f"{i:064x}", status="pending", metadata_json=original.metadata_json))
                session.flush()
                assert reviews.pending_fingerprints(session, tenant_id=sql_scope.tenant_id, proxy_user_id=sql_scope.proxy_id) is None
                assert len(reviews.list_reviews(session, tenant_id=sql_scope.tenant_id, proxy_user_id=sql_scope.proxy_id)) <= 3
            before, visibility, _ = await asyncio.to_thread(read, sql_scope,
                [identity(snapshot_read(sql_scope)).target.memory_id], context=preference_context())
            assert before == [] and visibility.scope == "user_learned_context"
    asyncio.run(exercise())


def test_cache_delivery_failure_does_not_undo_confirmed_receipt(sql_scope, turn_http, monkeypatch):
    monkeypatch.setattr("api.services.memory_service.MemoryService._invalidate_retrieval_caches",
        AsyncMock(side_effect=ConnectionError("controlled cache delivery failure")))
    embeddings = SimpleNamespace(get_active_model=AsyncMock(return_value=SimpleNamespace(id="test-model")),
        embed=AsyncMock(side_effect=lambda text, **kw: _embedding(text)))
    async def exercise():
        async with turn_http(embeddings=embeddings) as (client, _, _, _):
            row = await stage(sql_scope, client)
            result = await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json={
                "external_user_id":sql_scope.external_id,"version":row["version"],"action":"confirm"})
            assert result.status_code == 200, result.text
            assert identity(snapshot_read(sql_scope)).target.value == "Python"
    asyncio.run(exercise())


def test_new_scoped_preference_does_not_replace_global_default(sql_scope, turn_http):
    embeddings = SimpleNamespace(get_active_model=AsyncMock(return_value=SimpleNamespace(id="test-model")),
        embed=AsyncMock(side_effect=lambda text, **kw: _embedding(text)))
    async def exercise():
        async with turn_http(embeddings=embeddings) as (client, _, _, _):
            row = await stage(sql_scope, client, message="Could you make Python my default for the Cedar project?",
                scope_ref="cedar", action="assert")
            assert row["current_value"] is None
            assert row["applicability"] == {"kind":"context","namespace":"project","key":"Cedar"}
            with sql_scope.factory() as session:
                assert reviews.list_reviews(session, tenant_id=sql_scope.tenant_id, proxy_user_id=sql_scope.proxy_id,
                    context=preference_context()) == []
            result = await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json={
                "external_user_id":sql_scope.external_id,"version":row["version"],"action":"confirm"})
            assert result.status_code == 200, result.text
            snapshot = snapshot_read(sql_scope)
            assert identity(snapshot).target.value == "C++"
            assert identity(snapshot, "cedar").target.value == "Python"
            assert identity(snapshot, "cedar").target.authority == 20
    asyncio.run(exercise())


def test_wrong_tenant_and_blocked_user_cannot_resolve(sql_scope, turn_http):
    from api.db.models import ProxyUser
    from api.errors import APIError
    async def exercise():
        async with turn_http() as (client, _, _, _):
            row = await stage(sql_scope, client)
            with sql_scope.factory.begin() as session:
                with pytest.raises(APIError) as denied:
                    reviews.answer(session, tenant_id=uuid.uuid4(), proxy_user_id=sql_scope.proxy_id,
                        review_id=row["id"], version=row["version"], action="dismiss")
                assert denied.value.status_code == 404
                session.get(ProxyUser, sql_scope.proxy_id).is_blocked = True
            result = await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json={
                "external_user_id":sql_scope.external_id,"version":row["version"],"action":"dismiss"})
            assert result.status_code == 404, result.text
            with sql_scope.factory() as session:
                assert session.get(PendingExtractionCandidate, uuid.UUID(row["id"])).status == "pending"
    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["target_version", "target_authority"])
def test_target_change_during_embedding_is_rechecked(sql_scope, turn_http, change):
    async def embed(text, **kwargs):
        target_id = identity(snapshot_read(sql_scope)).target.memory_id
        with sql_scope.factory.begin() as session:
            if change == "target_version":
                target = session.get(Memory, target_id)
                target.metadata_json = {**target.metadata_json, "concurrent_change": True}
            else:
                claim = session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == target_id)).one()
                claim.authority_priority = 80
        return _embedding(text)
    async def exercise():
        async with turn_http(embeddings=SimpleNamespace(get_active_model=AsyncMock(return_value=SimpleNamespace(id="test-model")),
            embed=embed)) as (client, _, _, _):
            row = await stage(sql_scope, client)
            denied = await client.post(f"/v1/memories/preference-reviews/{row['id']}/answer", json={
                "external_user_id":sql_scope.external_id,"version":row["version"],"action":"confirm"})
            assert denied.status_code in {403,409}, denied.text
            assert identity(snapshot_read(sql_scope)).target.value == "C++"
            with sql_scope.factory() as session:
                assert session.get(PendingExtractionCandidate, uuid.UUID(row["id"])).status == "pending"
    asyncio.run(exercise())


def test_confirmation_partial_index_bounds_owner_read(sql_scope):
    import json
    from sqlalchemy import text
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex
    with sql_scope.factory.begin() as session:
        session.execute(text("CREATE TEMP TABLE confirmation_plan (LIKE pending_extraction_candidates INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP"))
        session.execute(text("""
            INSERT INTO confirmation_plan (id, tenant_id, proxy_user_id, category, content,
                importance_score, confidence_score, candidate_reason, candidate_fingerprint, status,
                created_at, updated_at, last_seen_at)
            SELECT gen_random_uuid(), :tenant, :proxy, 'preference', 'synthetic', 5, .5,
                'preference_confirmation_pending', md5(i::text), 'pending',
                now() - i * interval '1 second', now(), now()
            FROM generate_series(1,100000) i
        """), {"tenant":sql_scope.tenant_id,"proxy":sql_scope.proxy_id})
        index = next(i for i in PendingExtractionCandidate.__table__.indexes if i.name == "ix_preference_confirmation_owner")
        ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect())).replace(
            "pending_extraction_candidates", "confirmation_plan").replace("ix_preference_confirmation_owner", "confirmation_plan_owner")
        session.execute(text(ddl))
        session.execute(text("ANALYZE confirmation_plan"))
        query = select(PendingExtractionCandidate).where(PendingExtractionCandidate.tenant_id == sql_scope.tenant_id,
            PendingExtractionCandidate.proxy_user_id == sql_scope.proxy_id, reviews.confirmation_predicate(),
            PendingExtractionCandidate.status == "pending").order_by(PendingExtractionCandidate.created_at.desc(),
                PendingExtractionCandidate.id).limit(reviews.MAX_READ_HOLDS + 1)
        sql = str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds":True})).replace(
            "pending_extraction_candidates", "confirmation_plan")
        plan = session.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)).scalar_one()[0]
        assert plan["Plan"]["Actual Rows"] == 9
        assert "confirmation_plan_owner" in json.dumps(plan["Plan"])
        assert "Seq Scan" not in json.dumps(plan["Plan"])


def test_v2_mixed_turn_updates_fact_but_keeps_preference_pending(sql_scope, turn_http):
    from tests.integration.test_preference_fact_transaction import fact
    from tests.integration.test_source_decision_postgres import _decision
    fact_quote = "my exam moved to October 18"
    preference_quote = "Use Python instead of C++"
    message = f"{preference_quote}, and {fact_quote}."
    async def exercise():
        async with turn_http() as (client, _, sessions, _):
            old_fact_id = fact(sql_scope)
            old_preference_id = identity(snapshot_read(sql_scope)).target.memory_id
            accepted = await client.post("/v1/memories/prepare-turn", json=body(sql_scope, message=message))
            assert accepted.status_code == 200, accepted.text
            model = Model([reply([intent(quote=preference_quote)], nonpreferences=[
                {"category":"fact","content":fact_quote,"quote":fact_quote,"turn_index":0}]),
                _decision(old_fact_id, relation="supersedes")])
            embeddings = Embeddings()
            worker = RuntimePreferenceWorker(sql_scope.factory, llm_service=model, embedding_service=embeddings,
                cache_service=Cache(), contract_version=2)
            result = await asyncio.to_thread(worker.process, uuid.UUID(accepted.json()["admission"]["job_id"]))
            assert result["memories_created"] == 1  # The unrelated fact, not the preference.
            assert embeddings.calls == [fact_quote]
            assert identity(snapshot_read(sql_scope)).target.memory_id == old_preference_id
            async with sessions() as session:
                assert (await session.get(Memory, old_fact_id)).is_archived
                created = await session.get(Memory, uuid.UUID(result["stored_memories"][0]["id"]))
                assert created.content == fact_quote and created.category.value == "fact"
                current_fact_id = created.id
            rows, visibility, _ = await asyncio.to_thread(read, sql_scope,
                [old_preference_id, current_fact_id], context=preference_context())
            assert [row.content for row in rows] == [fact_quote]
            assert visibility.scope == "affected_context"
            listed = await client.get("/v1/memories/preference-reviews", params={"external_user_id":sql_scope.external_id})
            assert listed.status_code == 200 and len(listed.json()["data"]) == 1
            assert listed.json()["data"][0]["current_value"] == "C++"
    asyncio.run(exercise())
