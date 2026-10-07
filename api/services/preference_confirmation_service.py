"""Review-only question proposals and explicit application-user-action commands.

Never promote source-review text. Confirmation stays authority 20 and reuses
the normal preference writer/outbox. The caller owns the SQL transaction.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import dataclass
import json
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from api.db.models import (
    AuditAction, AuditLog, ExtractionJob, Memory, MemoryClaim, MemoryClaimRevision,
    MemorySourceEvent, PendingExtractionCandidate, ProxyUser, Tenant,
)
from api.errors import APIError
from api.schemas.memory_schemas import ExtractedMemory
from api.schemas.preferences import PreferenceApplicability, PreferenceClaimRequest, PropertyKey
from api.schemas.responses import PreferenceConfirmationReview, PreferenceReviewAnswerData
from api.services.extraction_service import ExtractionService
from api.services.preference_claim_service import PREFERENCE_MARKER, preference_fingerprint
from api.services.preference_intent_admission_service import _hash
from api.services.preference_snapshot_service import identity_binding_version, snapshot_target
from api.settings import get_settings

REASON = "preference_confirmation_pending"
TTL = timedelta(days=7)
MAX_RETURNED = 3
MAX_READ_HOLDS = 8


def confirmation_predicate():
    return text("pending_extraction_candidates.candidate_reason = 'preference_confirmation_pending'")


@dataclass(frozen=True)
class ConfirmedPreferenceCommand:
    """Internal object only. Never deserialized from model/client JSON."""
    tenant_id: str
    proxy_user_id: str
    conversation_id: str
    event_id: uuid.UUID
    review_id: str
    review_version: str
    declaration: PreferenceClaimRequest

    def matches(self, *, memory, tenant_id, proxy_user_id, conversation_id, event_id):
        confirmation = memory.validated_evidence.get("preference_confirmation") or {}
        return (self.tenant_id == tenant_id and self.proxy_user_id == proxy_user_id
            and self.conversation_id == str(conversation_id) and self.event_id == event_id
            and memory.content == self.declaration.quote
            and memory.validated_evidence.get(PREFERENCE_MARKER) == self.declaration.model_dump(mode="json", exclude_none=True)
            and confirmation.get("review_id") == self.review_id and confirmation.get("version") == self.review_version
            and confirmation.get("event_id") == str(self.event_id))


class QuestionReviewSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: str = "preference_question_confirmation_v1"
    tenant_id: uuid.UUID
    proxy_user_id: uuid.UUID
    source_conversation_id: uuid.UUID
    property_key: PropertyKey
    scope_ref: str
    applicability: PreferenceApplicability
    proposed_value: str = Field(min_length=1, max_length=256)
    quote: str = Field(min_length=1, max_length=2000)
    turn_index: int = Field(strict=True, ge=0, le=63)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    claim_id: uuid.UUID | None
    target_memory_id: uuid.UUID | None
    target_version: str | None
    binding_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_evidence: dict

    @model_validator(mode="after")
    def check_binding(self):
        if (self.kind != "preference_question_confirmation_v1" or self.applicability.kind == "unresolved"
            or self.proposed_value not in self.quote or self.quote != self.quote.strip()
            or self.fingerprint != preference_fingerprint(self.property_key, self.applicability)
            or self.source_evidence.get("requires_preference_confirmation") is not True
            or self.source_evidence.get("grounding_mode") != "verified_source_spans"):
            raise ValueError("invalid preference confirmation binding")
        if (self.target_memory_id is None) != (self.target_version is None) or (
                (self.claim_id is None) != (self.target_memory_id is None)):
            raise ValueError("partial preference confirmation target")
        return self


def prepare_question_review(intent, *, snapshot, messages, source_conversation_id):
    identity = next(i for i in snapshot.identities if (i.property_key, i.scope_ref) == (intent.property_key, intent.scope_ref))
    scope = next(s for s in snapshot.catalog.scopes if s.ref == intent.scope_ref)
    memory = ExtractedMemory(content=intent.quote, category="preference", importance_score=5,
        confidence=.5, expiry="permanent", reasoning="Pending confirmation, not a current preference.")
    evidence, reason = ExtractionService._validate_user_evidence(memory, messages, [intent.turn_index],
        "direct_user_statement", evidence_spans=[{"turn_index": intent.turn_index, "quote": intent.quote}],
        question_review_only=True)
    if reason:
        raise ValueError(f"question review evidence rejected: {reason}")
    target = identity.target
    return QuestionReviewSpec(tenant_id=snapshot.tenant_id, proxy_user_id=snapshot.proxy_user_id,
        source_conversation_id=source_conversation_id, property_key=intent.property_key, scope_ref=intent.scope_ref,
        applicability=scope.applicability, proposed_value=intent.value, quote=intent.quote, turn_index=intent.turn_index,
        fingerprint=identity.fingerprint, claim_id=identity.claim_id,
        target_memory_id=target.memory_id if target else None, target_version=target.memory_version if target else None,
        binding_version=identity.binding_version, source_evidence=evidence).model_dump(mode="json")


def persist_question_reviews(session, *, job, record, reviews):
    # Same transaction as the applied receipt. Per-tenant lock bounds concurrent capacity.
    key = int.from_bytes(uuid.uuid5(uuid.NAMESPACE_URL, f"preference-reviews:{job.tenant_id}").bytes[:8], signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    count = 0
    for raw in reviews:
        spec = QuestionReviewSpec.model_validate_json(json.dumps(raw))
        if spec.tenant_id != job.tenant_id or spec.proxy_user_id != job.proxy_user_id:
            raise ValueError("preference review owner mismatch")
        review_id = uuid.uuid5(job.id, f"preference-confirmation:{spec.fingerprint}")
        if session.get(PendingExtractionCandidate, review_id) is not None:
            continue
        owned = select(PendingExtractionCandidate.id).where(
            PendingExtractionCandidate.tenant_id == job.tenant_id, confirmation_predicate())
        settings = get_settings()
        tenant_count = session.scalar(select(func.count()).select_from(owned.limit(settings.source_review_tenant_row_limit).subquery()))
        profile_count = session.scalar(select(func.count()).select_from(owned.where(
            PendingExtractionCandidate.proxy_user_id == job.proxy_user_id).limit(settings.source_review_profile_row_limit).subquery()))
        if tenant_count >= settings.source_review_tenant_row_limit or profile_count >= settings.source_review_profile_row_limit:
            # Never release a guard after silently dropping a review.
            raise ValueError("preference_confirmation_capacity_exceeded")
        session.add(PendingExtractionCandidate(id=review_id, tenant_id=job.tenant_id, proxy_user_id=job.proxy_user_id,
            extraction_job_id=job.id, source_event_id=job.source_event_id, content=spec.quote, category="preference",
            importance_score=5, confidence_score=.5, reasoning="Explicit confirmation required; no value activated.",
            candidate_reason=REASON, candidate_fingerprint=_hash({"job": str(job.id), "identity": spec.fingerprint}),
            status="pending", metadata_json={"preference_confirmation": raw, "capture_id": str(record.capture_id),
                "decision_sha256": record.decision_sha256}, created_at=job.created_at, last_seen_at=job.created_at))
        session.flush()
        count += 1
    return count


def _spec(candidate):
    if candidate is None or candidate.candidate_reason != REASON:
        raise APIError(status_code=404, code="PCR_404", error="preference_review_not_found")
    try:
        spec = QuestionReviewSpec.model_validate_json(json.dumps(candidate.metadata_json["preference_confirmation"]))
        if (spec.tenant_id != candidate.tenant_id or spec.proxy_user_id != candidate.proxy_user_id
                or spec.quote != candidate.content or candidate.category != "preference"):
            raise ValueError("changed review")
        return spec
    except (KeyError, TypeError, ValueError) as exc:
        raise APIError(status_code=409, code="PCR_409", error="preference_review_binding_invalid") from exc


def review_version(candidate):
    return _hash({"id": str(candidate.id), "job": str(candidate.extraction_job_id),
        "source": str(candidate.source_event_id), "created_at": candidate.created_at.isoformat(),
        "binding": _spec(candidate).model_dump(mode="json"),
        "capture": candidate.metadata_json.get("capture_id"), "decision": candidate.metadata_json.get("decision_sha256")})


def _source(session, candidate, spec):
    from api.services.runtime_preference_worker import RuntimePreferenceTransactionService
    # Pre-lock without waiting before the shared validator's job-lock query.
    locked = session.scalar(select(ExtractionJob).where(ExtractionJob.id == candidate.extraction_job_id,
        ExtractionJob.tenant_id == spec.tenant_id, ExtractionJob.proxy_user_id == spec.proxy_user_id)
        .with_for_update(nowait=True))
    if locked is None:
        raise ValueError("preference review source unavailable")
    service = RuntimePreferenceTransactionService(lambda: None)
    job, messages = service._owned_job(session, job_id=candidate.extraction_job_id,
        tenant_id=spec.tenant_id, proxy_user_id=spec.proxy_user_id, allow_applied=True)
    record = service._read(job)
    if (record is None or record.state != "applied" or record.preference_confirmation_policy != 2
            or job.runtime_applied_at is None or job.runtime_visibility_finalized_at is None
            or str(record.capture_id) != candidate.metadata_json.get("capture_id")
            or record.decision_sha256 != candidate.metadata_json.get("decision_sha256")):
        raise ValueError("preference review source not applied")
    _conversation, backing = service._check(session, job, messages, record, record.snapshot.catalog)
    from api.services.runtime_preference_routing import runtime_catalog
    if runtime_catalog(session.get(Tenant, spec.tenant_id)).model_dump(mode="json") != record.snapshot.catalog.model_dump(mode="json"):
        raise ValueError("preference review catalog changed")
    batch = service._bind(record, messages)
    if spec.model_dump(mode="json") not in batch.question_reviews:
        raise ValueError("preference review source changed")
    return job, record, backing


def _target(session, spec, *, lock=False):
    query = select(MemoryClaim).where(MemoryClaim.tenant_id == spec.tenant_id,
        MemoryClaim.proxy_user_id == spec.proxy_user_id, MemoryClaim.claim_fingerprint == spec.fingerprint)
    if lock:
        query = query.with_for_update(nowait=True)
    ledger = session.scalar(query.execution_options(populate_existing=True))
    if spec.claim_id is None:
        if ledger is not None and (ledger.active_memory_id or ledger.winning_revision_id or ledger.active_value):
            raise ValueError("preference review target changed")
        return ledger, None, None
    if ledger is None or ledger.id != spec.claim_id or ledger.active_memory_id != spec.target_memory_id:
        raise ValueError("preference review target changed")
    query = select(Memory).where(Memory.id == ledger.active_memory_id, Memory.proxy_user_id == spec.proxy_user_id)
    if lock:
        query = query.with_for_update(nowait=True)
    target = session.scalar(query.execution_options(populate_existing=True))
    revision = session.get(MemoryClaimRevision, ledger.winning_revision_id)
    current = snapshot_target(ledger, target, revision, property_key=spec.property_key, applicability=spec.applicability)
    if not current.is_current or current.memory_version != spec.target_version or identity_binding_version(ledger, current) != spec.binding_version:
        raise ValueError("preference review target changed")
    return ledger, target, current


def _owned(session, *, tenant_id, proxy_user_id, review_id, lock=False):
    query = select(PendingExtractionCandidate).where(PendingExtractionCandidate.id == uuid.UUID(str(review_id)),
        PendingExtractionCandidate.tenant_id == uuid.UUID(str(tenant_id)),
        PendingExtractionCandidate.proxy_user_id == uuid.UUID(str(proxy_user_id)),
        confirmation_predicate())
    if lock:
        query = query.with_for_update(nowait=True)
    candidate = session.scalar(query.execution_options(populate_existing=True))
    spec = _spec(candidate)
    proxy = session.get(ProxyUser, spec.proxy_user_id, populate_existing=True)
    tenant = session.get(Tenant, spec.tenant_id, populate_existing=True)
    if proxy is None or proxy.is_blocked or proxy.tenant_id != spec.tenant_id or tenant is None or not tenant.is_active:
        raise APIError(status_code=404, code="PCR_404", error="preference_review_not_found")
    return candidate, spec


def _deadline(candidate):
    value = candidate.created_at
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)) + TTL


def view(session, candidate):
    spec = _spec(candidate)
    _source(session, candidate, spec)
    _, _target_row, current = _target(session, spec)
    if candidate.status != "pending" or _deadline(candidate) <= datetime.now(UTC):
        return None
    actions = ["confirm", "keep_current", "dismiss"] if current is None or current.authority <= 20 else ["keep_current", "dismiss"]
    return PreferenceConfirmationReview(id=str(candidate.id), version=review_version(candidate),
        property_key=spec.property_key, applicability=spec.applicability.model_dump(exclude_none=True),
        current_value=current.value if current else None, proposed_value=spec.proposed_value,
        question=f"Change {spec.property_key} to {spec.proposed_value} in the displayed scope? This proposal is not current.",
        actions=actions, expires_at=_deadline(candidate))


def list_reviews(session, *, tenant_id, proxy_user_id, context=None):
    query = select(PendingExtractionCandidate).where(
        PendingExtractionCandidate.tenant_id == uuid.UUID(str(tenant_id)),
        PendingExtractionCandidate.proxy_user_id == uuid.UUID(str(proxy_user_id)),
        confirmation_predicate(), PendingExtractionCandidate.status == "pending")
    if context is not None:
        spec = PendingExtractionCandidate.metadata_json["preference_confirmation"]
        query = query.where(spec["property_key"].astext.in_(context.property_keys),
            spec["applicability"] == context.applicability.model_dump(exclude_none=True))
    rows = session.scalars(query.order_by(PendingExtractionCandidate.created_at.desc(),
        PendingExtractionCandidate.id).limit(MAX_RETURNED)).all()
    results = []
    for row in rows:
        try:
            with session.begin_nested():
                item = view(session, row)
        except DBAPIError as exc:
            if (getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)) == "55P03":
                continue
            raise
        except ValueError:
            continue  # Stale/retained source never becomes an answerable confirmation.
        if item is not None:
            results.append(item)
    return results


def pending_fingerprints(session, *, tenant_id, proxy_user_id):
    # Bounded profile capacity; expiration never silently activates the proposal.
    rows = session.scalars(select(PendingExtractionCandidate).where(
        PendingExtractionCandidate.tenant_id == uuid.UUID(str(tenant_id)),
        PendingExtractionCandidate.proxy_user_id == uuid.UUID(str(proxy_user_id)),
        confirmation_predicate(), PendingExtractionCandidate.status == "pending")
        .order_by(PendingExtractionCandidate.created_at.desc(), PendingExtractionCandidate.id)
        .limit(MAX_READ_HOLDS + 1)).all()
    if len(rows) > MAX_READ_HOLDS:
        return None  # Unexamined pending coverage stays broadly unavailable.
    result = set()
    for row in rows:
        spec = _spec(row)
        try:
            _, _, current = _target(session, spec)
        except ValueError:
            continue
        if current is None or current.authority <= 20:
            result.add(spec.fingerprint)
    return result


def confirmation_content(session, *, tenant_id, proxy_user_id, review_id, version):
    candidate, spec = _owned(session, tenant_id=tenant_id, proxy_user_id=proxy_user_id, review_id=review_id)
    if review_version(candidate) != version:
        raise APIError(status_code=409, code="PCR_409", error="preference_review_stale")
    if candidate.status != "pending":
        return None  # Final handler verifies the exact closed-action replay.
    current = view(session, candidate)
    if current is None or "confirm" not in current.actions:
        raise APIError(status_code=409, code="PCR_409", error="preference_review_not_confirmable")
    return spec.quote


def answer(session, *, tenant_id, proxy_user_id, review_id, version, action, embedding=None, api_key_id=None):
    from api.services.conflict_resolver import ConflictResolver
    if action not in {"confirm", "keep_current", "dismiss"}:
        raise ValueError("invalid preference confirmation action")
    # Nonblocking candidate/ledger locks avoid waiting in reverse worker order.
    candidate, spec = _owned(session, tenant_id=tenant_id, proxy_user_id=proxy_user_id, review_id=review_id, lock=True)
    if api_key_id is not None:
        from api.db.models import ApiKey
        key = session.get(ApiKey, uuid.UUID(str(api_key_id)), populate_existing=True)
        if (key is None or key.tenant_id != spec.tenant_id or not key.is_active
            or key.permissions is not None and not ({"write", "admin"} & set(key.permissions))):
            raise APIError(status_code=403, code="PCR_403", error="preference_review_writer_unavailable")
    if review_version(candidate) != version:
        raise APIError(status_code=409, code="PCR_409", error="preference_review_stale")
    if candidate.status != "pending":
        resolution = candidate.metadata_json.get("preference_confirmation_resolution") or {}
        if resolution.get("action") == action and resolution.get("version") == version:
            return PreferenceReviewAnswerData(review_id=str(candidate.id), action=action, replayed=True,
                memory_id=resolution.get("memory_id"))
        raise APIError(status_code=409, code="PCR_409", error="preference_review_closed")
    if _deadline(candidate) <= datetime.now(UTC) and action != "dismiss":
        raise APIError(status_code=409, code="PCR_409", error="preference_review_expired")
    if action == "dismiss":
        from api.db.models import User
        backing = session.scalar(select(User).where(User.external_id == f"proxy::{spec.proxy_user_id}", User.is_active.is_(True)))
        if backing is None:
            raise APIError(status_code=404, code="PCR_404", error="preference_review_not_found")
    else:
        job, record, backing = _source(session, candidate, spec)
        ledger, target, current = _target(session, spec, lock=True)
    memory_id = None
    if action == "confirm":
        from api.services.runtime_preference_routing import runtime_enabled
        if not runtime_enabled(tenant_id):
            raise APIError(status_code=409, code="PCR_409", error="preference_confirmation_disabled")
        if current is not None and current.authority > 20 or ledger is not None and ledger.authority_priority > 20:
            raise APIError(status_code=403, code="PCR_403", error="preference_authority_insufficient")
        if embedding is None:
            raise ValueError("precomputed confirmation embedding required")
        declaration = PreferenceClaimRequest(property_key=spec.property_key, value=spec.proposed_value,
            applicability=spec.applicability, commitment="replacement" if target else "asserted",
            turn_index=spec.turn_index, quote=spec.quote,
            replaces_memory_id=str(target.id) if target else None, replaces_version=spec.target_version)
        event_id = uuid.uuid4()
        event = MemorySourceEvent(id=event_id, tenant_id=spec.tenant_id, proxy_user_id=spec.proxy_user_id,
            api_key_id=uuid.UUID(str(api_key_id)) if api_key_id else None, source_service="memoryos-preference-confirmation",
            source_event_id=str(candidate.id), observed_at=datetime.now(UTC), scope=spec.applicability.model_dump(exclude_none=True),
            payload_hash=_hash({"review_id": str(candidate.id), "version": version, "action": action}),
            processing_metadata={"policy": "application_user_action_v1", "authority_priority": 20,
                "trusted_evidence_policy": {"attestation": "client_asserted", "authority_priority": 20,
                    "authority_rules": {"default_priority": 20}},
                "source_job_id": str(job.id), "source_event_id": str(job.source_event_id)})
        session.add(event)
        session.flush()
        evidence = {k:v for k,v in spec.source_evidence.items() if k != "requires_preference_confirmation"}
        evidence.update(citation_mode="application_user_action", relation="confirmed_preference_command",
            **{PREFERENCE_MARKER: declaration.model_dump(mode="json", exclude_none=True)}, preference_fingerprint=spec.fingerprint,
            preference_confirmation={"review_id": str(candidate.id), "version": version, "event_id": str(event_id),
                "action_origin": "authenticated_application_assertion"})
        memory = ExtractedMemory(content=spec.quote, category="preference", importance_score=5, confidence=.5,
            expiry="permanent", reasoning="Explicit application user-action confirmation; authority remains 20.")
        memory.validated_evidence = evidence
        writer = ConflictResolver(session=session, qdrant_service=None, embedder=lambda _content: embedding,
            source_messages=job.payload["messages"],
            confirmed_preference_command=ConfirmedPreferenceCommand(tenant_id=str(spec.tenant_id),
                proxy_user_id=str(spec.proxy_user_id), conversation_id=str(spec.source_conversation_id), event_id=event_id,
                review_id=str(candidate.id), review_version=version, declaration=declaration),
            default_source_event_id=event_id, default_source_conversation_id=spec.source_conversation_id,
            provenance_snapshot={"attestation": "client_asserted", "authority_priority": 20,
                "authority_rules": {"default_priority": 20}, "service": event.source_service,
                "event_id": str(candidate.id), "source_event_id": str(event_id),
                "external_conversation_id": record.external_conversation_id})
        stored = writer.check_and_store([memory], user_id=str(backing.id), tenant_id=str(spec.tenant_id),
            proxy_user_id=str(spec.proxy_user_id), source_conversation_id=str(spec.source_conversation_id), auto_commit=False)
        if writer.last_pending_candidates:
            raise ValueError("confirmation transition was not accepted")
        if len(stored) == 1:
            memory_id = stored[0].id
        elif not stored and target is not None and ledger.active_memory_id == target.id and ledger.active_value == spec.proposed_value:
            memory_id = str(target.id)
        else:
            raise ValueError("confirmation transition was not accepted")
        from api.tasks.extraction_tasks import _refresh_proxy_user_memory_count
        _refresh_proxy_user_memory_count(session, spec.proxy_user_id)
    candidate.status = "promoted" if action == "confirm" else "dismissed"
    candidate.updated_at = datetime.now(UTC)
    candidate.metadata_json = {**candidate.metadata_json, "preference_confirmation_resolution": {
        "action": action, "version": version, "memory_id": memory_id, "resolved_at": candidate.updated_at.isoformat()}}
    session.add(AuditLog(id=uuid.uuid4(), user_id=backing.id, proxy_user_id=spec.proxy_user_id,
        action=AuditAction.updated, memory_id=uuid.UUID(memory_id) if memory_id else None,
        metadata_json={"event": "preference_confirmation_answered", "review_id": str(candidate.id),
            "action": action, "version": version, "authority_priority": 20}))
    return PreferenceReviewAnswerData(review_id=str(candidate.id), action=action, memory_id=memory_id)
