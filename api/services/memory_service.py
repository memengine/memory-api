from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Awaitable
from typing import Callable

from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import EmbeddingModel
from api.db.models import ExtractionJob
from api.db.models import ExtractionJobStatus
from api.db.models import Memory
from api.db.models import MemoryClaim
from api.db.models import MemoryClaimRevision
from api.db.models import MemorySourceEvent
from api.db.models import ProxyUser
from api.db.vector_store import QdrantService
from api.errors import APIError
from api.services.embedding_service import EmbeddingResult
from api.services.embedding_service import EmbeddingService
from api.services.proxy_user_service import ProxyUserService
from api.services.provenance_service import ProvenanceService
from api.services.provenance_service import SOURCE_EVENT_HASH_VERSION
from api.services.provenance_service import source_event_sha256
from api.services.provenance_service import source_event_payload_matches
from api.services.common import resolve_authorized_user
from api.services.quota_manager import QuotaManager
from api.services.vector_outbox import build_vector_payload
from api.services.vector_outbox import enqueue_vector_delete
from api.infra.protected_storage import encrypt_json_for_dual_write
from api.infra.protected_storage import encrypt_text_for_dual_write
from api.services.vector_outbox import enqueue_vector_upsert
from api.services.version_service import VersionService
from api.tasks.queue_router import QueueRouter
from api.settings import get_settings


EXTRACTION_TASK_NAME = "api.tasks.extraction_tasks.process_extraction_job"
DEFAULT_MAX_EXTRACTION_ATTEMPTS = 3
DispatchTask = Callable[[str, list[Any]], Awaitable[Any] | Any]
LOGGER = logging.getLogger("memoryos.memory_service")


class MemoryService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        cache_service: CacheService,
        qdrant_service: QdrantService,
        quota_manager: QuotaManager,
        proxy_user_service: ProxyUserService | None = None,
        embedding_service: EmbeddingService | None = None,
        dispatch_task: DispatchTask | None = None,
        region_id: str | None = None,
    ) -> None:
        self.session = session
        self.cache_service = cache_service
        self.qdrant_service = qdrant_service
        self.quota_manager = quota_manager
        self.proxy_user_service = proxy_user_service
        self.embedding_service = embedding_service or EmbeddingService(async_session=session)
        self.dispatch_task = dispatch_task
        self.queue_router = QueueRouter(session=session, cache_service=cache_service)
        self.region_id = region_id

    async def get_idempotent_memory_add(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        return await self.cache_service.get_idempotent_response(
            idempotency_key,
            scope=f"tenant:{tenant_id}",
            operation="memory_add",
        )

    async def queue_memory_add(
        self,
        *,
        requested_user_id: str | None,
        authenticated_user_id: str | None,
        agent_id: str | None,
        messages: list[dict[str, str]],
        metadata: dict[str, Any],
        idempotency_key: str | None,
        tenant_id: str | None = None,
        external_user_id: str | None = None,
        proxy_user_id: str | None = None,
        api_key_id: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        proxy_user = None
        resolved_proxy_user_id = proxy_user_id
        if tenant_id:
            quota_envelope = await self.quota_manager.get_quota_envelope(tenant_id)
            if quota_envelope.mode.value == "BLOCKED":
                return {
                    "job_id": None,
                    "status": "blocked",
                    "blocked_reason": "budget_exhausted",
                    "budget_remaining_pct": quota_envelope.budget_remaining_pct,
                }
            if quota_envelope.mode.value == "PASSTHROUGH":
                return {
                    "job_id": None,
                    "status": "passthrough",
                    "blocked_reason": None,
                    "budget_remaining_pct": quota_envelope.budget_remaining_pct,
                }
            if not external_user_id:
                raise APIError(
                    status_code=422,
                    code="REQ_422",
                    error="external_user_id_required",
                )
            if not resolved_proxy_user_id:
                if self.proxy_user_service is None:
                    raise APIError(
                        status_code=500,
                        code="PRX_500",
                        error="proxy_user_service_unavailable",
                    )
                proxy_user = await self.proxy_user_service.resolve(
                    tenant_id=tenant_id,
                    external_user_id=external_user_id,
                    metadata=metadata,
                )
                resolved_proxy_user_id = str(proxy_user.id)

        if authenticated_user_id and not tenant_id:
            await resolve_authorized_user(
                self.session,
                requested_user_id=requested_user_id,
                authenticated_user_id=authenticated_user_id,
            )
        idempotency_scope = (
            f"tenant:{tenant_id}"
            if tenant_id
            else f"user:{authenticated_user_id or requested_user_id or resolved_proxy_user_id or 'anonymous'}"
        )
        if idempotency_key:
            cached_job = await self.cache_service.get_idempotent_response(
                idempotency_key,
                scope=idempotency_scope,
                operation="memory_add",
            )
            if cached_job is not None:
                return cached_job

        job = {
            "job_id": str(uuid.uuid4()),
            "status": "queued",
            "memories_created": 0,
            "proxy_user_id": resolved_proxy_user_id,
            "tenant_id": tenant_id,
            "external_user_id": external_user_id,
            "agent_id": agent_id,
            "message_count": len(messages),
            "messages": messages,
            "metadata": metadata,
            "queued_at": datetime.now(UTC).isoformat(),
        }
        if tenant_id:
            provenance_service = ProvenanceService(self.session)
            writer = await provenance_service.resolve_writer(
                tenant_id=tenant_id,
                api_key_id=api_key_id,
                requested_service=str(source.get("service")) if source and source.get("service") else None,
            )
            normalized_source = provenance_service.normalize_source(
                source=source,
                writer=writer,
                api_key_id=api_key_id,
                job_id=job["job_id"],
            )
            job["source"] = {
                **normalized_source,
                "observed_at": normalized_source["observed_at"].isoformat(),
                "writer_id": str(writer.id) if writer is not None else None,
                "api_key_id": api_key_id,
                "payload_hash": source_event_sha256(
                    messages=messages,
                    source=normalized_source,
                ),
                "payload_hash_version": SOURCE_EVENT_HASH_VERSION,
                "explicit": source is not None,
            }
        if tenant_id:
            reservation = await self.queue_router.reserve_extraction_slot(
                tenant_id=tenant_id,
                job_id=job["job_id"],
            )
            if reservation is None:
                return {
                    "job_id": None,
                    "status": "queue_full",
                    "blocked_reason": "tenant_queue_limit_reached",
                }
            job["queue_name"] = reservation.queue_name
            job["plan_tier"] = reservation.plan_tier
        persisted_job, created = await self._create_extraction_job(job)
        if not created:
            if tenant_id and job.get("queue_name"):
                await self.queue_router.release_extraction_slot(
                    tenant_id=tenant_id,
                    queue_name=str(job["queue_name"]),
                    job_id=job["job_id"],
                )
            return persisted_job
        await self.cache_service.set_job_status(job["job_id"], job, ttl=3600)
        if idempotency_key:
            await self.cache_service.set_idempotent_response(
                idempotency_key,
                job,
                ttl=86400,
                scope=idempotency_scope,
                operation="memory_add",
            )
        dispatch_error = await self._dispatch_extraction_job(job)
        if dispatch_error:
            job["status"] = "error"
            job["error"] = dispatch_error
            await self._mark_extraction_job_failed(
                job_id=job["job_id"],
                error=dispatch_error,
                error_type=self._classify_job_error(dispatch_error),
                status=ExtractionJobStatus.failed,
            )
            if tenant_id and job.get("queue_name"):
                await self.queue_router.release_extraction_slot(
                    tenant_id=tenant_id,
                    queue_name=str(job["queue_name"]),
                    job_id=job["job_id"],
                )
            await self.cache_service.set_job_status(job["job_id"], job, ttl=3600)
        return job

    async def list_memories(
        self,
        *,
        requested_user_id: str | None,
        authenticated_user_id: str | None,
        tenant_id: str | None = None,
        cursor: str | None,
        limit: int,
        categories: list[str],
        agent_id: str | None,
        external_user_id: str | None = None,
    ) -> tuple[list[Memory], str | None, int]:
        if tenant_id:
            base_query = (
                select(Memory)
                .join(ProxyUser, Memory.proxy_user_id == ProxyUser.id)
                .where(ProxyUser.tenant_id == uuid.UUID(tenant_id))
            )
            if external_user_id:
                base_query = base_query.where(ProxyUser.external_user_id == external_user_id)
        else:
            if not authenticated_user_id:
                raise APIError(status_code=401, code="AUTH_001", error="unauthorized")
            user = await resolve_authorized_user(
                self.session,
                requested_user_id=requested_user_id,
                authenticated_user_id=authenticated_user_id,
            )
            base_query = (
                select(Memory)
                .where(Memory.user_id == user.id)
            )
        if categories:
            base_query = base_query.where(Memory.category.in_(categories))
        if agent_id:
            base_query = base_query.where(Memory.agent_id == uuid.UUID(agent_id))

        count_query = select(func.count()).select_from(base_query.order_by(None).subquery())
        total_result = await self.session.execute(count_query)
        total = int(total_result.scalar_one() or 0)

        page_query = base_query
        if cursor:
            try:
                cursor_id = uuid.UUID(cursor)
            except ValueError:
                cursor_id = None
            if cursor_id is not None:
                cursor_row = (
                    await self.session.execute(
                        base_query.order_by(None).where(Memory.id == cursor_id).limit(1)
                    )
                ).scalar_one_or_none()
                if cursor_row is not None:
                    page_query = page_query.where(
                        or_(
                            Memory.created_at < cursor_row.created_at,
                            (
                                (Memory.created_at == cursor_row.created_at)
                                & (Memory.id < cursor_row.id)
                            ),
                        )
                    )

        result = await self.session.execute(
            page_query.order_by(Memory.created_at.desc(), Memory.id.desc()).limit(limit + 1)
        )
        page = list(result.scalars().all())
        has_more = len(page) > limit
        memories = page[:limit]
        next_cursor = str(memories[-1].id) if has_more and memories else None
        return memories, next_cursor, total

    async def get_memory(
        self,
        *,
        authenticated_user_id: str | None,
        memory_id: str,
        tenant_id: str | None = None,
    ) -> Memory:
        memory = await self._get_authorized_memory(
            authenticated_user_id=authenticated_user_id,
            memory_id=memory_id,
            tenant_id=tenant_id,
        )
        return memory

    async def update_memory(
        self,
        *,
        authenticated_user_id: str | None,
        memory_id: str,
        content: str | None,
        importance_score: float | None,
        is_archived: bool | None,
        tenant_id: str | None = None,
    ) -> Memory:
        memory = await self._get_authorized_memory(
            authenticated_user_id=authenticated_user_id,
            memory_id=memory_id,
            tenant_id=tenant_id,
        )
        requires_vector_sync = content is not None or importance_score is not None or is_archived is not None
        next_content = content if content is not None else memory.content
        next_archived = bool(is_archived) if is_archived is not None else bool(memory.is_archived)
        next_embedding: EmbeddingResult | None = None
        if requires_vector_sync and not next_archived:
            next_embedding = await self._embed_content(next_content)
        if content is not None:
            memory.content = content
            if tenant_id is not None:
                memory.content_envelope = encrypt_text_for_dual_write(
                    tenant_id=str(tenant_id),
                    record_type="memory-content",
                    record_id=str(memory.id),
                    value=content,
                )
        if importance_score is not None:
            memory.importance_score = importance_score
        if is_archived is not None:
            memory.is_archived = is_archived
        memory.updated_at = datetime.now(UTC)
        if content is not None or importance_score is not None:
            await VersionService(self.session).asafe_record_version(
                memory,
                "manual_edit",
                "Edited by tenant admin",
                "user",
            )
        elif is_archived:
            await VersionService(self.session).asafe_record_version(
                memory,
                "archived",
                "Archived by tenant admin",
                "user",
            )
        if requires_vector_sync:
            if memory.is_archived:
                qdrant_collection = await self._embedding_collection_for_memory(memory)
                enqueue_vector_delete(
                    self.session,
                    memory_id=memory.id,
                    payload={
                        "memory_id": str(memory.id),
                        "embedding_model_id": memory.embedding_model_id,
                        "qdrant_collection": qdrant_collection,
                    },
                )
            elif next_embedding is not None:
                tenant_id = None
                proxy_user_id = str(memory.proxy_user_id) if memory.proxy_user_id else None
                if memory.proxy_user_id is not None:
                    proxy_user = await self.session.get(ProxyUser, memory.proxy_user_id)
                    tenant_id = str(proxy_user.tenant_id) if proxy_user is not None else None
                memory.embedding_model_id = next_embedding.model_id
                enqueue_vector_upsert(
                    self.session,
                    memory_id=memory.id,
                    embedding=next_embedding.vector,
                    payload=build_vector_payload(
                        memory,
                        tenant_id=tenant_id,
                        proxy_user_id=proxy_user_id,
                        user_id=str(memory.user_id),
                        embedding_model_id=next_embedding.model_id,
                        qdrant_collection=next_embedding.qdrant_collection,
                    ),
                )
        await self.session.commit()
        await self.session.refresh(memory)
        await self._invalidate_retrieval_caches(self._cache_identity(memory))
        return memory

    async def delete_memory(
        self,
        *,
        authenticated_user_id: str | None,
        memory_id: str,
        hard_delete: bool,
        tenant_id: str | None = None,
    ) -> bool:
        memory = await self._get_authorized_memory(
            authenticated_user_id=authenticated_user_id,
            memory_id=memory_id,
            tenant_id=tenant_id,
        )
        if hard_delete:
            await VersionService(self.session).asafe_record_version(
                memory,
                "archived",
                "Deleted by tenant admin",
                "user",
            )
            qdrant_collection = await self._embedding_collection_for_memory(memory)
            enqueue_vector_delete(
                self.session,
                memory_id=memory.id,
                payload={
                    "memory_id": str(memory.id),
                    "embedding_model_id": memory.embedding_model_id,
                    "qdrant_collection": qdrant_collection,
                },
            )
            await self._hard_delete_memory_and_reconcile_claims(memory)
        else:
            memory.is_archived = True
            await VersionService(self.session).asafe_record_version(
                memory,
                "archived",
                "Deleted by tenant admin",
                "user",
            )
            qdrant_collection = await self._embedding_collection_for_memory(memory)
            enqueue_vector_delete(
                self.session,
                memory_id=memory.id,
                payload={
                    "memory_id": str(memory.id),
                    "embedding_model_id": memory.embedding_model_id,
                    "qdrant_collection": qdrant_collection,
                },
            )
        await self.session.commit()
        await self._invalidate_retrieval_caches(self._cache_identity(memory))
        return True

    async def _invalidate_retrieval_caches(self, cache_identity: str) -> None:
        await self.cache_service.invalidate_user_cache(cache_identity)
        # Local import avoids coupling RetrieverService construction to write paths.
        from api.services.retriever import RetrieverService

        RetrieverService.invalidate_local_user_cache(cache_identity)

    async def _hard_delete_memory_and_reconcile_claims(self, memory: Memory) -> None:
        """Remove a memory and transactionally eliminate stale claim truth.

        This does not choose a new winner. A still-activated revision remains the winner;
        otherwise the claim is archived and cleared. Claims with no revisions are deleted.
        """
        claim_ids = list(
            (
                await self.session.execute(
                    select(MemoryClaimRevision.claim_id)
                    .where(MemoryClaimRevision.memory_id == memory.id)
                    .distinct()
                )
            ).scalars().all()
        )
        claims = []
        if claim_ids:
            claims = list(
                (
                    await self.session.execute(
                        select(MemoryClaim)
                        .where(MemoryClaim.id.in_(claim_ids))
                        .order_by(MemoryClaim.id)
                        .with_for_update()
                    )
                ).scalars().all()
            )

        await self.session.delete(memory)
        await self.session.flush()

        for claim in claims:
            revisions = list(
                (
                    await self.session.execute(
                        select(MemoryClaimRevision)
                        .where(MemoryClaimRevision.claim_id == claim.id)
                        .order_by(MemoryClaimRevision.created_at.desc())
                        .with_for_update()
                    )
                ).scalars().all()
            )
            if not revisions:
                await self.session.delete(claim)
                continue

            activated = [revision for revision in revisions if revision.status == "activated"]
            if len(activated) == 1:
                winner = activated[0]
                claim.status = "active"
                claim.active_value = winner.asserted_value
                claim.active_memory_id = winner.memory_id
                claim.winning_revision_id = winner.id
                claim.authority_priority = winner.authority_priority
                claim.confidence_score = winner.confidence_score
                claim.observed_at = winner.observed_at or claim.observed_at
            else:
                # Do not promote a superseded/disputed revision during privacy deletion.
                claim.status = "archived"
                claim.active_value = None
                claim.active_memory_id = None
                claim.winning_revision_id = None
            claim.updated_at = datetime.now(UTC)
            self.session.add(claim)

    async def get_job_status(self, *, job_id: str) -> dict[str, Any]:
        job_row = await self.session.get(ExtractionJob, uuid.UUID(job_id))
        if job_row is not None:
            return {
                "tenant_id": str(job_row.tenant_id),
                "proxy_user_id": str(job_row.proxy_user_id),
                "external_user_id": job_row.external_user_id,
                "job_id": str(job_row.id),
                "status": job_row.status.value,
                "memories_created": int(job_row.memories_created or 0),
                "pending_candidates_buffered": int((job_row.result or {}).get("pending_candidates_buffered", 0) or 0),
                "pending_candidates_promoted": int((job_row.result or {}).get("pending_candidates_promoted", 0) or 0),
                "attempts": int(job_row.attempts or 0),
                "max_attempts": int(job_row.max_attempts or DEFAULT_MAX_EXTRACTION_ATTEMPTS),
                "created_at": job_row.created_at.isoformat() if job_row.created_at else None,
                "processing_started_at": job_row.processing_started_at.isoformat()
                if job_row.processing_started_at
                else None,
                "queue_name": job_row.queue_name,
                "error": job_row.error,
                "error_summary": self._job_error_summary(job_row.status, job_row.error),
                "queued_at": job_row.queued_at.isoformat() if job_row.queued_at else None,
                "started_at": job_row.started_at.isoformat() if job_row.started_at else None,
                "completed_at": job_row.completed_at.isoformat() if job_row.completed_at else None,
                "dead_lettered_at": job_row.dead_lettered_at.isoformat() if job_row.dead_lettered_at else None,
                "extraction_metadata": (job_row.result or {}).get("extraction_metadata") or {},
            }
        cached_job = await self.cache_service.get_job_status(job_id)
        if cached_job is not None:
            return cached_job

        return {"job_id": job_id, "status": "unknown", "memories_created": 0}

    async def _create_extraction_job(self, job: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        tenant_id = job.get("tenant_id")
        proxy_user_id = job.get("proxy_user_id")
        external_user_id = job.get("external_user_id")
        if not tenant_id or not proxy_user_id or not external_user_id:
            return job, True
        source = dict(job.get("source") or {})
        try:
            source_event: MemorySourceEvent | None = None
            if source:
                source_event = MemorySourceEvent(
                    id=uuid.uuid4(),
                    tenant_id=uuid.UUID(str(tenant_id)),
                    proxy_user_id=uuid.UUID(str(proxy_user_id)),
                    writer_id=uuid.UUID(str(source["writer_id"])) if source.get("writer_id") else None,
                    api_key_id=uuid.UUID(str(source["api_key_id"])) if source.get("api_key_id") else None,
                    source_service=str(source["service"]),
                    source_event_id=str(source["event_id"]),
                    observed_at=datetime.fromisoformat(str(source["observed_at"]).replace("Z", "+00:00")),
                    payload_hash=str(source["payload_hash"]),
                    scope=dict(source.get("scope") or {}),
                    evidence_refs=list(source.get("evidence") or []),
                    processing_metadata={
                        "app_version": get_settings().app_version,
                        "schema_version": 1,
                        "policy_version": "provenance-phase2-v1",
                        "prompt_version": "general-extraction-v1",
                        "payload_hash_version": source.get("payload_hash_version"),
                    },
                )
                self.session.add(source_event)
                job["source_event_id"] = str(source_event.id)
            row = ExtractionJob(
                id=uuid.UUID(job["job_id"]),
                tenant_id=uuid.UUID(str(tenant_id)),
                proxy_user_id=uuid.UUID(str(proxy_user_id)),
                external_user_id=str(external_user_id),
                status=ExtractionJobStatus.queued,
                max_attempts=DEFAULT_MAX_EXTRACTION_ATTEMPTS,
                queue_name=str(job.get("queue_name")) if job.get("queue_name") else None,
                payload=job,
                payload_envelope=encrypt_json_for_dual_write(
                    tenant_id=str(tenant_id),
                    record_type="extraction-job-payload",
                    record_id=str(job["job_id"]),
                    value=job,
                ),
                result={},
                source_event_id=source_event.id if source_event is not None else None,
                raw_payload_expires_at=(
                    datetime.now(UTC) + timedelta(days=get_settings().extraction_payload_retention_days)
                ),
            )
            self.session.add(row)
            await self.session.commit()
            return job, True
        except IntegrityError:
            await self.session.rollback()
            if not source:
                raise
            existing_event = (
                await self.session.execute(
                    select(MemorySourceEvent).where(
                        MemorySourceEvent.tenant_id == uuid.UUID(str(tenant_id)),
                        MemorySourceEvent.source_service == str(source["service"]),
                        MemorySourceEvent.source_event_id == str(source["event_id"]),
                    )
                )
            ).scalar_one_or_none()
            if existing_event is None:
                raise
            if not source_event_payload_matches(
                existing_event=existing_event,
                messages=list(job.get("messages") or []),
                incoming_hash=str(source["payload_hash"]),
            ):
                raise APIError(
                    status_code=409,
                    code="PROV_409",
                    error="source_event_payload_mismatch",
                    details={
                        "service": source["service"],
                        "event_id": source["event_id"],
                    },
                )
            existing_job = (
                await self.session.execute(
                    select(ExtractionJob).where(ExtractionJob.source_event_id == existing_event.id)
                )
            ).scalar_one_or_none()
            if existing_job is None:
                raise
            return self._job_payload_with_live_status(existing_job), False

    @staticmethod
    def _job_payload_with_live_status(job: ExtractionJob) -> dict[str, Any]:
        payload = dict(job.payload or {})
        payload.update(
            {
                "job_id": str(job.id),
                "status": job.status.value,
                "memories_created": int(job.memories_created or 0),
                "pending_candidates_buffered": int((job.result or {}).get("pending_candidates_buffered", 0) or 0),
                "pending_candidates_promoted": int((job.result or {}).get("pending_candidates_promoted", 0) or 0),
                "attempts": int(job.attempts or 0),
                "max_attempts": int(job.max_attempts or DEFAULT_MAX_EXTRACTION_ATTEMPTS),
                "error_type": job.error_type,
            }
        )
        if job.error:
            payload["error"] = job.error
        return payload

    async def _mark_extraction_job_failed(
        self,
        *,
        job_id: str,
        error: str,
        error_type: str | None = None,
        status: ExtractionJobStatus,
    ) -> None:
        row = await self.session.get(ExtractionJob, uuid.UUID(job_id))
        if row is None:
            return
        row.status = status
        row.error = error
        row.error_type = error_type
        if status == ExtractionJobStatus.dead:
            row.dead_lettered_at = datetime.now(UTC)
        await self.session.commit()

    @staticmethod
    def _classify_job_error(error: str) -> str:
        normalized = str(error or "").lower()
        if "503" in str(error) or "service unavailable" in normalized:
            return "llm_provider_unavailable_503"
        if "429" in str(error) or "rate limit" in normalized or "quota" in normalized:
            return "llm_rate_limited_429"
        if "401" in str(error) or "403" in str(error) or "invalid api key" in normalized:
            return "llm_auth_failed"
        if "timeout" in normalized:
            return "timeout"
        if "connection" in normalized:
            return "connection_error"
        if "json" in normalized:
            return "llm_invalid_response"
        if "extraction_spec" in normalized:
            return "missing_extraction_spec"
        return "unknown_error"

    @staticmethod
    def _job_error_summary(status: ExtractionJobStatus | str, error: str | None) -> str | None:
        status_value = status.value if isinstance(status, ExtractionJobStatus) else str(status)
        if status_value == ExtractionJobStatus.dead.value:
            return "This extraction job failed multiple times and was marked dead. Please retry the job or contact support."
        if status_value == ExtractionJobStatus.failed.value:
            return "This extraction job failed and will be retried automatically."
        if not error:
            return None
        return "This extraction job encountered an internal processing error."

    async def _get_authorized_memory(
        self,
        *,
        authenticated_user_id: str | None,
        memory_id: str,
        tenant_id: str | None = None,
    ) -> Memory:
        if tenant_id:
            statement = (
                select(Memory)
                .join(ProxyUser, Memory.proxy_user_id == ProxyUser.id)
                .where(
                    Memory.id == uuid.UUID(memory_id),
                    ProxyUser.tenant_id == uuid.UUID(tenant_id),
                )
            )
            result = await self.session.execute(statement)
            memory = result.scalar_one_or_none()
        else:
            if not authenticated_user_id:
                raise APIError(status_code=401, code="AUTH_001", error="unauthorized")
            user = await resolve_authorized_user(
                self.session,
                requested_user_id=None,
                authenticated_user_id=authenticated_user_id,
            )
            memory = await self.session.get(Memory, uuid.UUID(memory_id))
            if memory is not None and memory.user_id != user.id:
                memory = None
        if memory is None:
            raise APIError(
                status_code=404,
                code="MEM_404",
                error="memory_not_found",
                details={"memory_id": memory_id},
            )
        return memory

    @staticmethod
    def _slice_with_cursor(items: list[Memory], *, cursor: str | None, limit: int) -> list[Memory]:
        if not cursor:
            return items[:limit]
        for index, item in enumerate(items):
            if str(item.id) == cursor:
                return items[index + 1 : index + 1 + limit]
        return items[:limit]

    @staticmethod
    def _cache_identity(memory: Memory) -> str:
        if memory.proxy_user_id is not None:
            return str(memory.proxy_user_id)
        return str(memory.user_id)

    async def _dispatch_extraction_job(self, job: dict[str, Any]) -> str | None:
        if self.dispatch_task is None:
            return None
        try:
            queue_name = job.get("queue_name")
            dispatched = await asyncio.to_thread(
                self.dispatch_task,
                EXTRACTION_TASK_NAME,
                args=[job],
                queue=queue_name,
            )
            if dispatched is not None and hasattr(dispatched, "__await__"):
                dispatched = await dispatched
            task_id = str(getattr(dispatched, "id", "") or "").strip()
            if task_id:
                try:
                    await self.session.execute(
                        update(ExtractionJob)
                        .where(
                            ExtractionJob.id == uuid.UUID(str(job["job_id"])),
                            ExtractionJob.status == ExtractionJobStatus.queued,
                            ExtractionJob.celery_task_id.is_(None),
                        )
                        .values(celery_task_id=task_id, updated_at=datetime.now(UTC))
                    )
                    await self.session.commit()
                except Exception:
                    await self.session.rollback()
                    LOGGER.exception(
                        "extraction_dispatch_task_id_persistence_failed job_id=%s task_id=%s",
                        job.get("job_id"),
                        task_id,
                    )
            return None
        except Exception:
            return "dispatch_failed"

    async def _embed_content(self, content: str) -> EmbeddingResult:
        try:
            return await self.embedding_service.embed(content)
        except Exception as exc:
            raise APIError(
                status_code=503,
                code="EMB_503",
                error="embedding_unavailable",
                details={"reason": str(exc)},
            ) from exc

    async def _embedding_collection_for_memory(self, memory: Memory) -> str | None:
        if not getattr(memory, "embedding_model_id", None):
            return None
        model = await self.session.get(EmbeddingModel, memory.embedding_model_id)
        return None if model is None else str(model.qdrant_collection)
