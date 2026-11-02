"""Target-bound resolution of retained registered-service corrections.

No semantic matching, source impersonation or confidence-based authority. Provider
work (embedding only) precedes locks. The final SQL transaction revalidates the
complete preview, writes history/ledger/outbox/audit and closes the candidate.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import (
    ApiKey,
    AuditAction,
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
    PendingExtractionCandidate,
    ProxyUser,
    ServiceWriter,
    Tenant,
    User,
)
from api.errors import APIError
from api.infra.protected_storage import encrypt_text_for_dual_write
from api.schemas.service_corrections import (
    ServiceCorrectionDecision,
    ServiceCorrectionItem,
    ServiceCorrectionPreview,
    ServiceCorrectionResult,
)
from api.services.claim_ledger_service import ClaimLedgerService, authority_priority
from api.services.claim_versions import supports_claim_schema
from api.services.embedding_service import EmbeddingService
from api.services.preference_claim_service import preference_declaration
from api.services.provenance_service import build_provenance_snapshot
from api.services.retriever import source_winner_matches
from api.services.source_review_service import target_version
from api.services.temporal_validity import temporal_validity_from_provenance
from api.services.vector_outbox import (
    build_vector_payload,
    enqueue_vector_archive,
    enqueue_vector_upsert,
)
from api.services.version_service import VersionService


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def conflict(reason):
    raise APIError(status_code=409, code="SCR_409", error=reason)


def one(session, model, *conditions, lock=False):
    query = select(model).where(*conditions).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update(nowait=True)
    return session.execute(query).scalar_one_or_none()


def authorize(session, tenant_id, actor_id, external_user_id, *, lock=False):
    # Recheck SQL: cached middleware permissions cannot authorize this operation.
    key = one(session, ApiKey, ApiKey.id == actor_id, ApiKey.tenant_id == tenant_id, lock=lock)
    if key is None or not key.is_active or "admin" not in key.permissions:
        raise APIError(status_code=403, code="AUTH_403", error="active_tenant_admin_api_key_required")
    tenant = one(session, Tenant, Tenant.id == tenant_id, Tenant.is_active.is_(True), lock=lock)
    if tenant is None:
        raise APIError(status_code=403, code="AUTH_403", error="active_tenant_admin_api_key_required")
    owner = one(session, ProxyUser, ProxyUser.tenant_id == tenant_id,
        ProxyUser.external_user_id == external_user_id, lock=lock)
    if owner is None or owner.is_blocked:
        raise APIError(status_code=404, code="SCR_404", error="service_correction_not_found")
    return owner, key


def candidate_for(session, tenant_id, owner_id, candidate_id, *, lock=False):
    candidate = one(session, PendingExtractionCandidate, PendingExtractionCandidate.id == candidate_id,
        PendingExtractionCandidate.tenant_id == tenant_id,
        PendingExtractionCandidate.proxy_user_id == owner_id, lock=lock)
    evidence = (candidate.metadata_json or {}).get("extraction_evidence", {}) if candidate else {}
    reasons = evidence.get("source_decision", {}).get("reason_codes", [])
    if (candidate is None or candidate.candidate_reason != "source_decision_pending"
        or evidence.get("claim_state") != "correction" or "source_correction_unresolved" not in reasons
        or evidence.get("grounding_mode") == "verified_source_spans"
        or preference_declaration({"extraction_evidence": evidence}) is not None):
        raise APIError(status_code=404, code="SCR_404", error="service_correction_not_found")
    return candidate, evidence


def load_preview(session, *, tenant_id, actor_id, external_user_id, candidate_id, target_id, lock=False):
    owner, actor = authorize(session, tenant_id, actor_id, external_user_id, lock=lock)
    candidate, evidence = candidate_for(session, tenant_id, owner.id, candidate_id, lock=lock)
    if candidate.status != "pending":
        conflict("service_correction_closed")
    event = one(session, MemorySourceEvent, MemorySourceEvent.id == candidate.source_event_id,
        MemorySourceEvent.tenant_id == tenant_id, MemorySourceEvent.proxy_user_id == owner.id, lock=lock)
    if event is None or event.writer_id is None:
        conflict("service_correction_source_unavailable")
    if (event.processing_metadata or {}).get("trusted_evidence_policy"):
        conflict("service_correction_attestation_not_supported")
    writer = one(session, ServiceWriter, ServiceWriter.id == event.writer_id,
        ServiceWriter.tenant_id == tenant_id, lock=lock)
    binding = evidence.get("pending_source_binding_v1")
    if (writer is None or writer.service_key != event.source_service or not isinstance(binding, dict)
        or digest(binding) != digest({"writer_id": str(event.writer_id),
            "source_event_id": str(event.id), "scope": event.scope})):
        conflict("service_correction_source_binding_changed")
    job = one(session, ExtractionJob, ExtractionJob.id == candidate.extraction_job_id,
        ExtractionJob.tenant_id == tenant_id, ExtractionJob.proxy_user_id == owner.id,
        ExtractionJob.source_event_id == event.id, lock=lock)
    if job is None or job.status != ExtractionJobStatus.completed:
        conflict("service_correction_job_not_completed")
    source_key = one(session, ApiKey, ApiKey.id == event.api_key_id,
        ApiKey.tenant_id == tenant_id, lock=lock) if event.api_key_id else None
    target = one(session, Memory, Memory.id == target_id, Memory.proxy_user_id == owner.id, lock=lock)
    if target is None:
        raise APIError(status_code=404, code="SCR_404", error="service_correction_target_not_found")
    user = one(session, User, User.id == target.user_id, User.is_active.is_(True),
        User.external_id == f"proxy::{owner.id}", lock=lock)
    claim = one(session, MemoryClaim, MemoryClaim.tenant_id == tenant_id,
        MemoryClaim.proxy_user_id == owner.id, MemoryClaim.active_memory_id == target.id, lock=lock)
    revision = one(session, MemoryClaimRevision, MemoryClaimRevision.id == claim.winning_revision_id,
        lock=lock) if claim else None
    target_source = one(session, MemorySourceEvent, MemorySourceEvent.id == target.source_event_id,
        MemorySourceEvent.tenant_id == tenant_id, MemorySourceEvent.proxy_user_id == owner.id, lock=lock)
    provenance = (target.metadata_json or {}).get("provenance") or {}
    if (user is None or target.agent_id is not None or preference_declaration(provenance) is not None
        or not source_winner_matches(claim, target, revision) or not supports_claim_schema(revision.schema_version)
        or target_source is None or target_source.writer_id is None
        or revision.asserted_value != target.content.strip()
        or revision.authority_priority != claim.authority_priority
        or authority_priority(provenance, candidate.category.value) != claim.authority_priority
        or target.category != candidate.category or claim.category != candidate.category
        or digest(event.scope) != digest(target_source.scope) or digest(event.scope) != digest(claim.scope)
        or digest(event.scope) != digest(provenance.get("scope"))):
        conflict("service_correction_target_not_current_or_scope_mismatch")
    now = datetime.now(UTC)
    if (any(value is not None and utc(value) <= now for value in (target.expires_at, target.effective_until))
        or (target.effective_from is not None and utc(target.effective_from) > now)):
        conflict("service_correction_target_not_current")
    # This pending candidate was never an accepted revision: apply uses current
    # registered writer policy, not an invented historical authority snapshot.
    incoming = build_provenance_snapshot(event)
    incoming["authority_rules"] = deepcopy(writer.authority_rules)
    incoming["authority_priority"] = None
    incoming["resolution_authority_basis"] = "current_writer_rules"
    category = candidate.category.value
    priority = authority_priority(incoming, category)
    blockers = []
    if not writer.is_active:
        blockers.append("source_disabled")
    if (source_key is None or not source_key.is_active or not {"write", "admin"}.intersection(source_key.permissions)
        or (writer.api_key_id is not None and writer.api_key_id != source_key.id)):
        blockers.append("source_credential_unavailable")
    if priority < claim.authority_priority:
        blockers.append("lower_authority_source")
    if (claim.observed_at is None or utc(event.observed_at) < utc(claim.observed_at)
        or utc(event.observed_at) > now):
        blockers.append("source_observation_not_current")
    if candidate.expires_at is not None:
        # Pending-row retention is not proof of the successor's truth lifetime.
        blockers.append("candidate_retention_not_supported")
    try:
        start, end = temporal_validity_from_provenance(incoming)
        if (start is not None and start > now) or (end is not None and end <= now):
            blockers.append("source_validity_not_current")
    except ValueError:
        blockers.append("source_validity_invalid")
    target_token = target_version(target)
    version = digest({"candidate": {"id": candidate.id, "content": candidate.content,
        "category": category, "importance": candidate.importance_score, "confidence": candidate.confidence_score,
        "reasoning": candidate.reasoning, "metadata": candidate.metadata_json, "expires_at": candidate.expires_at},
        "source": incoming, "source_key": {"id": source_key.id, "active": source_key.is_active,
            "permissions": source_key.permissions} if source_key else None,
        "writer": {"id": writer.id, "active": writer.is_active, "key": writer.api_key_id},
        "target": target_token, "claim": {"id": claim.id, "status": claim.status,
            "revision": claim.winning_revision_id, "priority": claim.authority_priority,
            "observed_at": claim.observed_at, "value": claim.active_value}, "blockers": blockers})
    view = ServiceCorrectionPreview(candidate_id=candidate.id, candidate_content=candidate.content,
        category=category, scope=event.scope, source_event_id=event.id, source_service=event.source_service,
        source_external_event_id=event.source_event_id, observed_at=event.observed_at,
        target_memory_id=target.id, target_content=target.content, target_version=target_token,
        claim_id=claim.id, winning_revision_id=claim.winning_revision_id,
        incoming_priority=priority, target_priority=claim.authority_priority, review_version=version,
        actions=["decline"] if blockers else ["apply", "decline"], apply_blockers=blockers)
    return view, candidate, target, incoming, owner, actor


def request_digest(payload):
    return digest(payload.model_dump(mode="json"))


def replay(session, *, tenant_id, actor_id, candidate_id, payload, lock=False):
    owner, _ = authorize(session, tenant_id, actor_id, payload.external_user_id, lock=lock)
    candidate, _ = candidate_for(session, tenant_id, owner.id, candidate_id, lock=lock)
    if candidate.status == "pending":
        return None
    receipt = (candidate.metadata_json or {}).get("service_correction_resolution") or {}
    if receipt.get("request_sha256") != request_digest(payload) or not receipt.get("result"):
        conflict("service_correction_closed_or_binding_changed")
    return ServiceCorrectionResult(**dict(receipt["result"], replayed=True))


def decide(session, *, tenant_id, actor_id, candidate_id, payload, embedding):
    previous = replay(session, tenant_id=tenant_id, actor_id=actor_id, candidate_id=candidate_id,
        payload=payload, lock=True)
    if previous is not None:
        return previous
    view, candidate, target, incoming, owner, actor = load_preview(session, tenant_id=tenant_id,
        actor_id=actor_id, external_user_id=payload.external_user_id, candidate_id=candidate_id,
        target_id=payload.target_memory_id, lock=True)
    if view.review_version != payload.review_version or view.target_version != payload.target_version:
        conflict("service_correction_preview_stale")
    if payload.action not in view.actions:
        conflict("service_correction_apply_not_permitted")
    claim_id, revision_id, memory_id = view.claim_id, view.winning_revision_id, target.id
    decision = {"action": "UPDATE" if payload.action == "apply" else "REJECT",
        "decision_level": "operator", "reason_codes": ["explicit_service_correction", payload.action],
        "details": {"candidate_id": str(candidate.id), "target_memory_id": str(target.id),
            "target_version": payload.target_version, "actor_api_key_id": str(actor.id),
            "authority_basis": "current_writer_rules"}}
    if payload.action == "apply":
        model = one(session, EmbeddingModel, EmbeddingModel.id == target.embedding_model_id, lock=True)
        if (embedding is None or model is None or not model.is_active or embedding.model_id != model.id
            or embedding.dimensions != model.dimensions or len(embedding.vector) != model.dimensions
            or embedding.qdrant_collection != model.qdrant_collection
            or not all(math.isfinite(v) for v in embedding.vector)):
            conflict("service_correction_embedding_invalid")
        new_id = uuid.uuid4()
        incoming = deepcopy(incoming)
        incoming["extraction_evidence"] = deepcopy(candidate.metadata_json["extraction_evidence"])
        incoming["service_correction_resolution"] = decision
        start, end = temporal_validity_from_provenance(incoming)
        conversation = Conversation(id=uuid.uuid4(), user_id=target.user_id, message_count=0,
            processing_status=ConversationProcessingStatus.done)
        session.add(conversation)
        session.flush()
        successor = Memory(id=new_id, user_id=target.user_id, proxy_user_id=owner.id,
            source_event_id=candidate.source_event_id, source_conversation_id=conversation.id,
            content=candidate.content, content_envelope=encrypt_text_for_dual_write(tenant_id=str(tenant_id),
                record_type="memory-content", record_id=str(new_id), value=candidate.content),
            category=candidate.category, importance_score=candidate.importance_score,
            confidence_score=candidate.confidence_score, embedding_id=str(new_id), embedding_model_id=model.id,
            previous_version_id=target.id, effective_from=start, effective_until=end, is_archived=False,
            metadata_json={"provenance": incoming, "resolution": "UPDATE", "decision_evidence": decision,
                "reasoning": candidate.reasoning, "expiry": "permanent"})
        VersionService(session).record_version(target, "conflict_resolved", "Explicit service correction", "operator")
        target.is_archived = True
        enqueue_vector_archive(session, memory_id=target.id, payload={**build_vector_payload(target,
            tenant_id=str(tenant_id), proxy_user_id=str(owner.id), qdrant_collection=model.qdrant_collection),
            "lifecycle_state": "superseded"})
        session.add(successor)
        session.flush()
        VersionService(session).record_version(successor, "created", "Explicit service correction", "operator")
        claim = ClaimLedgerService(session).record_memory(successor, tenant_id=tenant_id,
            proxy_user_id=owner.id, provenance=incoming, resolution="UPDATE", decision_evidence=decision,
            predecessor_memory_id=target.id)
        if claim is None or claim.id != view.claim_id or claim.active_memory_id != successor.id:
            conflict("service_correction_ledger_not_activated")
        enqueue_vector_upsert(session, memory_id=successor.id, embedding=embedding.vector,
            payload=build_vector_payload(successor, tenant_id=str(tenant_id), proxy_user_id=str(owner.id),
                qdrant_collection=model.qdrant_collection))
        memory_id, claim_id, revision_id = successor.id, claim.id, claim.winning_revision_id
    candidate.status = "promoted" if payload.action == "apply" else "dismissed"
    result = ServiceCorrectionResult(candidate_id=candidate.id, action=payload.action,
        candidate_status=candidate.status, memory_id=memory_id, claim_id=claim_id,
        winning_revision_id=revision_id, indexing_state="pending" if payload.action == "apply" else "unchanged")
    candidate.metadata_json = {**deepcopy(candidate.metadata_json), "service_correction_resolution": {
        "request_sha256": request_digest(payload), "actor_api_key_id": str(actor.id),
        "decided_at": datetime.now(UTC).isoformat(), "result": result.model_dump(mode="json")}}
    candidate.updated_at = datetime.now(UTC)
    session.add(AuditLog(id=uuid.uuid4(), user_id=target.user_id, proxy_user_id=owner.id,
        memory_id=memory_id, action=AuditAction.updated,
        old_value={"candidate_id": str(candidate.id), "status": "pending", "target_memory_id": str(target.id)},
        new_value={"service_correction": result.model_dump(mode="json"), "actor_api_key_id": str(actor.id),
            "review_version": payload.review_version, "target_version": payload.target_version}))
    session.flush()
    return result


class ServiceCorrectionService:
    def __init__(self, session: AsyncSession, *, embedder=None):
        self.session = session
        self.embedder = embedder

    async def preview(self, **binding):
        return (await self.session.run_sync(lambda s: load_preview(s, **binding)))[0]

    async def resolve(self, *, tenant_id, actor_id, candidate_id, payload: ServiceCorrectionDecision):
        binding = {"tenant_id": tenant_id, "actor_id": actor_id, "candidate_id": candidate_id}
        try:
            if self.session.new or self.session.dirty or self.session.deleted:
                raise ValueError("service correction requires a clean request session")
            previous = await self.session.run_sync(lambda s: replay(s, **binding, payload=payload))
            if previous is not None:
                return previous
            view, candidate, target, *_ = await self.session.run_sync(lambda s: load_preview(s,
                **binding, external_user_id=payload.external_user_id, target_id=payload.target_memory_id))
            if view.review_version != payload.review_version or view.target_version != payload.target_version:
                conflict("service_correction_preview_stale")
            if payload.action not in view.actions:
                conflict("service_correction_apply_not_permitted")
            text, model_id = candidate.content, target.embedding_model_id
            await self.session.rollback()  # Provider work never holds SQL row locks.
            embedding = None
            if payload.action == "apply":
                embed = self.embedder or EmbeddingService(async_session=self.session).embed
                embedding = await embed(text, model_id=model_id, tenant_id=str(tenant_id))
            await self.session.rollback()  # Refresh any embedding-model read transaction too.
            result = await self.session.run_sync(lambda s: decide(s, **binding, payload=payload, embedding=embedding))
            await self.session.commit()
            return result
        except BaseException as error:
            await self.session.rollback()
            if isinstance(error, DBAPIError) and getattr(error.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
                conflict("service_correction_busy_retry")
            raise

    async def list_pending(self, *, tenant_id, actor_id, external_user_id, limit=20, after=None):
        def read(session):
            owner, _ = authorize(session, tenant_id, actor_id, external_user_id)
            evidence = PendingExtractionCandidate.metadata_json["extraction_evidence"]
            query = select(PendingExtractionCandidate).join(MemorySourceEvent,
                MemorySourceEvent.id == PendingExtractionCandidate.source_event_id).where(
                PendingExtractionCandidate.tenant_id == tenant_id,
                PendingExtractionCandidate.proxy_user_id == owner.id,
                PendingExtractionCandidate.status == "pending",
                PendingExtractionCandidate.candidate_reason == "source_decision_pending",
                evidence["claim_state"].astext == "correction",
                evidence["source_decision"]["reason_codes"].contains(["source_correction_unresolved"]),
                MemorySourceEvent.tenant_id == tenant_id, MemorySourceEvent.proxy_user_id == owner.id,
                MemorySourceEvent.writer_id.is_not(None))
            if after is not None:
                query = query.where(PendingExtractionCandidate.id > after)
            rows = session.scalars(query.order_by(PendingExtractionCandidate.id).limit(limit + 1)).all()
            return [ServiceCorrectionItem(id=c.id, content=c.content,
                category=c.category.value, created_at=c.created_at) for c in rows[:limit]], (
                rows[limit - 1].id if len(rows) > limit else None)
        return await self.session.run_sync(read)
