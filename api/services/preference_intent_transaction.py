"""Provider-free durable runtime decisions on extraction jobs.

Reuses v1 ownership, capture, encrypted storage and retention. No queue/router
calls v2 directly. Its v3 subclass is used by the default-off runtime adapter.
In v2 facts are deferred pending relation validation, not current.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import Field, model_validator

from api.services.preference_choice_service import PreferenceChoiceRequest, Version
from api.services.preference_intent_admission_service import (
    REJECTION_REASONS,
    AdmissionCapture,
    PreferenceAdmissionUnavailable,
    PreferenceIntentAdmissionService,
    _Closed,
    _hash,
    _json,
)
from api.services.preference_intent_contract import (
    PreferenceIntentReply,
    bind_preference_intents,
)


class IntentTransactionReceipt(_Closed):
    created_memory_ids: tuple[uuid.UUID, ...] = Field(max_length=16)
    withheld: int = Field(strict=True, ge=0, le=16)
    reaffirmed: int = Field(strict=True, ge=0, le=16)
    pending_candidates_buffered: int = Field(strict=True, ge=0, le=16)
    reviews_capacity_rejected: int = Field(strict=True, ge=0, le=16)
    reviews_closed_replays: int = Field(strict=True, ge=0, le=16)
    deferred_facts: int = Field(strict=True, ge=0, le=16)
    rejected: dict[str, Annotated[int, Field(strict=True, ge=1, le=16)]] = Field(max_length=4)

    @model_validator(mode="after")
    def bounded_receipt(self):
        if (len(set(self.created_memory_ids)) != len(self.created_memory_ids)
            or any(reason not in REJECTION_REASONS for reason in self.rejected)):
            raise ValueError("invalid intent receipt")
        return self


class _IntentTransactionRecord(AdmissionCapture):
    external_conversation_id: str | None = Field(default=None, max_length=255)
    reply: PreferenceIntentReply | None = None
    decision_sha256: Version | None = None
    receipt: IntentTransactionReceipt | None = None

    @model_validator(mode="after")
    def consistent_record(self):
        if self.state == "captured":
            if self.reply is not None or self.decision_sha256 is not None or self.receipt is not None:
                raise ValueError("captured intent has no decision")
        elif self.reply is None or self.decision_sha256 != self.decision_hash():
            raise ValueError("intent decision mismatch")
        if self.state == "rejected":
            raise ValueError("runtime rejections are item outcomes, not a refreshed capture")
        if (self.state == "applied") != (self.receipt is not None):
            raise ValueError("terminal intent requires receipt")
        if self.receipt is not None:
            result = self.receipt
            if (not self.fact_counts_valid()
                or len(result.created_memory_ids) + result.withheld + result.reaffirmed
                   + sum(result.rejected.values()) > self.write_count_limit()
                or result.pending_candidates_buffered + result.reviews_capacity_rejected + result.reviews_closed_replays
                   > len(self.reply.preferences) + len(self.reply.nonpreferences)):
                raise ValueError("intent receipt count mismatch")
        return self

    def decision_hash(self):
        return _hash(self.reply.model_dump(mode="json"))

    def fact_counts_valid(self):
        return self.receipt.deferred_facts == len(self.reply.nonpreferences)

    def write_count_limit(self):
        return len(self.reply.preferences)


class IntentTransactionRecord(_IntentTransactionRecord):
    schema_version: Literal[2] = 2


class PreferenceIntentTransactionService(PreferenceIntentAdmissionService):
    record_model: type[AdmissionCapture] = IntentTransactionRecord
    receipt_model: type[IntentTransactionReceipt] = IntentTransactionReceipt

    @staticmethod
    def _external_id(job):
        value = job.payload.get("external_conversation_id")
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 255:
            raise PreferenceAdmissionUnavailable("intent_external_conversation_invalid")
        return value.strip() or None

    def _capture_fields(self, job, *, session):
        return {"external_conversation_id": self._external_id(job)}

    def _embedding_memories(self, batch, record):
        return batch.declarations

    def _prepare_nonpreferences(self, session, record, backing):
        return None

    def _store_nonpreferences(self, resolver, batch, record, context, writer_args, pending, defer):
        for memory in batch.nonpreferences:
            defer(memory, "Fact relation validation is deferred; existing facts remain unchanged.")
        return [], {"deferred_facts": len(batch.nonpreferences)}

    def _configure_write_transaction(self, session):
        pass

    def _check(self, session, job, messages, record, catalog, *, capture_id=None):
        result = super()._check(session, job, messages, record, catalog, capture_id=capture_id)
        if record.external_conversation_id != self._external_id(job):
            raise PreferenceAdmissionUnavailable("admission_inputs_changed")
        return result

    def _owned_job(self, session, **args):
        job, messages = super()._owned_job(session, **args)
        self._validate_source(session, job)
        return job, messages

    def _validate_source(self, session, job):
        if job.source_event_id is not None or (job.payload or {}).get("source_event_id") is not None:
            raise PreferenceAdmissionUnavailable("intent_registered_source_ineligible")

    def _write_provenance(self, job, record):
        provenance = {"attestation": "client_asserted", "authority_priority": 20}
        if record.external_conversation_id:
            provenance["external_conversation_id"] = record.external_conversation_id
        return provenance

    def admit(self, reply, *, job_id, tenant_id, proxy_user_id, catalog, capture_id):
        """Fix the first evidence-checked reply without classifying or writing memory."""
        from api.db.models import ExtractionJobStatus
        parsed = PreferenceIntentReply.model_validate(reply)
        decision_hash = _hash(parsed.model_dump(mode="json"))
        with self.session_factory.begin() as session:
            job, messages = self._owned_job(session, job_id=job_id, tenant_id=tenant_id, proxy_user_id=proxy_user_id)
            record = self._read(job)
            if record is None:
                raise PreferenceAdmissionUnavailable("admission_capture_missing")
            self._check(session, job, messages, record, catalog, capture_id=capture_id)
            if record.state != "captured":
                if record.decision_sha256 != decision_hash:
                    raise PreferenceAdmissionUnavailable("admission_decision_already_fixed")
                return record
            if job.status in {ExtractionJobStatus.completed, ExtractionJobStatus.dead}:
                raise PreferenceAdmissionUnavailable("admission_job_closed")
            bind_preference_intents(parsed, snapshot=record.snapshot, messages=messages,
                source_conversation_id=record.source_conversation_id)
            admitted = IntentTransactionRecord.model_validate_json(_json({**record.model_dump(mode="json"),
                "state": "validated", "reply": parsed.model_dump(mode="json"), "decision_sha256": decision_hash}))
            self._write(job, admitted)
            return admitted

    def apply(self, *, job_id, tenant_id, proxy_user_id, catalog, capture_id, embeddings=None):
        """Use only precomputed embeddings; commit writers, reviews and receipt together.

        Nonpreferences remain source reviews. No fact mutation/classification is
        authorized by this stage. Retry returns the same receipt without writes.
        """
        from sqlalchemy import select

        from api.db.models import (
            ConversationProcessingStatus,
            ExtractionJobStatus,
            Memory,
            MemoryClaimRevision,
        )
        from api.schemas.extraction_schemas import PendingExtractedMemory
        from api.schemas.preferences import PreferenceClaimRequest
        from api.services.conflict_resolver import ConflictResolver
        from api.services.preference_choice_service import (
            CHOICE_OPERATION_MARKER,
            PreferenceChoiceOperation,
            lock_choice_target,
        )
        from api.services.preference_claim_service import (
            PREFERENCE_MARKER,
            reserve_claim,
        )
        from api.services.preference_snapshot_service import (
            identity_binding_version,
            snapshot_target,
        )
        from api.services.source_review_service import review_intent
        from api.tasks.extraction_tasks import (
            _persist_pending_extraction_candidates,
            _refresh_proxy_user_memory_count,
        )

        with self.session_factory.begin() as session:
            self._configure_write_transaction(session)
            job, messages = self._owned_job(session, job_id=job_id, tenant_id=tenant_id, proxy_user_id=proxy_user_id)
            record = self._read(job)
            if record is None:
                raise PreferenceAdmissionUnavailable("admission_capture_missing")
            conversation, backing = self._check(session, job, messages, record, catalog, capture_id=capture_id)
            if record.state == "applied":
                return record
            if record.state != "validated":
                raise PreferenceAdmissionUnavailable("admission_decision_missing")
            if job.status in {ExtractionJobStatus.completed, ExtractionJobStatus.dead}:
                raise PreferenceAdmissionUnavailable("admission_job_closed")
            batch = bind_preference_intents(record.reply, snapshot=record.snapshot, messages=messages,
                source_conversation_id=record.source_conversation_id)
            # A callback cannot sneak provider execution into this transaction.
            if embeddings is not None and type(embeddings) is not dict:
                raise TypeError("precomputed embedding mapping required, not a callback")
            prepared = {}
            for memory in self._embedding_memories(batch, record):
                if embeddings is None or memory.content not in embeddings:
                    raise PreferenceAdmissionUnavailable("intent_embedding_missing")
                prepared[memory.content] = ConflictResolver._coerce_embedding_result(embeddings[memory.content])
            provenance = self._write_provenance(job, record)
            resolver = ConflictResolver(session=session, qdrant_service=None, embedder=prepared.__getitem__,
                source_messages=messages, default_source_conversation_id=conversation.id,
                default_source_event_id=job.source_event_id, provenance_snapshot=provenance)
            fact_context = self._prepare_nonpreferences(session, record, backing)
            pending, rejected, declarations, requests = [], {}, [], []

            def defer(memory, reason):
                pending.append(PendingExtractedMemory(content=memory.content, category=memory.category,
                    importance_score=memory.importance_score, confidence=memory.confidence,
                    reasoning=reason, candidate_reason="source_decision_pending",
                    validated_evidence={**memory.validated_evidence, "source_review": review_intent(None)}))

            for memory in batch.pending_preferences:
                defer(memory, "Preference intent cannot become a current default.")
            items = [(m.validated_evidence["preference_fingerprint"], "declaration", m) for m in batch.declarations]
            items += [(m.validated_evidence["preference_fingerprint"], "choice", m) for m in batch.choices]
            # Acquire all preference identities before any mutation; absence is
            # protected by the same ON CONFLICT reservation as the existing writer.
            for fingerprint, kind, memory in sorted(items, key=lambda item: item[0]):
                if kind == "choice":
                    operation = PreferenceChoiceOperation.model_validate(memory.validated_evidence[CHOICE_OPERATION_MARKER])
                    _, _, reason = lock_choice_target(session, operation, user_id=backing.id)
                    if reason in {"WITHHOLD", "REAFFIRM", "DEDUP"}:
                        requests.append(PreferenceChoiceRequest.model_validate({k: getattr(operation, k)
                            for k in PreferenceChoiceRequest.model_fields}))
                    else:
                        rejected[reason] = rejected.get(reason, 0) + 1
                    continue
                declaration = PreferenceClaimRequest.model_validate(memory.validated_evidence[PREFERENCE_MARKER])
                offered = next(i for i in record.snapshot.identities if i.fingerprint == fingerprint)
                ledger = reserve_claim(session, tenant_id=str(job.tenant_id), proxy_user_id=str(job.proxy_user_id),
                    declaration=declaration)
                reason = "preference_target_stale"
                valid = (offered.target is None and ledger.active_memory_id is None
                    and ledger.winning_revision_id is None and ledger.active_value is None
                    and ledger.status == "disputed" and ledger.authority_priority <= 20)
                if offered.target is not None and ledger.id == offered.claim_id:
                    target = session.execute(select(Memory).where(Memory.id == ledger.active_memory_id,
                        Memory.user_id == backing.id, Memory.proxy_user_id == job.proxy_user_id)
                        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
                    revision = session.execute(select(MemoryClaimRevision).where(
                        MemoryClaimRevision.id == ledger.winning_revision_id,
                        MemoryClaimRevision.claim_id == ledger.id, MemoryClaimRevision.memory_id == ledger.active_memory_id)
                        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
                    try:
                        current = snapshot_target(ledger, target, revision, property_key=offered.property_key,
                            applicability=declaration.applicability)
                        valid = identity_binding_version(ledger, current) == offered.binding_version
                        if current.authority > 20:
                            valid, reason = False, "preference_authority_insufficient"
                    except ValueError:
                        reason = "preference_ledger_inconsistent"
                if valid:
                    declarations.append(memory)
                else:
                    rejected[reason] = rejected.get(reason, 0) + 1
            writer_args = {"user_id": str(backing.id), "tenant_id": str(job.tenant_id), "proxy_user_id": str(job.proxy_user_id),
                "source_conversation_id": str(conversation.id), "auto_commit": False}
            stored = resolver.check_and_store(declarations, **writer_args) if declarations else []
            pending.extend(resolver.last_pending_candidates)
            withheld = reaffirmed = 0
            if requests:
                resolver.apply_preference_choices(requests, snapshot=record.snapshot, user_id=str(backing.id),
                    source_conversation_id=record.source_conversation_id)
                pending.extend(resolver.last_pending_candidates)
                withheld, reaffirmed = resolver.last_preference_choices_withheld, resolver.last_preference_choices_reaffirmed
                for reason, count in resolver.last_preference_choices_rejected.items():
                    rejected[reason] = rejected.get(reason, 0) + count
            fact_stored, fact_counts = self._store_nonpreferences(
                resolver, batch, record, fact_context, writer_args, pending, defer)
            stored.extend(fact_stored)
            diagnostics = {}
            buffered = _persist_pending_extraction_candidates(session, candidates=pending,
                tenant_id=str(job.tenant_id), proxy_user_id=str(job.proxy_user_id), extraction_job_id=str(job.id),
                source_event_id=str(job.source_event_id) if job.source_event_id else None, diagnostics=diagnostics)
            receipt = self.receipt_model(created_memory_ids=tuple(uuid.UUID(m.id) for m in stored),
                withheld=withheld, reaffirmed=reaffirmed, pending_candidates_buffered=buffered,
                reviews_capacity_rejected=diagnostics.get("source_reviews_capacity_rejected", 0),
                reviews_closed_replays=diagnostics.get("source_reviews_closed_replays", 0),
                **fact_counts, rejected=rejected)
            finished = self.record_model.model_validate_json(_json({**record.model_dump(mode="json"),
                "state": "applied", "receipt": receipt.model_dump(mode="json")}))
            conversation.processing_status = ConversationProcessingStatus.done
            if stored:
                _refresh_proxy_user_memory_count(session, job.proxy_user_id)
            self._write(job, finished)
            return finished
