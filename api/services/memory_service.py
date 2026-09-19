from __future__ import annotations

import asyncio
import hashlib
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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import EmbeddingModel
from api.db.models import ExtractionJob
from api.db.models import ExtractionJobStatus
from api.db.models import ConversationEvidenceTurn
from api.db.models import MemoryProposal
from api.db.models import Memory
from api.db.models import MemoryClaim
from api.db.models import MemoryClaimRevision
from api.db.models import MemorySourceEvent
from api.db.models import ProxyUser
from api.db.vector_store import QdrantService
from api.errors import APIError
from api.services.embedding_service import EmbeddingResult
from api.services.embedding_service import EmbeddingService
from api.services.evidence_policy import authority_for_submission
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


def _turn_id_for_job(*, job_id: str, index: int, message: dict[str, Any]) -> str:
    """Return the durable identifier recorded for a transcript turn."""

    external_turn_id = str(message.get("external_turn_id") or "").strip()
    if external_turn_id:
        return f"external:{external_turn_id}"
    return f"job:{job_id}:turn:{index}"


def _prepare_transcript_turns(*, job_id: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve supplied turn IDs and mint retry-stable IDs for legacy callers."""

    prepared: list[dict[str, Any]] = []
    for index, original in enumerate(messages):
        message = dict(original)
        message["turn_id"] = _turn_id_for_job(job_id=job_id, index=index, message=message)
        # The memory provenance stores this digest rather than duplicating
        # raw customer text for every cited turn.
        message["turn_content_sha256"] = hashlib.sha256(
            str(message.get("content") or "").encode("utf-8")
        ).hexdigest()
        prepared.append(message)
    return prepared


def _compact_task_payload(job: dict[str, Any]) -> dict[str, Any]:
    """Avoid copying committed tenant transcripts through broker messages."""

    if job.get("tenant_id") and job.get("proxy_user_id"):
        return {
            "job_id": str(job["job_id"]),
            "queue_name": job.get("queue_name"),
            "_payload_reference": "extraction_job",
        }
    return job


def _job_status_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    """Keep transient status records small and free of transcript content."""

    fields = (
        "tenant_id", "proxy_user_id", "external_user_id", "job_id", "status",
        "memories_created", "pending_candidates_buffered", "pending_candidates_promoted",
        "attempts", "max_attempts", "queue_name", "plan_tier", "error", "error_type",
        "queued_at", "created_at", "processing_started_at", "started_at", "completed_at",
        "dead_lettered_at", "extraction_metadata", "operational_metrics", "proposal_ids",
    )
    snapshot = {field: job[field] for field in fields if field in job}
    stored_memories = list(job.get("stored_memories") or [])
    if stored_memories:
        snapshot["result_memory_ids"] = [
            str(memory["id"])
            for memory in stored_memories
            if isinstance(memory, dict) and memory.get("id")
        ]
    return snapshot


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
        messages: list[dict[str, Any]],
        metadata: dict[str, Any],
        idempotency_key: str | None,
        tenant_id: str | None = None,
        external_user_id: str | None = None,
        proxy_user_id: str | None = None,
        api_key_id: str | None = None,
        source: dict[str, Any] | None = None,
        evidence_mode: str = "conversation_evidence",
        conversation_id: str | None = None,
        trusted_submission_kind: str | None = None,
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

        job_id = str(uuid.uuid4())
        job = {
            "job_id": job_id,
            "status": "queued",
            "memories_created": 0,
            "proxy_user_id": resolved_proxy_user_id,
            "tenant_id": tenant_id,
            "external_user_id": external_user_id,
            "agent_id": agent_id,
            "message_count": len(messages),
            "messages": _prepare_transcript_turns(job_id=job_id, messages=messages),
            "metadata": metadata,
            "queued_at": datetime.now(UTC).isoformat(),
        }
        if conversation_id:
            job["external_conversation_id"] = conversation_id
        evidence_authority = authority_for_submission(mode=evidence_mode)
        job["evidence_policy"] = {
            "mode": evidence_mode,
            "authority_priority": int(evidence_authority),
            "authority_rules": {"default_priority": int(evidence_authority)},
            "attestation": (
                "client_asserted"
                if evidence_mode == "client_assertion"
                else "legacy_conversation"
            ),
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
            # This flag is server-only: the public MCP request cannot choose a
            # service label, attestation, or authority.  The source event is
            # therefore the authority basis, accurately classified as an MCP
            # client assertion rather than inheriting the API-key fallback.
            trusted_evidence_policy: dict[str, Any] | None = None
            if trusted_submission_kind == "mcp_client_assertion":
                normalized_source = {
                    **normalized_source,
                    "service": "memoryos-mcp",
                    "event_id": job["job_id"],
                }
                trusted_evidence_policy = {
                    "attestation": "client_asserted",
                    "authority_priority": int(evidence_authority),
                    "authority_rules": {"default_priority": int(evidence_authority)},
                }
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
            if trusted_evidence_policy is not None:
                job["source"]["trusted_evidence_policy"] = trusted_evidence_policy
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
        try:
            persisted_job, created = await self._create_extraction_job(job)
        except BaseException:
            await self.session.rollback()
            if tenant_id and job.get("queue_name"):
                await self.queue_router.release_extraction_slot(
                    tenant_id=tenant_id,
                    queue_name=str(job["queue_name"]),
                    job_id=job["job_id"],
                )
            raise
        if not created:
            if tenant_id and job.get("queue_name"):
                await self.queue_router.release_extraction_slot(
                    tenant_id=tenant_id,
                    queue_name=str(job["queue_name"]),
                    job_id=job["job_id"],
                )
            return persisted_job
        await self.cache_service.set_job_status(job["job_id"], _job_status_snapshot(job), ttl=3600)
        if idempotency_key:
            await self.cache_service.set_idempotent_response(
                idempotency_key,
                _job_status_snapshot(job),
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
            await self.cache_service.set_job_status(job["job_id"], _job_status_snapshot(job), ttl=3600)
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
        external_user_id: str | None = None,
    ) -> Memory:
        memory = await self._get_authorized_memory(
            authenticated_user_id=authenticated_user_id,
            memory_id=memory_id,
            tenant_id=tenant_id,
            external_user_id=external_user_id,
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
        external_user_id: str | None = None,
    ) -> Memory:
        memory = await self._get_authorized_memory(
            authenticated_user_id=authenticated_user_id,
            memory_id=memory_id,
            tenant_id=tenant_id,
            external_user_id=external_user_id,
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
        external_user_id: str | None = None,
    ) -> bool:
        memory = await self._get_authorized_memory(
            authenticated_user_id=authenticated_user_id,
            memory_id=memory_id,
            tenant_id=tenant_id,
            external_user_id=external_user_id,
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
            stored_memories = list((job_row.result or {}).get("stored_memories") or [])
            result_memory_ids = [
                str(memory["id"])
                for memory in stored_memories
                if isinstance(memory, dict) and memory.get("id")
            ]
            return {
                "tenant_id": str(job_row.tenant_id),
                "proxy_user_id": str(job_row.proxy_user_id),
                "external_user_id": job_row.external_user_id,
                "job_id": str(job_row.id),
                "status": job_row.status.value,
                "memories_created": int(job_row.memories_created or 0),
                "result_memory_ids": result_memory_ids,
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
                "proposal_ids": [str(item) for item in (job_row.payload or {}).get("proposal_ids", [])],
                "operational_metrics": (job_row.result or {}).get("operational_metrics") or {},
            }
        cached_job = await self.cache_service.get_job_status(job_id)
        if cached_job is not None:
            return cached_job

        return {"job_id": job_id, "status": "unknown", "memories_created": 0}

    @staticmethod
    def _conversation_scope_id(job: dict[str, Any]) -> str:
        external_id = str(job.get("external_conversation_id") or "").strip()
        return f"external:{external_id}" if external_id else f"job:{job['job_id']}"

    @staticmethod
    def _occurred_at(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    async def _persist_evidence_ledger(
        self,
        *,
        job: dict[str, Any],
        tenant_id: str,
        proxy_user_id: str,
    ) -> list[str]:
        """Persist immutable turn facts and explicitly marked assistant proposals."""

        tenant_uuid = uuid.UUID(str(tenant_id))
        proxy_uuid = uuid.UUID(str(proxy_user_id))
        job_uuid = uuid.UUID(str(job["job_id"]))
        scope_id = self._conversation_scope_id(job)
        proposal_ids: list[str] = []
        proposal_messages = [
            message for message in list(job.get("messages") or [])
            if bool(message.get("is_memory_proposal"))
        ]
        proposal_turn_ids = {
            str(message.get("turn_id") or "").strip()
            for message in proposal_messages
            if str(message.get("turn_id") or "").strip()
        }
        existing_proposals = {
            proposal.assistant_turn_id: proposal
            for proposal in (
                (
                    await self.session.execute(
                        select(MemoryProposal).where(
                            MemoryProposal.tenant_id == tenant_uuid,
                            MemoryProposal.proxy_user_id == proxy_uuid,
                            MemoryProposal.conversation_scope_id == scope_id,
                            MemoryProposal.assistant_turn_id.in_(proposal_turn_ids),
                        )
                    )
                ).scalars().all()
                if proposal_turn_ids
                else []
            )
        }
        new_proposal_turn_ids = proposal_turn_ids - existing_proposals.keys()
        if new_proposal_turn_ids:
            # Serialize proposal-window replacement for this user. Without the
            # row lock, two concurrent jobs can each supersede the old group
            # and then both insert a new active group.
            locked_proxy_user = (
                await self.session.execute(
                    select(ProxyUser.id)
                    .where(
                        ProxyUser.id == proxy_uuid,
                        ProxyUser.tenant_id == tenant_uuid,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked_proxy_user is None:
                raise APIError(status_code=404, code="USR_404", error="proxy_user_not_found")
            await self.session.execute(
                update(MemoryProposal)
                .where(
                    MemoryProposal.tenant_id == tenant_uuid,
                    MemoryProposal.proxy_user_id == proxy_uuid,
                    MemoryProposal.conversation_scope_id == scope_id,
                    MemoryProposal.status == "active",
                    MemoryProposal.extraction_job_id != job_uuid,
                )
                .values(status="superseded", resolved_at=datetime.now(UTC))
            )
        proposal_ordinal = 0
        for message in list(job.get("messages") or []):
            turn_id = str(message.get("turn_id") or "").strip()
            content_sha256 = str(message.get("turn_content_sha256") or "").strip()
            if not turn_id or len(content_sha256) != 64:
                raise APIError(status_code=400, code="EVID_400", error="invalid_evidence_turn")
            role = str(message.get("role") or "").lower()
            source_kind = str(message.get("source_kind") or "").lower() or None
            turn_values = {
                "tenant_id": tenant_uuid,
                "proxy_user_id": proxy_uuid,
                "extraction_job_id": job_uuid,
                "conversation_scope_id": scope_id,
                "turn_id": turn_id,
                "role": role,
                "source_kind": source_kind,
                "content_sha256": content_sha256,
                "occurred_at": self._occurred_at(message.get("occurred_at")),
            }
            inserted_turn = await self.session.execute(
                pg_insert(ConversationEvidenceTurn)
                .values(**turn_values)
                .on_conflict_do_nothing(constraint="uq_conversation_evidence_turn_scope")
                .returning(ConversationEvidenceTurn.id)
            )
            if inserted_turn.scalar_one_or_none() is None:
                existing_turn = (
                    await self.session.execute(
                        select(ConversationEvidenceTurn).where(
                            ConversationEvidenceTurn.tenant_id == tenant_uuid,
                            ConversationEvidenceTurn.proxy_user_id == proxy_uuid,
                            ConversationEvidenceTurn.conversation_scope_id == scope_id,
                            ConversationEvidenceTurn.turn_id == turn_id,
                        )
                    )
                ).scalar_one()
                if (
                    existing_turn.role != role
                    or existing_turn.source_kind != source_kind
                    or existing_turn.content_sha256 != content_sha256
                ):
                    raise APIError(
                        status_code=409,
                        code="EVID_409",
                        error="evidence_turn_payload_mismatch",
                        details={"turn_id": turn_id},
                    )

            if not bool(message.get("is_memory_proposal")):
                continue
            if role != "assistant" or source_kind != "assistant_output":
                raise APIError(status_code=400, code="PROP_400", error="invalid_memory_proposal")
            existing_proposal = existing_proposals.get(turn_id)
            if existing_proposal is not None:
                if existing_proposal.assistant_content_sha256 != content_sha256:
                    raise APIError(
                        status_code=409,
                        code="PROP_409",
                        error="proposal_turn_payload_mismatch",
                        details={"turn_id": turn_id},
                    )
                proposal_ids.append(str(existing_proposal.id))
                continue

            proposal_ordinal += 1
            proposal_values = {
                "tenant_id": tenant_uuid,
                "proxy_user_id": proxy_uuid,
                "extraction_job_id": job_uuid,
                "conversation_scope_id": scope_id,
                "proposal_group_id": str(job_uuid),
                "proposal_ordinal": proposal_ordinal,
                "assistant_turn_id": turn_id,
                "assistant_content_sha256": content_sha256,
                "status": "active",
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
            }
            inserted_proposal = await self.session.execute(
                pg_insert(MemoryProposal)
                .values(**proposal_values)
                .on_conflict_do_nothing(constraint="uq_memory_proposals_assistant_turn")
                .returning(MemoryProposal.id)
            )
            proposal_id = inserted_proposal.scalar_one_or_none()
            if proposal_id is None:
                existing_proposal = (
                    await self.session.execute(
                        select(MemoryProposal).where(
                            MemoryProposal.tenant_id == tenant_uuid,
                            MemoryProposal.proxy_user_id == proxy_uuid,
                            MemoryProposal.conversation_scope_id == scope_id,
                            MemoryProposal.assistant_turn_id == turn_id,
                        )
                    )
                ).scalar_one()
                if existing_proposal.assistant_content_sha256 != content_sha256:
                    raise APIError(
                        status_code=409,
                        code="PROP_409",
                        error="proposal_turn_payload_mismatch",
                        details={"turn_id": turn_id},
                    )
                if (
                    existing_proposal.proposal_group_id != str(job_uuid)
                    or existing_proposal.proposal_ordinal != proposal_ordinal
                ):
                    raise APIError(
                        status_code=409,
                        code="PROP_409",
                        error="proposal_order_mismatch",
                        details={"turn_id": turn_id},
                    )
                proposal_id = existing_proposal.id
            proposal_ids.append(str(proposal_id))
        return proposal_ids
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
                        **(
                            {"trusted_evidence_policy": dict(source["trusted_evidence_policy"])}
                            if isinstance(source.get("trusted_evidence_policy"), dict)
                            else {}
                        ),
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
                payload=dict(job),
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
            await self.session.flush()
            proposal_ids = await self._persist_evidence_ledger(
                job=job,
                tenant_id=str(tenant_id),
                proxy_user_id=str(proxy_user_id),
            )
            if proposal_ids:
                job["proposal_ids"] = proposal_ids
                row.payload = dict(job)
                row.payload_envelope = encrypt_json_for_dual_write(
                    tenant_id=str(tenant_id),
                    record_type="extraction-job-payload",
                    record_id=str(job["job_id"]),
                    value=job,
                )
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
        external_user_id: str | None = None,
    ) -> Memory:
        if tenant_id:
            statement = (
                select(Memory)
                .join(ProxyUser, Memory.proxy_user_id == ProxyUser.id)
                .where(
                    Memory.id == uuid.UUID(memory_id),
                    ProxyUser.tenant_id == uuid.UUID(tenant_id),
                    *(
                        [ProxyUser.external_user_id == external_user_id]
                        if external_user_id is not None
                        else []
                    ),
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
                args=[_compact_task_payload(job)],
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
