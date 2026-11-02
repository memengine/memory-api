"""Opt-in local PostgreSQL regressions, no providers or queue deliveries.

Creates only a fresh synthetic tenant per test and deletes that exact tenant and
its synthetic user afterward. Existing rows, identities and keys are not edited.
Can run with stdlib unittest inside the local container; no test dependency install.
"""
import asyncio
import hashlib
import os
import unittest
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    ApiKey,
    AuditLog,
    Conversation,
    ConversationProcessingStatus,
    EmbeddingModel,
    ExtractionJob,
    ExtractionJobStatus,
    Memory,
    MemoryClaim,
    MemoryClaimRevision,
    MemorySourceEvent,
    MemoryVersion,
    PendingExtractionCandidate,
    PlanTier,
    ProxyUser,
    ServiceWriter,
    Tenant,
    User,
    VectorSyncOutbox,
)
from api.errors import APIError
from api.schemas.service_corrections import ServiceCorrectionDecision
from api.services.claim_ledger_service import ClaimLedgerService
from api.services.embedding_service import EmbeddingResult
from api.services.provenance_service import build_provenance_snapshot
from api.services.retriever import RetrieverService
from api.services.service_correction_service import ServiceCorrectionService
from api.services.version_service import VersionService
from api.settings import get_settings


@unittest.skipUnless(os.environ.get("MEMORYOS_SERVICE_CORRECTION_SQL") == "1", "opt-in local SQL regressions")
class ServiceCorrectionSQL(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = get_settings().database_url
        self.engine = create_async_engine(url)
        self.assertIn(self.engine.url.host, {"postgres", "127.0.0.1", "localhost"})
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.tenant, self.owner, self.user, self.admin, self.source_key, self.writer = [uuid.uuid4() for _ in range(6)]
        self.old_event, self.event, self.job, self.target, self.candidate = [uuid.uuid4() for _ in range(5)]
        self.external = "service-correction-regression-" + str(self.owner)
        self.embedding_calls = 0
        async with self.factory.begin() as session:
            await session.run_sync(self.seed)

    def seed(self, session):
        model = session.scalars(select(EmbeddingModel).where(EmbeddingModel.is_active.is_(True))).first()
        assert model is not None
        self.model_id, self.dimensions, self.collection = model.id, model.dimensions, model.qdrant_collection
        session.add(Tenant(id=self.tenant, company_name="service-correction-regression", plan_tier=PlanTier.free))
        session.add(User(id=self.user, external_id=f"proxy::{self.owner}", email=f"{self.owner}@invalid.test"))
        session.flush()
        session.add(ProxyUser(id=self.owner, tenant_id=self.tenant, external_user_id=self.external,
            external_user_id_hash=hashlib.sha256(self.external.encode()).hexdigest()))
        session.add_all([ApiKey(id=self.admin, tenant_id=self.tenant, name="synthetic operator", key_hash="x"*60,
            permissions=["admin"]), ApiKey(id=self.source_key, tenant_id=self.tenant, name="synthetic source",
            key_hash="y"*60, permissions=["write"])])
        session.flush()
        writer = ServiceWriter(id=self.writer, tenant_id=self.tenant, api_key_id=self.source_key,
            service_key="synthetic-crm", display_name="Synthetic CRM", authority_rules={"default_priority": 95})
        session.add(writer)
        session.flush()
        scope = {"account": str(self.owner), "project": True}
        events = []
        for event_id, age in ((self.old_event, 30), (self.event, 10)):
            event = MemorySourceEvent(id=event_id, tenant_id=self.tenant, proxy_user_id=self.owner,
                writer_id=self.writer, api_key_id=self.source_key, source_service=writer.service_key,
                source_event_id=str(event_id), observed_at=datetime.now(UTC)-timedelta(minutes=age),
                payload_hash="f"*64, scope=scope, evidence_refs=[{"reference": "synthetic fixture"}])
            session.add(event)
            events.append(event)
        session.flush()
        provenance = build_provenance_snapshot(events[0])
        conversation = Conversation(id=uuid.uuid4(), user_id=self.user, message_count=0,
            processing_status=ConversationProcessingStatus.done)
        session.add(conversation)
        session.flush()
        memory = Memory(id=self.target, user_id=self.user, proxy_user_id=self.owner,
            source_event_id=self.old_event, source_conversation_id=conversation.id, content="Account plan Scale",
            category="fact", importance_score=6, confidence_score=0.9, embedding_id=str(self.target),
            embedding_model_id=self.model_id, metadata_json={"provenance": provenance})
        session.add(memory)
        session.flush()
        VersionService(session).record_version(memory, "created", "Synthetic fixture", "system")
        ClaimLedgerService(session).record_memory(memory, tenant_id=self.tenant, proxy_user_id=self.owner,
            provenance=provenance, resolution="NEW")
        session.add(ExtractionJob(id=self.job, tenant_id=self.tenant, proxy_user_id=self.owner,
            external_user_id=self.external, source_event_id=self.event, status=ExtractionJobStatus.completed))
        session.flush()
        session.add(PendingExtractionCandidate(id=self.candidate, tenant_id=self.tenant, proxy_user_id=self.owner,
            extraction_job_id=self.job, source_event_id=self.event, content="Account plan Enterprise",
            category="fact", importance_score=6, confidence_score=0.9, reasoning="Synthetic correction",
            candidate_reason="source_decision_pending", candidate_fingerprint=hashlib.sha256(str(self.candidate).encode()).hexdigest(),
            metadata_json={"extraction_evidence": {"claim_state": "correction", "source_decision": {
                "reason_codes": ["source_correction_unresolved"]}, "pending_source_binding_v1": {
                    "source_event_id": str(self.event), "writer_id": str(self.writer), "scope": scope}}}))

    async def asyncTearDown(self):
        async with self.factory.begin() as session:
            tenant = await session.get(Tenant, self.tenant)
            if tenant is not None:
                self.assertEqual(tenant.company_name, "service-correction-regression")
                await session.execute(delete(VectorSyncOutbox).where(VectorSyncOutbox.memory_id.in_(
                    select(Memory.id).where(Memory.proxy_user_id == self.owner))))
                await session.execute(delete(Tenant).where(Tenant.id == self.tenant))
            await session.execute(delete(User).where(User.id == self.user, User.external_id == f"proxy::{self.owner}"))
        async with self.factory() as session:
            self.assertIsNone(await session.get(Tenant, self.tenant))
            self.assertIsNone(await session.get(User, self.user))
            self.assertEqual(await session.scalar(select(func.count()).select_from(Memory).where(Memory.proxy_user_id == self.owner)), 0)
        await self.engine.dispose()

    def binding(self):
        return {"tenant_id": self.tenant, "actor_id": self.admin, "candidate_id": self.candidate}

    async def preview(self):
        async with self.factory() as session:
            return await ServiceCorrectionService(session).preview(**self.binding(),
                external_user_id=self.external, target_id=self.target)

    async def payload(self, action="apply"):
        view = await self.preview()
        return ServiceCorrectionDecision(external_user_id=self.external, target_memory_id=self.target,
            target_version=view.target_version, review_version=view.review_version, action=action)

    async def embed(self, text, **kwargs):
        self.embedding_calls += 1
        self.assertEqual(text, "Account plan Enterprise")
        return EmbeddingResult([0.01]*self.dimensions, self.model_id, self.dimensions, self.collection)

    async def resolve(self, payload, embed=None):
        async with self.factory() as session:
            return await ServiceCorrectionService(session, embedder=embed or self.embed).resolve(**self.binding(), payload=payload)

    async def mutate(self, model, key, **values):
        async with self.factory.begin() as session:
            await session.execute(update(model).where(model.id == key).values(**values))

    async def assert_untouched(self):
        async with self.factory() as session:
            self.assertFalse((await session.get(Memory, self.target)).is_archived)
            self.assertEqual((await session.get(PendingExtractionCandidate, self.candidate)).status, "pending")
            for model, expected, condition in (
                (Memory, 1, Memory.proxy_user_id == self.owner),
                (MemoryClaimRevision, 1, MemoryClaimRevision.memory_id == self.target),
                (MemoryVersion, 1, MemoryVersion.memory_id == self.target),
                (AuditLog, 0, AuditLog.proxy_user_id == self.owner),
                (VectorSyncOutbox, 0, VectorSyncOutbox.memory_id.in_(select(Memory.id).where(Memory.proxy_user_id == self.owner)))):
                self.assertEqual(await session.scalar(select(func.count()).select_from(model).where(condition)), expected)

    async def assert_one_application(self, result):
        async with self.factory() as session:
            old = await session.get(Memory, self.target)
            new = await session.get(Memory, result.memory_id)
            candidate = await session.get(PendingExtractionCandidate, self.candidate)
            claim = await session.get(MemoryClaim, result.claim_id)
            revision = await session.get(MemoryClaimRevision, result.winning_revision_id)
            self.assertTrue(old.is_archived)
            self.assertFalse(new.is_archived)
            self.assertEqual(new.content, "Account plan Enterprise")
            self.assertEqual(new.source_event_id, self.event)
            self.assertEqual(new.previous_version_id, old.id)
            self.assertEqual(candidate.status, "promoted")
            self.assertEqual(claim.active_memory_id, new.id)
            self.assertEqual(revision.memory_id, new.id)
            self.assertEqual(revision.source_event_id, self.event)
            self.assertEqual(revision.authority_priority, 95)
            self.assertEqual(claim.winning_revision_id, revision.id)
            self.assertEqual(await session.scalar(select(func.count()).select_from(Memory).where(Memory.proxy_user_id == self.owner)), 2)
            self.assertEqual(await session.scalar(select(func.count()).select_from(MemoryClaimRevision).where(
                MemoryClaimRevision.claim_id == claim.id, MemoryClaimRevision.status == "activated")), 1)
            self.assertEqual(await session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.proxy_user_id == self.owner)), 1)
            self.assertEqual(await session.scalar(select(func.count()).select_from(MemoryVersion).where(MemoryVersion.memory_id == old.id)), 2)
            self.assertEqual(await session.scalar(select(func.count()).select_from(MemoryVersion).where(MemoryVersion.memory_id == new.id)), 1)
            outbox = (await session.scalars(select(VectorSyncOutbox).where(VectorSyncOutbox.memory_id.in_([old.id, new.id])))).all()
            self.assertEqual(len(outbox), 2)
            self.assertEqual({r.operation.value for r in outbox}, {"archive", "upsert"})
            service = RetrieverService(session=session, qdrant_service=None, cache_service=None,
                quota_manager=None, embedding_service=object())
            current = await service._project_current_sources([service._memory_to_result(old, semantic_score=1)],
                owner=str(self.owner), tenant_id=str(self.tenant), categories=[], agent_id=None, time_filter_days=None, limit=10)
            self.assertEqual([r.id for r in current], [str(new.id)])
            self.assertIsNone(service.last_memory_visibility)
            # Separate publication guard remains active until outbox delivery.
            self.assertTrue(await service._source_index_pending(str(self.owner), str(self.tenant)))

    async def test_apply_replay_and_changed_binding(self):
        payload = await self.payload()
        result = await self.resolve(payload)
        self.assertFalse(result.replayed)
        await self.assert_one_application(result)
        repeated = await self.resolve(payload)
        self.assertTrue(repeated.replayed)
        self.assertEqual(repeated.memory_id, result.memory_id)
        self.assertEqual(self.embedding_calls, 1)
        with self.assertRaises(APIError) as error:
            await self.resolve(payload.model_copy(update={"action": "decline"}))
        self.assertEqual(error.exception.status_code, 409)
        await self.assert_one_application(result)

    async def test_decline_preserves_current_fact(self):
        result = await self.resolve(await self.payload("decline"))
        self.assertEqual(result.memory_id, self.target)
        self.assertEqual(result.indexing_state, "unchanged")
        self.assertEqual(self.embedding_calls, 0)
        async with self.factory() as session:
            self.assertFalse((await session.get(Memory, self.target)).is_archived)
            self.assertEqual((await session.get(PendingExtractionCandidate, self.candidate)).status, "dismissed")

    async def test_policy_demotion_blocks_apply_but_allows_explicit_decline(self):
        await self.mutate(ServiceWriter, self.writer, authority_rules={"default_priority": 10})
        view = await self.preview()
        self.assertEqual(view.actions, ["decline"])
        self.assertEqual(view.target_priority, 95)
        self.assertIn("lower_authority_source", view.apply_blockers)
        with self.assertRaises(APIError):
            await self.resolve(await self.payload())
        await self.assert_untouched()
        await self.resolve(await self.payload("decline"))
        self.assertEqual(self.embedding_calls, 0)

    async def test_stale_and_future_observations_cannot_apply(self):
        for age in (timedelta(days=-2), timedelta(days=2)):
            await self.mutate(MemorySourceEvent, self.event, observed_at=datetime.now(UTC)+age)
            self.assertIn("source_observation_not_current", (await self.preview()).apply_blockers)
            with self.assertRaises(APIError):
                await self.resolve(await self.payload())
        self.assertEqual(self.embedding_calls, 0)
        await self.assert_untouched()

    async def test_stale_target_or_policy_token_is_refused(self):
        payload = await self.payload()
        await self.mutate(ServiceWriter, self.writer, authority_rules={"default_priority": 96})
        with self.assertRaises(APIError) as error:
            await self.resolve(payload)
        self.assertEqual(error.exception.error, "service_correction_preview_stale")
        payload = await self.payload()
        async with self.factory.begin() as session:
            target = await session.get(Memory, self.target)
            target.metadata_json = {**target.metadata_json, "synthetic_change": True}
        with self.assertRaises(APIError):
            await self.resolve(payload)
        await self.assert_untouched()

    async def test_authorization_revoked_during_embedding_is_rechecked(self):
        payload = await self.payload()
        async def revoke(text, **kwargs):
            await self.mutate(ApiKey, self.admin, permissions=["read", "write"])
            return await self.embed(text, **kwargs)
        with self.assertRaises(APIError) as error:
            await self.resolve(payload, revoke)
        self.assertEqual(error.exception.status_code, 403)
        await self.assert_untouched()

    async def test_policy_changed_during_embedding_is_rechecked(self):
        payload = await self.payload()
        async def change(text, **kwargs):
            await self.mutate(ServiceWriter, self.writer, authority_rules={"default_priority": 96})
            return await self.embed(text, **kwargs)
        with self.assertRaises(APIError) as error:
            await self.resolve(payload, change)
        self.assertEqual(error.exception.status_code, 409)
        await self.assert_untouched()

    async def test_target_changed_during_embedding_is_rechecked(self):
        payload = await self.payload()
        async def change(text, **kwargs):
            async with self.factory.begin() as session:
                target = await session.get(Memory, self.target)
                target.metadata_json = {**target.metadata_json, "synthetic_change": True}
            return await self.embed(text, **kwargs)
        with self.assertRaises(APIError) as error:
            await self.resolve(payload, change)
        self.assertEqual(error.exception.error, "service_correction_preview_stale")
        await self.assert_untouched()

    async def test_invalid_embedding_cannot_commit_partial_state(self):
        payload = await self.payload()
        async def invalid(*args, **kwargs):
            return EmbeddingResult([float("nan")]*self.dimensions, self.model_id, self.dimensions, self.collection)
        with self.assertRaises(APIError) as error:
            await self.resolve(payload, invalid)
        self.assertEqual(error.exception.error, "service_correction_embedding_invalid")
        await self.assert_untouched()

    async def test_source_disabled_or_credential_revoked_cannot_apply(self):
        await self.mutate(ServiceWriter, self.writer, is_active=False)
        self.assertIn("source_disabled", (await self.preview()).apply_blockers)
        await self.mutate(ServiceWriter, self.writer, is_active=True)
        await self.mutate(ApiKey, self.source_key, is_active=False)
        self.assertIn("source_credential_unavailable", (await self.preview()).apply_blockers)
        with self.assertRaises(APIError):
            await self.resolve(await self.payload())
        await self.assert_untouched()

    async def test_foreign_owner_tenant_and_blocked_owner_are_denied(self):
        async with self.factory() as session:
            service = ServiceCorrectionService(session)
            for changes in ({"external_user_id": "other-owner"}, {"tenant_id": uuid.uuid4()}):
                binding = dict(self.binding(), external_user_id=self.external, target_id=self.target)
                binding.update(changes)
                with self.assertRaises(APIError):
                    await service.preview(**binding)
                await session.rollback()
        await self.mutate(ProxyUser, self.owner, is_blocked=True)
        with self.assertRaises(APIError):
            await self.preview()
        await self.assert_untouched()

    async def test_wrong_scope_type_and_source_binding_cannot_be_adopted(self):
        async with self.factory.begin() as session:
            candidate = await session.get(PendingExtractionCandidate, self.candidate)
            event = await session.get(MemorySourceEvent, self.event)
            scope = {"account": str(self.owner), "project": "true"}
            event.scope = scope
            metadata = deepcopy(candidate.metadata_json)
            metadata["extraction_evidence"]["pending_source_binding_v1"]["scope"] = scope
            candidate.metadata_json = metadata
        with self.assertRaises(APIError) as error:
            await self.preview()
        self.assertEqual(error.exception.error, "service_correction_target_not_current_or_scope_mismatch")
        async with self.factory.begin() as session:
            candidate = await session.get(PendingExtractionCandidate, self.candidate)
            candidate.metadata_json = {"extraction_evidence": {"claim_state": "correction",
                "source_decision": {"reason_codes": ["source_correction_unresolved"]}}}
        with self.assertRaises(APIError) as error:
            await self.preview()
        self.assertEqual(error.exception.error, "service_correction_source_binding_changed")
        await self.assert_untouched()

    async def test_injected_outbox_failure_rolls_back_all_mutations(self):
        payload = await self.payload()
        with patch("api.services.service_correction_service.enqueue_vector_upsert", side_effect=RuntimeError("injected")), self.assertRaisesRegex(RuntimeError, "injected"):
            await self.resolve(payload)
        await self.assert_untouched()

    async def test_five_way_retry_has_one_mutation_and_receipt(self):
        payload = await self.payload()
        ready = asyncio.Event()
        arrivals = 0
        async def barrier(text, **kwargs):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 5:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=20)
            return await self.embed(text, **kwargs)
        results = await asyncio.gather(*(self.resolve(payload, barrier) for _ in range(5)), return_exceptions=True)
        successful = [r for r in results if not isinstance(r, BaseException)]
        self.assertTrue(successful)
        for result in results:
            if isinstance(result, BaseException):
                self.assertIsInstance(result, APIError)
                self.assertEqual(result.error, "service_correction_busy_retry")
        final = await self.resolve(payload)
        self.assertTrue(final.replayed)
        self.assertTrue(all(r.memory_id == final.memory_id for r in successful))
        self.assertEqual(sum(not r.replayed for r in successful), 1)
        await self.assert_one_application(final)

    async def test_admin_revocation_also_blocks_replay(self):
        payload = await self.payload()
        await self.resolve(payload)
        await self.mutate(ApiKey, self.admin, is_active=False)
        with self.assertRaises(APIError) as error:
            await self.resolve(payload)
        self.assertEqual(error.exception.status_code, 403)

    async def test_discovery_and_preview_do_not_modify_pending_state(self):
        async with self.factory() as session:
            items, cursor = await ServiceCorrectionService(session).list_pending(tenant_id=self.tenant,
                actor_id=self.admin, external_user_id=self.external)
            self.assertEqual([item.id for item in items], [self.candidate])
            self.assertIsNone(cursor)
        await self.preview()
        await self.assert_untouched()

    async def test_in_process_http_contract_decline_and_current_sql_permission(self):
        # Real routes/schemas/SQL; injected principal, NOT native authentication proof.
        import httpx
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse

        from api.dependencies import get_authenticated_tenant_id, get_db_session
        from api.routers.service_corrections import router
        app = FastAPI()
        app.include_router(router, prefix="/v1/memories")
        @app.middleware("http")
        async def principal(request: Request, call_next):
            request.state.auth_scheme = "apikey"
            request.state.api_key_id = request.headers.get("x-synthetic-key-id", str(self.admin))
            return await call_next(request)
        @app.exception_handler(APIError)
        async def error_handler(request, error):
            return JSONResponse({"error": error.error}, status_code=error.status_code)
        async def database():
            async with self.factory() as session:
                yield session
        app.dependency_overrides[get_db_session] = database
        app.dependency_overrides[get_authenticated_tenant_id] = lambda: str(self.tenant)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
            base = "/v1/memories/service-corrections"
            listed = await client.get(base, params={"external_user_id": self.external})
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(listed.json()["data"][0]["id"], str(self.candidate))
            preview = await client.get(base+f"/{self.candidate}", params={"external_user_id": self.external,
                "target_memory_id": str(self.target)})
            self.assertEqual(preview.status_code, 200, preview.text)
            view = preview.json()["data"]
            body = {"external_user_id": self.external, "target_memory_id": str(self.target),
                "target_version": view["target_version"], "review_version": view["review_version"], "action": "decline"}
            denied = await client.post(base+f"/{self.candidate}/decision", json=body,
                headers={"x-synthetic-key-id": str(self.source_key)})
            self.assertEqual(denied.status_code, 403, denied.text)
            invalid = await client.post(base+f"/{self.candidate}/decision", json={**body, "content": "forged"})
            self.assertEqual(invalid.status_code, 422, invalid.text)
            decided = await client.post(base+f"/{self.candidate}/decision", json=body)
            self.assertEqual(decided.status_code, 200, decided.text)
            self.assertEqual(decided.json()["data"]["candidate_status"], "dismissed")
            repeated = await client.post(base+f"/{self.candidate}/decision", json=body)
            self.assertEqual(repeated.status_code, 200, repeated.text)
            self.assertTrue(repeated.json()["data"]["replayed"])
        self.assertEqual(self.embedding_calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
