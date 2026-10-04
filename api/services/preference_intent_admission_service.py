"""Inactive, server-only durable admission on the existing extraction job.

No classifier, queue, endpoint or live worker calls this service. A validated
request means attribution/shape checked, not independently proven semantics.
Capture/admission sessions close before any future classifier call. Application
uses the existing writer, audit and review transaction, never a parallel writer.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from api.db.models import (
    Conversation,
    ConversationProcessingStatus,
    ExtractionJob,
    ExtractionJobStatus,
    ProxyUser,
    User,
)
from api.infra.protected_storage import encrypt_json_for_dual_write
from api.services.conflict_resolver import ConflictResolver
from api.services.preference_choice_service import (
    PreferenceChoiceRequest,
    Version,
    bind_choice_operations,
)
from api.services.preference_snapshot_service import (
    PreferenceSnapshot,
    PreferenceSnapshotCatalog,
    read_preference_snapshot,
    validate_snapshot_coverage,
)
from api.settings import get_settings

MAX_ADMISSION_BYTES = 240 * 1024  # Headroom below the DB's 256 KiB JSONB-text bound.
MAX_TRANSCRIPT_BYTES = 64 * 1024
REJECTION_REASONS = {
    "preference_target_stale", "preference_ledger_inconsistent",
    "preference_authority_insufficient", "preference_choice_not_held",
}


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdmissionOutcome(_Closed):
    withheld: int = Field(strict=True, ge=0, le=16)
    reaffirmed: int = Field(strict=True, ge=0, le=16)
    deduplicated: int = Field(strict=True, ge=0, le=16)
    reviews_buffered: int = Field(strict=True, ge=0, le=16)
    reviews_capacity_rejected: int = Field(strict=True, ge=0, le=16)
    reviews_closed_replays: int = Field(strict=True, ge=0, le=16)
    rejected: dict[str, Annotated[int, Field(strict=True, ge=1, le=16)]] = Field(max_length=4)

    @model_validator(mode="after")
    def bounded_outcome(self):
        if (any(key not in REJECTION_REASONS for key in self.rejected)
            or self.reviews_buffered + self.reviews_capacity_rejected + self.reviews_closed_replays > self.withheld):
            raise ValueError("invalid admission outcome")
        return self


class PreferenceIntentAdmission(_Closed):
    schema_version: Literal[1] = 1
    capture_id: uuid.UUID
    tenant_id: uuid.UUID
    proxy_user_id: uuid.UUID
    source_conversation_id: uuid.UUID
    transcript_sha256: Version
    catalog_sha256: Version
    snapshot: PreferenceSnapshot
    state: Literal["captured", "validated", "applied", "rejected"] = "captured"
    requests: tuple[PreferenceChoiceRequest, ...] = Field(default=(), max_length=16)
    decision_sha256: Version | None = None
    outcome: AdmissionOutcome | None = None

    @model_validator(mode="after")
    def consistent_record(self):
        validate_snapshot_coverage(self.snapshot)
        if (self.snapshot.tenant_id != self.tenant_id or self.snapshot.proxy_user_id != self.proxy_user_id
            or self.catalog_sha256 != _hash(self.snapshot.catalog.model_dump(mode="json"))):
            raise ValueError("admission principal/catalog mismatch")
        if self.state == "captured":
            if self.requests or self.decision_sha256 is not None or self.outcome is not None:
                raise ValueError("captured admission has no decision")
        elif self.decision_sha256 != _hash([request.model_dump(mode="json") for request in self.requests]):
            raise ValueError("admission decision mismatch")
        if (self.state in {"applied", "rejected"}) != (self.outcome is not None):
            raise ValueError("terminal admission requires an outcome")
        if self.outcome is not None:
            result = self.outcome
            if result.withheld + result.reaffirmed + result.deduplicated + sum(result.rejected.values()) != len(self.requests):
                raise ValueError("admission outcome count mismatch")
            rejected_only = bool(result.rejected) and result.withheld + result.reaffirmed + result.deduplicated == 0
            if (self.state == "rejected") != rejected_only:
                raise ValueError("admission outcome/state mismatch")
        return self


class PreferenceAdmissionUnavailable(ValueError):
    """Never convert an unavailable or changed admission into a fresh capture."""


class PreferenceIntentAdmissionService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def _owned_job(self, session, *, job_id, tenant_id, proxy_user_id):
        if not get_settings().explicit_preference_claims_enabled:
            raise PreferenceAdmissionUnavailable("explicit_preferences_unavailable")
        job = session.execute(select(ExtractionJob).where(
            ExtractionJob.id == uuid.UUID(str(job_id)), ExtractionJob.tenant_id == uuid.UUID(str(tenant_id)),
            ExtractionJob.proxy_user_id == uuid.UUID(str(proxy_user_id)),
        ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        proxy = session.get(ProxyUser, uuid.UUID(str(proxy_user_id)), populate_existing=True)
        if job is None or proxy is None or proxy.tenant_id != job.tenant_id or proxy.is_blocked:
            raise PreferenceAdmissionUnavailable("admission_owner_unavailable")
        now = datetime.now(UTC)
        expires = job.raw_payload_expires_at
        if expires is not None:
            expires = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires.astimezone(UTC)
        if job.payload_redacted_at is not None or (expires is not None and expires <= now):
            raise PreferenceAdmissionUnavailable("admission_source_unavailable")
        payload = job.payload or {}
        if not isinstance(payload, dict) or not isinstance(payload.get("source") or {}, dict):
            raise PreferenceAdmissionUnavailable("admission_source_ineligible")
        if (payload.get("agent_id") is not None or (payload.get("source") or {}).get("explicit") is True
            or payload.get("preference_claims") is not None
            or any(payload.get(key) is not None and str(payload[key]) != str(getattr(job, key))
                   for key in ("tenant_id", "proxy_user_id"))):
            raise PreferenceAdmissionUnavailable("admission_source_ineligible")
        messages = payload.get("messages")
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 64
            or any(not isinstance(turn, dict) or not isinstance(turn.get("content"), str) for turn in messages)
            or len(_json(messages).encode()) > MAX_TRANSCRIPT_BYTES):
            raise PreferenceAdmissionUnavailable("admission_source_unavailable")
        return job, messages

    @staticmethod
    def _read(job):
        raw = job.preference_intent_admission
        if raw is None:
            if job.preference_intent_admission_envelope is not None:
                raise PreferenceAdmissionUnavailable("admission_record_inconsistent")
            return None
        try:
            encoded = _json(raw)
            if len(encoded.encode()) > MAX_ADMISSION_BYTES:
                raise ValueError("admission oversized")
            return PreferenceIntentAdmission.model_validate_json(encoded)
        except (TypeError, ValueError) as exc:
            raise PreferenceAdmissionUnavailable("admission_record_inconsistent") from exc

    @staticmethod
    def _write(job, record):
        raw = record.model_dump(mode="json")
        encoded = _json(raw)
        if len(encoded.encode()) > MAX_ADMISSION_BYTES:
            raise PreferenceAdmissionUnavailable("admission_record_oversized")
        # Encrypt before assigning. Encryption failure aborts the enclosing
        # transaction, including any choice, audit and pending-review writes.
        envelope = encrypt_json_for_dual_write(tenant_id=str(job.tenant_id),
            record_type="preference-intent-admission", record_id=str(job.id), value=raw)
        job.preference_intent_admission = raw
        job.preference_intent_admission_envelope = envelope

    @staticmethod
    def _check(session, job, messages, record, catalog, *, capture_id=None):
        if (record.tenant_id != job.tenant_id or record.proxy_user_id != job.proxy_user_id
            or catalog.tenant_id != job.tenant_id
            or record.transcript_sha256 != _hash(messages)
            or record.catalog_sha256 != _hash(catalog.model_dump(mode="json"))
            or (capture_id is not None and record.capture_id != uuid.UUID(str(capture_id)))):
            raise PreferenceAdmissionUnavailable("admission_inputs_changed")
        conversation = session.get(Conversation, record.source_conversation_id, populate_existing=True)
        backing = session.execute(select(User).where(
            User.external_id == f"proxy::{job.proxy_user_id}", User.is_active.is_(True),
        )).scalar_one_or_none()
        if conversation is None or backing is None or conversation.user_id != backing.id or conversation.agent_id is not None:
            raise PreferenceAdmissionUnavailable("admission_source_unavailable")
        return conversation, backing

    def capture(self, *, job_id, tenant_id, proxy_user_id, catalog: PreferenceSnapshotCatalog):
        """Capture once. Never refresh bindings or create a retry conversation."""
        catalog = PreferenceSnapshotCatalog.model_validate(catalog.model_dump(mode="json"))
        with self.session_factory.begin() as session:
            job, messages = self._owned_job(session, job_id=job_id, tenant_id=tenant_id, proxy_user_id=proxy_user_id)
            record = self._read(job)
            if record is not None:
                self._check(session, job, messages, record, catalog)
                return record
            if catalog.tenant_id != job.tenant_id or job.status in {ExtractionJobStatus.completed, ExtractionJobStatus.dead}:
                raise PreferenceAdmissionUnavailable("admission_capture_unavailable")
            from api.tasks.extraction_tasks import (
                _create_source_conversation,
                _ensure_proxy_backing_user,
            )
            backing = _ensure_proxy_backing_user(session, str(job.proxy_user_id))
            if not backing.is_active:
                raise PreferenceAdmissionUnavailable("admission_owner_unavailable")
            snapshot = read_preference_snapshot(session, tenant_id=job.tenant_id,
                proxy_user_id=job.proxy_user_id, catalog=catalog)
            conversation = _create_source_conversation(session, user_id=backing.id, agent_id=None, message_count=len(messages))
            record = PreferenceIntentAdmission(capture_id=uuid.uuid4(), tenant_id=job.tenant_id,
                proxy_user_id=job.proxy_user_id, source_conversation_id=conversation.id,
                transcript_sha256=_hash(messages), catalog_sha256=_hash(catalog.model_dump(mode="json")), snapshot=snapshot)
            self._write(job, record)
            return record

    def admit(self, requests, *, job_id, tenant_id, proxy_user_id, catalog, capture_id):
        """First checked decision wins. Invalid replies cannot modify a capture."""
        if not isinstance(requests, (list, tuple)) or len(requests) > 16:
            raise PreferenceAdmissionUnavailable("admission_decision_invalid")
        parsed = tuple(PreferenceChoiceRequest.model_validate(raw) for raw in requests)
        with self.session_factory.begin() as session:
            job, messages = self._owned_job(session, job_id=job_id, tenant_id=tenant_id, proxy_user_id=proxy_user_id)
            record = self._read(job)
            if record is None:
                raise PreferenceAdmissionUnavailable("admission_capture_missing")
            self._check(session, job, messages, record, catalog, capture_id=capture_id)
            decision_hash = _hash([request.model_dump(mode="json") for request in parsed])
            if record.state != "captured":
                if decision_hash != record.decision_sha256:
                    raise PreferenceAdmissionUnavailable("admission_decision_already_fixed")
                return record
            if job.status in {ExtractionJobStatus.completed, ExtractionJobStatus.dead}:
                raise PreferenceAdmissionUnavailable("admission_job_closed")
            if parsed:
                bind_choice_operations(parsed, snapshot=record.snapshot, messages=messages,
                    source_conversation_id=record.source_conversation_id)
            raw = {**record.model_dump(mode="json"), "state": "validated",
                "requests": [request.model_dump(mode="json") for request in parsed], "decision_sha256": decision_hash}
            admitted = PreferenceIntentAdmission.model_validate_json(_json(raw))
            self._write(job, admitted)
            return admitted

    def apply(self, *, job_id, tenant_id, proxy_user_id, catalog, capture_id):
        """Apply once, atomically with the original writer's audit/review writes."""
        with self.session_factory.begin() as session:
            job, messages = self._owned_job(session, job_id=job_id, tenant_id=tenant_id, proxy_user_id=proxy_user_id)
            record = self._read(job)
            if record is None:
                raise PreferenceAdmissionUnavailable("admission_capture_missing")
            conversation, backing = self._check(session, job, messages, record, catalog, capture_id=capture_id)
            if record.state in {"applied", "rejected"}:
                return record  # Durable receipt: no stale rebinding or review resurrection.
            if record.state != "validated":
                raise PreferenceAdmissionUnavailable("admission_decision_missing")
            if job.status in {ExtractionJobStatus.completed, ExtractionJobStatus.dead}:
                raise PreferenceAdmissionUnavailable("admission_job_closed")
            outcome = AdmissionOutcome(withheld=0, reaffirmed=0, deduplicated=0, reviews_buffered=0,
                reviews_capacity_rejected=0, reviews_closed_replays=0, rejected={})
            if record.requests:
                from api.tasks.extraction_tasks import (
                    _persist_pending_extraction_candidates,
                )
                def no_embedding(*_args, **_kwargs):
                    raise RuntimeError("value-free admission must not embed")
                resolver = ConflictResolver(session=session, qdrant_service=None, embedder=no_embedding,
                    source_messages=messages)
                resolver.apply_preference_choices(record.requests, snapshot=record.snapshot,
                    user_id=str(backing.id), source_conversation_id=record.source_conversation_id)
                diagnostics = {}
                buffered = _persist_pending_extraction_candidates(session,
                    candidates=resolver.last_pending_candidates, tenant_id=str(job.tenant_id),
                    proxy_user_id=str(job.proxy_user_id), extraction_job_id=str(job.id), source_event_id=None,
                    diagnostics=diagnostics)
                withheld, reaffirmed = resolver.last_preference_choices_withheld, resolver.last_preference_choices_reaffirmed
                rejected = resolver.last_preference_choices_rejected
                outcome = AdmissionOutcome(withheld=withheld, reaffirmed=reaffirmed,
                    deduplicated=len(record.requests) - withheld - reaffirmed - sum(rejected.values()),
                    rejected=rejected, reviews_buffered=buffered,
                    reviews_capacity_rejected=diagnostics.get("source_reviews_capacity_rejected", 0),
                    reviews_closed_replays=diagnostics.get("source_reviews_closed_replays", 0))
            rejected_only = bool(outcome.rejected) and outcome.withheld + outcome.reaffirmed + outcome.deduplicated == 0
            finished = PreferenceIntentAdmission.model_validate_json(_json({**record.model_dump(mode="json"),
                "state": "rejected" if rejected_only else "applied", "outcome": outcome.model_dump(mode="json")}))
            conversation.processing_status = ConversationProcessingStatus.done
            self._write(job, finished)
            return finished
