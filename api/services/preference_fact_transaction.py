"""Inactive v3 fact planning/application; no queue, router or provider selection.

The injected relation service runs only after the read session closes. Apply
replays backend-parsed decisions, never a provider response or a new target.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import Field, model_validator
from sqlalchemy import func, or_, select, text

from api.db.models import ExtractionJobStatus, Memory, MemoryCategory, MemoryClaim, User
from api.services.claim_ledger_service import authority_priority
from api.services.conflict_detection import ConflictType
from api.services.conflict_resolver import (
    ConflictDecision,
    ConflictResolver,
    SourceMemoryContext,
)
from api.services.preference_choice_service import Version
from api.services.preference_claim_service import preference_declaration
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
    _Closed,
    _hash,
    _json,
)
from api.services.preference_intent_contract import (
    PreferenceIntentReply,
    bind_preference_intents,
)
from api.services.preference_intent_transaction import (
    IntentTransactionReceipt,
    PreferenceIntentTransactionService,
    _IntentTransactionRecord,
)
from api.services.source_review_service import review_intent, target_version

MAX_FACT_CONTEXT = 50


class FactTarget(_Closed):
    memory_id: uuid.UUID
    version: Version


class FactPlan(_Closed):
    target_id: uuid.UUID | None
    action: Literal["UPDATE", "KEEP_BOTH", "REJECT", "pending"]
    reasoning: str = Field(min_length=1, max_length=512)
    decision_evidence: dict[str, Any] | None

    @model_validator(mode="after")
    def bounded_plan(self):
        if self.action == "UPDATE" and self.target_id is None:
            raise ValueError("fact correction requires captured target")
        if len(_json(self.decision_evidence).encode("utf-8")) > 8192:
            raise ValueError("fact evidence exceeds limit")
        return self


class FactTransactionReceipt(IntentTransactionReceipt):
    facts_created: int = Field(strict=True, ge=0, le=16)
    facts_rejected: int = Field(strict=True, ge=0, le=16)


class FactTransactionRecord(_IntentTransactionRecord):
    schema_version: Literal[3] = 3
    fact_context: tuple[FactTarget, ...] = Field(max_length=MAX_FACT_CONTEXT)
    fact_context_complete: bool = Field(strict=True)
    fact_plans: tuple[FactPlan, ...] | None = Field(default=None, max_length=16)
    receipt: FactTransactionReceipt | None = None

    @model_validator(mode="after")
    def consistent_fact_plans(self):
        ids = {item.memory_id for item in self.fact_context}
        if len(ids) != len(self.fact_context):
            raise ValueError("duplicate fact target")
        if self.state == "captured":
            if self.fact_plans is not None:
                raise ValueError("captured facts have no decision")
        elif (self.fact_plans is None or len(self.fact_plans) != len(self.reply.nonpreferences)
              or any(p.target_id is not None and p.target_id not in ids for p in self.fact_plans)):
            raise ValueError("fact plans require exact captured context")
        if not self.fact_context_complete and self.fact_plans and any(p.action != "pending" for p in self.fact_plans):
            raise ValueError("incomplete fact context cannot authorize writes")
        return self

    def decision_hash(self):
        return _hash({"reply": self.reply.model_dump(mode="json"),
                      "fact_plans": [p.model_dump(mode="json") for p in self.fact_plans or ()],
                      "fact_context": [t.model_dump(mode="json") for t in self.fact_context],
                      "fact_context_complete": self.fact_context_complete})

    def fact_counts_valid(self):
        r = self.receipt
        return (r.facts_created + r.facts_rejected + r.deferred_facts == len(self.reply.nonpreferences)
                and r.facts_created <= len(r.created_memory_ids))

    def write_count_limit(self):
        return len(self.reply.preferences) + self.receipt.facts_created


def _pending(reason: str, target=None) -> FactPlan:
    decision = ConflictResolver._classifier_failure_decision(
        conflict_type=ConflictType.UNKNOWN, reason_code=reason)
    return FactPlan(target_id=target.id if target is not None else None, action="pending",
                    reasoning=decision.reasoning, decision_evidence=decision.decision_evidence)


def _protected(row):
    return row.category == MemoryCategory.preference or bool(
        preference_declaration((row.metadata_json or {}).get("provenance")))


def _targets(session, record):
    """One bounded owned current context, including protected rows as veto inputs.

    +1 proves completeness; an omitted row never counts as verified absence.
    """
    now = datetime.now(UTC)
    query = select(Memory).join(User, User.id == Memory.user_id).where(
        User.external_id == f"proxy::{record.proxy_user_id}", User.is_active.is_(True),
        Memory.proxy_user_id == record.proxy_user_id, Memory.is_archived.is_(False),
        or_(Memory.expires_at.is_(None), Memory.expires_at > now),
        or_(Memory.effective_until.is_(None), Memory.effective_until > now),
        or_(Memory.effective_from.is_(None), Memory.effective_from <= now),
    ).order_by(Memory.id).limit(MAX_FACT_CONTEXT + 1)
    rows = list(session.scalars(query.execution_options(populate_existing=True)))
    return rows[:MAX_FACT_CONTEXT], len(rows) <= MAX_FACT_CONTEXT


def _bindings(rows):
    return tuple(FactTarget(memory_id=row.id, version=target_version(row)) for row in rows)


class _PreparedFacts(ConflictResolver):
    """Local replay adapter. All mutations still belong to ConflictResolver."""
    plan: FactPlan
    expected_version: str | None
    tenant_id: uuid.UUID

    def _classify_source_claim(self, memory, targets):
        target = next((row for row in targets if row.id == self.plan.target_id), None)
        if self.plan.target_id is not None:
            if target is None or _protected(target):
                return None, self._classifier_failure_decision(
                    conflict_type=ConflictType.UNKNOWN, reason_code="source_target_stale")
            # No lock across classification; acquire the selected fact only now.
            current = self.session.execute(select(Memory).where(Memory.id == target.id).with_for_update()
                .execution_options(populate_existing=True)).scalar_one_or_none()
            if current is None or target_version(current) != self.expected_version:
                return None, self._classifier_failure_decision(
                    conflict_type=ConflictType.UNKNOWN, reason_code="source_target_stale")
            target = current
            ledger_authority = self.session.scalar(select(func.max(MemoryClaim.authority_priority)).where(
                MemoryClaim.active_memory_id == current.id,
                MemoryClaim.tenant_id == self.tenant_id,
                MemoryClaim.proxy_user_id == current.proxy_user_id))
            if max(ledger_authority or 0, authority_priority(
                    (current.metadata_json or {}).get("provenance"), current.category.value)) > 20:
                decision = self._classifier_failure_decision(
                    conflict_type=ConflictType.UNKNOWN, reason_code="source_authority_insufficient")
                decision.source_review = review_intent(current)
                return None, decision
        assert self.plan.action != "pending"
        decision = ConflictDecision(action=self.plan.action, reasoning=self.plan.reasoning,
                                    decision_evidence=self.plan.decision_evidence)
        return target, decision


class PreferenceFactTransactionService(PreferenceIntentTransactionService):
    record_model = FactTransactionRecord
    receipt_model = FactTransactionReceipt

    def _capture_fields(self, job, *, session):
        rows, complete = _targets(session, job)
        return {**super()._capture_fields(job, session=session),
                "fact_context": _bindings(rows), "fact_context_complete": complete}

    def admit(self, reply, *, llm_service, job_id, tenant_id, proxy_user_id, catalog, capture_id):
        """Classify detached inputs, then fix the first backend-parsed plans.

        llm_service uses the existing relation API/router. It is not retained for
        apply. No provider work happens when a terminal/fixed decision is retried.
        """
        parsed = PreferenceIntentReply.model_validate(reply)
        args = {"job_id": job_id, "tenant_id": tenant_id, "proxy_user_id": proxy_user_id}
        with self.session_factory.begin() as session:
            job, messages = self._owned_job(session, **args)
            record = self._read(job)
            if record is None:
                raise PreferenceAdmissionUnavailable("admission_capture_missing")
            self._check(session, job, messages, record, catalog, capture_id=capture_id)
            if record.state != "captured":
                if record.reply != parsed:
                    raise PreferenceAdmissionUnavailable("admission_decision_already_fixed")
                return record
            if job.status in {ExtractionJobStatus.completed, ExtractionJobStatus.dead}:
                raise PreferenceAdmissionUnavailable("admission_job_closed")
            batch = bind_preference_intents(parsed, snapshot=record.snapshot, messages=messages,
                                           source_conversation_id=record.source_conversation_id)
            rows, complete = _targets(session, record)
            safe = complete and record.fact_context_complete and _bindings(rows) == record.fact_context
            # Detach before session exit, including expire_on_commit=True factories.
            for row in rows:
                session.expunge(row)
        plans = self._plan_facts(record, batch, rows, messages, llm_service) if safe else tuple(
            _pending("source_context_incomplete") for _ in batch.nonpreferences)
        with self.session_factory.begin() as session:
            job, messages = self._owned_job(session, **args)
            current = self._read(job)
            self._check(session, job, messages, current, catalog, capture_id=capture_id)
            if current.state != "captured":
                if current.reply != parsed:
                    raise PreferenceAdmissionUnavailable("admission_decision_already_fixed")
                return current
            if job.status in {ExtractionJobStatus.completed, ExtractionJobStatus.dead}:
                raise PreferenceAdmissionUnavailable("admission_job_closed")
            raw = {**current.model_dump(mode="json"), "state": "validated", "reply": parsed.model_dump(mode="json"),
                   "fact_plans": [p.model_dump(mode="json") for p in plans]}
            raw["decision_sha256"] = _hash({key: raw[key] for key in (
                "reply", "fact_plans", "fact_context", "fact_context_complete")})
            admitted = self.record_model.model_validate_json(_json(raw))
            self._write(job, admitted)
            return admitted

    @staticmethod
    def _plan_facts(record, batch, rows, messages, llm_service):
        planner = ConflictResolver(session=SimpleNamespace(), qdrant_service=None, embedder=None,
                                   source_messages=messages, llm_service=llm_service)
        plans, transient_indexes, selected_indexes, conflicted = [], {}, {}, set()
        candidates = list(rows)
        for index, memory in enumerate(batch.nonpreferences):
            target, decision = planner._classify_source_claim(memory, candidates)
            if target is None and decision.source_review:
                review_id = decision.source_review.get("target_memory_id")
                target = next((row for row in candidates if str(row.id) == review_id), None)
            if target is not None and target.id in transient_indexes:
                conflicted.update((index, transient_indexes[target.id]))
                plan = _pending("source_batch_dependency")
            elif target is not None and _protected(target):
                plan = _pending("source_preference_lane_protected")
            elif target is not None and authority_priority(
                    (target.metadata_json or {}).get("provenance"), target.category.value) > 20:
                plan = _pending("source_authority_insufficient", target)
            elif decision.action in {"UPDATE", "KEEP_BOTH", "REJECT"}:
                if target is not None and target.id in selected_indexes:
                    conflicted.update((index, selected_indexes[target.id]))
                if target is not None:
                    selected_indexes[target.id] = index
                try:
                    plan = FactPlan(target_id=target.id if target is not None else None, action=decision.action,
                                    reasoning=decision.reasoning, decision_evidence=decision.decision_evidence)
                except ValueError:
                    plan = _pending("source_response_invalid")
            else:
                plan = _pending("source_relation_pending", target)
            plans.append(plan)
            # Prior incoming facts are nomination-only, never durable target IDs.
            transient_id = uuid.uuid5(record.capture_id, f"fact:{index}")
            transient_indexes[transient_id] = index
            candidates.append(Memory(id=transient_id, user_id=uuid.UUID(int=0), proxy_user_id=record.proxy_user_id,
                content=memory.content, category=MemoryCategory(memory.category), importance_score=5,
                confidence_score=0.5, is_archived=False, created_at=datetime.now(UTC), metadata_json={}))
        for index in conflicted:
            plans[index] = _pending("source_batch_dependency")
        return tuple(plans)

    def _configure_write_transaction(self, session):
        # Protect bounded-context/absence reads against phantoms, not just UPDATE
        # targets. A serialization failure rolls back everything; caller retries
        # the SAME fixed record. Do not refresh or reclassify after 40001.
        session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))

    def _embedding_memories(self, batch, record):
        return (*batch.declarations, *(m for m, p in zip(batch.nonpreferences, record.fact_plans, strict=True)
                                      if p.action != "pending"))

    def _prepare_nonpreferences(self, session, record, backing):
        rows, complete = _targets(session, record)
        return rows if complete and record.fact_context_complete and _bindings(rows) == record.fact_context else None

    def _store_nonpreferences(self, resolver, batch, record, context, writer_args, pending, defer):
        stored, rejected, deferred = [], 0, 0
        writer = _PreparedFacts(session=resolver.session, qdrant_service=None, embedder=resolver.embedder,
            source_messages=resolver.source_messages, llm_service=resolver.llm_service,
            default_source_conversation_id=record.source_conversation_id, provenance_snapshot={
                **resolver.provenance_snapshot, "authority_rules": {"default_priority": 20}})
        writer.tenant_id = record.tenant_id
        versions = {item.memory_id: item.version for item in record.fact_context}
        for memory, plan in zip(batch.nonpreferences, record.fact_plans, strict=True):
            writer.last_pending_candidates = []
            if context is None or plan.action == "pending":
                if context is None:
                    plan = _pending("source_context_stale")
                target = next((r for r in context or () if r.id == plan.target_id), None)
                decision = ConflictDecision(action="CLARIFY", reasoning=plan.reasoning,
                    decision_evidence=plan.decision_evidence, source_review=review_intent(target))
                writer._buffer_source_candidate(memory, decision)
                pending.extend(writer.last_pending_candidates)
                writer.last_pending_candidates = []
                deferred += 1
                continue
            writer.plan = plan
            writer.expected_version = versions.get(plan.target_id)
            result = writer.check_and_store([memory], source_context=SourceMemoryContext(
                memories=tuple(context), complete=True), **writer_args)
            stored.extend(result)
            pending.extend(writer.last_pending_candidates)
            if result:
                context = [r for r in context if not r.is_archived]
                context.extend(resolver.session.get(Memory, uuid.UUID(m.id)) for m in result)
            elif writer.last_pending_candidates:
                deferred += 1
            else:
                rejected += 1
        return stored, {"facts_created": len(stored), "facts_rejected": rejected, "deferred_facts": deferred}
