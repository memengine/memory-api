from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Literal

from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import select

from api.db.models import AuditAction
from api.db.models import AuditLog
from api.db.models import ClarificationQueue
from api.db.models import CrossUserConflict
from api.db.models import CrossUserConflictStatus
from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import SharedContextEntityType
from api.db.models import SharedContextSignal
from api.infra.llm_providers.gemini_provider import DEFAULT_GEMINI_EXTRACT_MODEL
from api.infra.llm_router import LLMRouter
from api.services.llm_service import AllProvidersFailedError
from api.services.llm_service import LLMProvider
from api.services.llm_service import LLMService
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID
from api.services.embedding_service import EmbeddingResult
from api.services.conflict_detection import ConflictCandidate
from api.services.conflict_detection import ConflictDetector
from api.services.conflict_detection import ConflictType
from api.services.conflict_detection import SEMANTIC_CONFLICT_THRESHOLD
from api.services.conflict_detection import SharedContextConflict
from api.services.conflict_detection import build_cross_user_conflict_row
from api.services.conflict_detection import classify_conflict_type
from api.services.claim_ledger_service import ClaimLedgerService
from api.services.conflict_decision_evidence import automatic_evidence
from api.services.conflict_decision_evidence import review_evidence
from api.services.conflict_routing.generic_router import GenericEntityRouter
from api.services.conflict_routing.registry import get_router
from api.services.extractor import ExtractedMemory
from api.services.vector_outbox import build_vector_payload
from api.services.vector_outbox import enqueue_vector_delete
from api.services.vector_outbox import enqueue_vector_upsert
from api.services.version_service import VersionService
from api.settings import get_settings


PROMPT_PATH = Path(__file__).with_name("prompts") / "conflict_prompt.txt"
SIMILARITY_THRESHOLD = SEMANTIC_CONFLICT_THRESHOLD


TYPE_SPECIFIC_PROMPTS: dict[ConflictType, str] = {
    ConflictType.FACT_UPDATE: (
        "Memory A: {existing} (stored {days_ago} days ago)\n"
        "Memory B: {new} (just extracted)\n"
        "These appear to be about the same fact at different times.\n"
        "Has the fact changed or is B more specific than A?\n"
        'Return JSON: {{"type":"updated|same","keep":"B|A|both","reason":"one sentence"}}'
    ),
    ConflictType.PREFERENCE_CHANGE: (
        "Memory A: {existing}\n"
        "Memory B: {new}\n"
        "Has this person's preference changed or are these about different things?\n"
        'Return JSON: {{"type":"changed|different_context","keep":"B|A|both","reason":"one sentence"}}'
    ),
    ConflictType.NEGATION: (
        "Memory A: {existing}\n"
        "Memory B: {new}\n"
        "Memory B appears to negate or supersede Memory A.\n"
        "Confirm: should A be archived?\n"
        'Return JSON: {{"archive_A":true|false,"reason":"one sentence"}}'
    ),
    ConflictType.SKILL_PROGRESSION: (
        "Memory A: {existing}\n"
        "Memory B: {new}\n"
        "Memory B may represent skill progression from learning to knowing.\n"
        "Should B supersede A, should both be kept, or are they duplicates?\n"
        'Return JSON: {{"type":"progressed|same|different_context","keep":"B|A|both","reason":"one sentence"}}'
    ),
    ConflictType.NUMERIC_UPDATE: (
        "Memory A: {existing}\n"
        "Memory B: {new}\n"
        "These memories contain numeric values that may represent an updated score, percentage, or metric.\n"
        "Should the newer number supersede the older one?\n"
        'Return JSON: {{"type":"updated|same|different_context","keep":"B|A|both","reason":"one sentence"}}'
    ),
    ConflictType.TEMPORAL_SHIFT: (
        "Memory A: {existing}\n"
        "Memory B: {new}\n"
        "Memory B contains temporal language such as now, recently, yesterday, or last week.\n"
        "Does B supersede A or should both be kept with temporal context?\n"
        'Return JSON: {{"type":"updated|temporal_context|different_context","keep":"B|A|both","reason":"one sentence"}}'
    ),
}


@dataclass(slots=True)
class StoredMemory:
    id: str
    user_id: str
    proxy_user_id: str | None
    content: str
    category: str
    importance_score: float
    confidence_score: float
    previous_version_id: str | None
    resolution: str


@dataclass(slots=True)
class ConflictDecision:
    action: Literal["UPDATE", "MERGE", "KEEP_BOTH", "REJECT", "CLARIFY"]
    reasoning: str
    merged_memory: ExtractedMemory | None = None
    decision_evidence: dict[str, Any] | None = None


@dataclass(slots=True)
class AutoResolutionResult:
    strategy_used: str
    resolution: str
    action_taken: str
    requires_attention: bool
    reason: str
    decision_evidence: dict[str, Any] | None = None


PER_USER_SCOPED_ENTITY_TYPES = {
    "exam_date",
    "grade_level",
    "individual_goal",
    "learning_style",
    "marks_target",
    "personal_fact",
    "personal_goal",
    "personal_preference",
    "personal_skill",
    "study_schedule",
}

PERSONAL_GOAL_SIGNALS = (
    "become ",
    "career",
    "for himself",
    "for herself",
    "for me",
    "internship",
    "is currently looking",
    "job opportunities",
    "learn ",
    "looking for",
    "my ",
    "personal",
    "user is currently looking",
    "user wants",
    "wants to",
)

SHARED_ORG_SIGNALS = (
    "all of us",
    "company",
    "organisation",
    "organization",
    "our ",
    "team",
    "we ",
)


def classify_resolution_path(
    conflict: CrossUserConflict,
    entity_type: str,
    memory_a: Memory,
    memory_b: Memory,
    domain_schema: str | None = None,
) -> Literal["user_session", "tenant_review"]:
    del conflict
    router = get_router(domain_schema)
    routed = router.classify(entity_type, memory_a, memory_b)
    if routed is not None:
        return routed

    if not isinstance(router, GenericEntityRouter):
        fallback = GenericEntityRouter().classify(entity_type, memory_a, memory_b)
        if fallback is not None:
            return fallback

    if memory_a.proxy_user_id == memory_b.proxy_user_id:
        return "user_session"
    return "tenant_review"


def resolve_cross_user_conflict_automatically(
    conflict: CrossUserConflict,
    db_session: Any,
    domain_schema: str | None = None,
) -> AutoResolutionResult:
    entity_type = (
        conflict.entity_type.value
        if hasattr(conflict.entity_type, "value")
        else str(conflict.entity_type)
    )
    memory_a = _load_conflict_memory(db_session, conflict.user_a_memory_id, conflict.user_a_memory)
    memory_b = _load_conflict_memory(db_session, conflict.user_b_memory_id, conflict.user_b_memory)
    if memory_a is None or memory_b is None:
        conflict.status = CrossUserConflictStatus.pending
        conflict.auto_resolution = "requires_attention"
        conflict.auto_resolution_at = datetime.now(UTC)
        conflict.resolution_path = "tenant_review"
        conflict.requires_attention = True
        return AutoResolutionResult(
            strategy_used="requires_attention",
            resolution="unresolvable",
            action_taken="left_pending",
            requires_attention=True,
            reason="One or both memories are unavailable for automatic resolution",
            decision_evidence=_attach_cross_user_decision_evidence(
                conflict,
                review_evidence(
                    action="TENANT_REVIEW",
                    reason_codes=["conflict_memory_missing", "tenant_review_required"],
                    explanation="One or both memories are unavailable, so MemoryOS cannot safely decide automatically.",
                    details={"entity_type": entity_type},
                ),
            ),
        )

    if _is_per_user_scoped_conflict(entity_type, memory_a, memory_b):
        conflict.status = CrossUserConflictStatus.ignored
        conflict.auto_resolution = "per_user_scoped"
        conflict.auto_resolution_at = datetime.now(UTC)
        conflict.resolution_path = "user_session"
        conflict.requires_attention = False
        conflict.resolution_reason = "Personal goals or facts are user-specific"
        return AutoResolutionResult(
            strategy_used="per_user_scoped",
            resolution="per_user_scoped",
            action_taken="ignored_personal_cross_user_conflict",
            requires_attention=False,
            reason="Personal facts are user-specific",
            decision_evidence=_attach_cross_user_decision_evidence(
                conflict,
                automatic_evidence(
                    action="IGNORE",
                    reason_codes=["personal_truth", "per_user_scoped"],
                    explanation="This is personal information, so it should not create an organisation-level conflict.",
                    confidence=0.95,
                    details={"entity_type": entity_type},
                ),
            ),
        )

    created_a = _aware_datetime(memory_a.created_at)
    created_b = _aware_datetime(memory_b.created_at)
    recency_days = abs((created_a - created_b).days)
    if recency_days > 7:
        newer_memory, older_memory = (
            (memory_a, memory_b) if created_a > created_b else (memory_b, memory_a)
        )
        _weight_down_memory(
            older_memory,
            newer_memory,
            reason="Newer claim weighted higher automatically",
            decision_evidence=_attach_cross_user_decision_evidence(
                conflict,
                automatic_evidence(
                    action="UPDATE",
                    reason_codes=["recency_weighted", "recency_safe_to_apply"],
                    explanation="The newer claim was weighted higher because the evidence is more recent.",
                    confidence=0.8,
                    winner_source=str(newer_memory.id),
                    details={"recency_days": recency_days, "entity_type": entity_type},
                ),
            ),
        )
        _mark_conflict_auto_resolved(conflict, "recency_weighted")
        return AutoResolutionResult(
            strategy_used="recency_weighted",
            resolution="recency_weighted",
            action_taken="weighted_down_older_claim",
            requires_attention=False,
            reason="Newer claim weighted higher automatically",
            decision_evidence=_attach_cross_user_decision_evidence(
                conflict,
                automatic_evidence(
                    action="UPDATE",
                    reason_codes=["recency_weighted", "recency_safe_to_apply"],
                    explanation="The newer claim was weighted higher because the evidence is more recent.",
                    confidence=0.8,
                    winner_source=str(newer_memory.id),
                    details={"recency_days": recency_days, "entity_type": entity_type},
                ),
            ),
        )

    confidence_diff = abs(float(memory_a.confidence_score or 0.0) - float(memory_b.confidence_score or 0.0))
    if confidence_diff > 0.20:
        higher_confidence, lower_confidence = (
            (memory_a, memory_b)
            if float(memory_a.confidence_score or 0.0) > float(memory_b.confidence_score or 0.0)
            else (memory_b, memory_a)
        )
        _weight_down_memory(
            lower_confidence,
            higher_confidence,
            reason="Higher-confidence claim weighted higher automatically",
            decision_evidence=_attach_cross_user_decision_evidence(
                conflict,
                automatic_evidence(
                    action="UPDATE",
                    reason_codes=["confidence_weighted", "higher_confidence_source"],
                    explanation="The higher-confidence claim was weighted higher automatically.",
                    confidence=0.75,
                    winner_source=str(higher_confidence.id),
                    details={"confidence_diff": confidence_diff, "entity_type": entity_type},
                ),
            ),
        )
        _mark_conflict_auto_resolved(conflict, "confidence_weighted")
        return AutoResolutionResult(
            strategy_used="confidence_weighted",
            resolution="confidence_weighted",
            action_taken="weighted_down_lower_confidence_claim",
            requires_attention=False,
            reason="Higher-confidence claim weighted higher automatically",
            decision_evidence=_attach_cross_user_decision_evidence(
                conflict,
                automatic_evidence(
                    action="UPDATE",
                    reason_codes=["confidence_weighted", "higher_confidence_source"],
                    explanation="The higher-confidence claim was weighted higher automatically.",
                    confidence=0.75,
                    winner_source=str(higher_confidence.id),
                    details={"confidence_diff": confidence_diff, "entity_type": entity_type},
                ),
            ),
        )

    resolution_path = classify_resolution_path(
        conflict,
        entity_type,
        memory_a,
        memory_b,
        domain_schema,
    )
    conflict.resolution_path = resolution_path
    if resolution_path == "user_session":
        target_memory = memory_a if created_a <= created_b else memory_b
        if target_memory.proxy_user_id:
            _queue_user_session_clarification(
                db_session=db_session,
                conflict=conflict,
                target_memory=target_memory,
                question_context=f"{entity_type}: {conflict.entity_value_a} vs {conflict.entity_value_b}",
            )
            conflict.status = CrossUserConflictStatus.clarification_queued
            conflict.auto_resolution = "clarification_queued"
            conflict.auto_resolution_at = datetime.now(UTC)
            conflict.requires_attention = False
            return AutoResolutionResult(
                strategy_used="clarification_queued",
                resolution="clarification_queued",
                action_taken="queued_clarification",
                requires_attention=False,
                reason="Will ask user to confirm in next session",
                decision_evidence=_attach_cross_user_decision_evidence(
                    conflict,
                    review_evidence(
                        action="USER_REVIEW",
                        reason_codes=["personal_truth_requires_user", "clarification_queued"],
                        explanation="This concerns personal memory, so MemoryOS queued a user-facing clarification.",
                        details={"entity_type": entity_type},
                    ),
                ),
            )

        conflict.status = CrossUserConflictStatus.pending
        conflict.auto_resolution = "requires_attention"
        conflict.auto_resolution_at = datetime.now(UTC)
        conflict.requires_attention = True
        return AutoResolutionResult(
            strategy_used="requires_attention",
            resolution="unresolvable",
            action_taken="left_pending",
            requires_attention=True,
            reason="Unable to queue clarification because no proxy user is attached",
            decision_evidence=_attach_cross_user_decision_evidence(
                conflict,
                review_evidence(
                    action="TENANT_REVIEW",
                    reason_codes=["missing_proxy_user", "tenant_review_required"],
                    explanation="MemoryOS could not identify a user session for clarification, so the conflict needs review.",
                    details={"entity_type": entity_type},
                ),
            ),
        )

    conflict.status = CrossUserConflictStatus.pending
    conflict.auto_resolution = "requires_attention"
    conflict.auto_resolution_at = datetime.now(UTC)
    conflict.resolution_path = "tenant_review"
    conflict.requires_attention = True
    return AutoResolutionResult(
        strategy_used="requires_attention",
        resolution="unresolvable",
        action_taken="left_pending",
        requires_attention=True,
        reason="Tenant review is required for shared organizational context",
        decision_evidence=_attach_cross_user_decision_evidence(
            conflict,
            review_evidence(
                action="TENANT_REVIEW",
                reason_codes=["organisation_truth_requires_tenant", "shared_context_conflict"],
                explanation="This concerns shared organisation context, so MemoryOS routed it to tenant review.",
                details={"entity_type": entity_type},
            ),
        ),
    )


def _attach_cross_user_decision_evidence(
    conflict: CrossUserConflict,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    conflict.decision_evidence = evidence
    return evidence


def _is_per_user_scoped_conflict(entity_type: str, memory_a: Memory, memory_b: Memory) -> bool:
    if entity_type in PER_USER_SCOPED_ENTITY_TYPES:
        return True

    combined = f"{memory_a.content} {memory_b.content}".lower()
    if entity_type == "shared_goal":
        has_personal_goal_signal = any(signal in combined for signal in PERSONAL_GOAL_SIGNALS)
        has_shared_org_signal = any(signal in combined for signal in SHARED_ORG_SIGNALS)
        return has_personal_goal_signal and not has_shared_org_signal

    return False


def _load_conflict_memory(
    db_session: Any,
    memory_id: uuid.UUID | None,
    fallback: Memory | None,
) -> Memory | None:
    if fallback is not None:
        return fallback
    if memory_id is not None and hasattr(db_session, "get"):
        return db_session.get(Memory, memory_id)
    return None


def _aware_datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _weight_down_memory(memory: Memory, superseding_memory: Memory, *, reason: str) -> None:
    memory.importance_score = float(memory.importance_score or 0.0) * 0.5
    metadata = dict(memory.metadata_json or {})
    metadata.update(
        {
            "potentially_outdated": True,
            "superseded_candidate_id": str(superseding_memory.id),
            "auto_resolution_reason": reason,
        }
    )
    memory.metadata_json = metadata


def _queue_user_session_clarification(
    *,
    db_session: Any,
    conflict: CrossUserConflict,
    target_memory: Memory,
    question_context: str,
) -> None:
    db_session.add(
        ClarificationQueue(
            tenant_id=conflict.tenant_id,
            proxy_user_id=target_memory.proxy_user_id,
            question_context=question_context,
            conflict_id=conflict.id,
            trigger_on="next_session",
        )
    )


def _mark_conflict_auto_resolved(conflict: CrossUserConflict, strategy: str) -> None:
    conflict.status = CrossUserConflictStatus.resolved
    conflict.auto_resolution = strategy
    conflict.auto_resolution_at = datetime.now(UTC)
    conflict.requires_attention = False


class ConflictResolver:
    def __init__(
        self,
        *,
        session: Any,
        qdrant_service: Any,
        embedder: Callable[[str], Any],
        client: Any | None = None,
        model: str | None = None,
        prompt_path: Path | None = None,
        default_source_conversation_id: uuid.UUID | None = None,
        default_source_event_id: uuid.UUID | None = None,
        provenance_snapshot: dict[str, Any] | None = None,
        llm_router: LLMRouter | None = None,
        llm_service: LLMService | None = None,
        domain_schema: str | None = None,
    ) -> None:
        self.session = session
        self.qdrant_service = qdrant_service
        self.embedder = embedder
        self.client = client
        configured_model = (get_settings().extraction_model or "").strip()
        self.model = model or configured_model or DEFAULT_GEMINI_EXTRACT_MODEL
        self.prompt_path = prompt_path or PROMPT_PATH
        self.system_prompt = self.prompt_path.read_text(encoding="utf-8")
        self.default_source_conversation_id = default_source_conversation_id
        self.default_source_event_id = default_source_event_id
        self.provenance_snapshot = provenance_snapshot
        self.llm_router = llm_router
        self.llm_service = llm_service or LLMService(
            provider_clients={LLMProvider.GEMINI: self.client} if self.client is not None else None,
            require_provider=False,
            use_state_store=self.client is None,
        )
        self.conflict_detector = ConflictDetector()
        self.domain_schema = domain_schema or os.getenv("MEMORYOS_DOMAIN_SCHEMA")
        self.last_cross_user_conflicts_flagged = 0
        self.last_detection_strategies_used: list[str] = []
        self.last_conflict_types_found: list[str] = []

    def check_and_store(
        self,
        new_memories: list[ExtractedMemory],
        user_id: str,
        tenant_id: str | None = None,
        proxy_user_id: str | None = None,
        source_conversation_id: str | None = None,
        agent_id: str | None = None,
        auto_commit: bool = True,
    ) -> list[StoredMemory]:
        stored_memories: list[StoredMemory] = []
        self.last_cross_user_conflicts_flagged = 0
        self.last_detection_strategies_used = []
        self.last_conflict_types_found = []

        for new_memory in new_memories:
            embedding = self._coerce_embedding_result(self.embedder(new_memory.content))
            search_kwargs: dict[str, Any] = {
                "query_embedding": embedding.vector,
                "limit": 20,
                "include_archived": False,
            }
            if embedding.qdrant_collection:
                search_kwargs["collection_name"] = embedding.qdrant_collection
            if tenant_id and proxy_user_id:
                search_kwargs["tenant_id"] = tenant_id
                search_kwargs["proxy_user_id"] = proxy_user_id
            else:
                search_kwargs["user_id"] = user_id
            try:
                raw_candidates = self.qdrant_service.search_memories(**search_kwargs)
            except TypeError:
                search_kwargs.pop("collection_name", None)
                raw_candidates = self.qdrant_service.search_memories(**search_kwargs)

            candidate_memories: list[Memory] = []
            for point in raw_candidates:
                existing_memory = self._load_existing_memory(point)
                if existing_memory is None:
                    continue
                try:
                    setattr(
                        existing_memory,
                        "_conflict_similarity_score",
                        float(getattr(point, "score", 0.0) or 0.0),
                    )
                except Exception:
                    pass
                candidate_memories.append(existing_memory)

            candidates = self.conflict_detector.detect_candidates(new_memory, candidate_memories)

            decision_applied = False
            for candidate in candidates:
                existing_memory = candidate.existing_memory

                decision = self._authority_conflict_decision(new_memory, existing_memory)
                equal_authority_conflict = self._is_equal_authority_cross_writer_conflict(
                    new_memory,
                    existing_memory,
                )
                if decision is None and equal_authority_conflict:
                    decision = ConflictDecision(
                        action="CLARIFY",
                        reasoning=(
                            "Registered services with equal authority reported "
                            "different values for the same user."
                        ),
                        decision_evidence=review_evidence(
                            action="TENANT_REVIEW",
                            reason_codes=[
                                "equal_authority_disagreement",
                                "cross_writer_conflict",
                                "human_review_required",
                            ],
                            explanation=(
                                "Two registered services with equal authority disagree, so "
                                "MemoryOS preserved both claims and routed the decision for review."
                            ),
                        ),
                    )
                if decision is None:
                    decision = self._temporal_conflict_decision(new_memory, existing_memory)
                if decision is None:
                    decision = self._classify_conflict(new_memory, existing_memory, candidate)

                decision_evidence = decision.decision_evidence or self._fallback_decision_evidence(
                    decision=decision,
                    candidate=candidate,
                    new_memory=new_memory,
                    existing_memory=existing_memory,
                )

                if decision.action == "CLARIFY":
                    pending = self._store_new_memory_with_shared_context(
                        extracted_memory=new_memory,
                        user_id=user_id,
                        proxy_user_id=proxy_user_id,
                        tenant_id=tenant_id,
                        embedding=embedding,
                        previous_version_id=str(existing_memory.id),
                        resolution="CLARIFICATION_PENDING",
                        source_conversation_id=source_conversation_id,
                        agent_id=agent_id,
                        is_archived=True,
                        record_shared_context=False,
                        decision_evidence=decision_evidence,
                    )
                    self._create_equal_authority_conflict(
                        tenant_id=tenant_id,
                        proxy_user_id=proxy_user_id,
                        existing_memory=existing_memory,
                        pending_memory_id=pending.id,
                        category=new_memory.category,
                    )
                    stored_memories.append(pending)
                    self._create_audit_log(
                        user_id=user_id,
                        proxy_user_id=proxy_user_id,
                        action=AuditAction.memory_created,
                        old_value=self._serialize_memory(existing_memory),
                        new_value={
                            "resolution": "CLARIFICATION_PENDING",
                            "reasoning": decision.reasoning,
                            "pending_memory_id": pending.id,
                            "new_memory": self._serialize_extracted_memory(new_memory),
                        },
                        memory_id=uuid.UUID(pending.id),
                    )
                    self._track_candidate_metadata(candidate, new_memory, existing_memory)
                    decision_applied = True
                    break

                if decision.action == "UPDATE":
                    self._archive_memory(
                        existing_memory,
                        change_reason=f"Superseded by: {new_memory.content[:100]}",
                    )
                    stored_memories.append(
                        self._store_new_memory_with_shared_context(
                            extracted_memory=new_memory,
                            user_id=user_id,
                            proxy_user_id=proxy_user_id,
                            tenant_id=tenant_id,
                            embedding=embedding,
                            previous_version_id=str(existing_memory.id),
                            resolution="UPDATE",
                            source_conversation_id=source_conversation_id,
                            agent_id=agent_id,
                            decision_evidence=decision_evidence,
                        )
                    )
                    self._create_audit_log(
                        user_id=user_id,
                        proxy_user_id=proxy_user_id,
                        action=AuditAction.updated,
                        old_value=self._serialize_memory(existing_memory),
                        new_value={
                            "resolution": "UPDATE",
                            "reasoning": decision.reasoning,
                            "detection_strategy": candidate.detection_strategy,
                            "detected_entities": candidate.detected_entities,
                            "new_memory": self._serialize_extracted_memory(new_memory),
                        },
                        memory_id=existing_memory.id,
                    )
                    self._track_candidate_metadata(candidate, new_memory, existing_memory)
                    decision_applied = True
                    break

                if decision.action == "MERGE":
                    merged_memory = self._build_merged_memory(
                        existing_memory=existing_memory,
                        new_memory=new_memory,
                        merged_memory=decision.merged_memory,
                    )
                    self._archive_memory(
                        existing_memory,
                        change_reason=f"Superseded by: {merged_memory.content[:100]}",
                    )
                    stored_memories.append(
                        self._store_new_memory_with_shared_context(
                            extracted_memory=merged_memory,
                            user_id=user_id,
                            proxy_user_id=proxy_user_id,
                            tenant_id=tenant_id,
                            embedding=self._coerce_embedding_result(self.embedder(merged_memory.content)),
                            previous_version_id=str(existing_memory.id),
                            resolution="MERGE",
                            source_conversation_id=source_conversation_id,
                            agent_id=agent_id,
                            decision_evidence=decision_evidence,
                        )
                    )
                    self._create_audit_log(
                        user_id=user_id,
                        proxy_user_id=proxy_user_id,
                        action=AuditAction.updated,
                        old_value={
                            "existing_memory": self._serialize_memory(existing_memory),
                            "incoming_memory": self._serialize_extracted_memory(new_memory),
                        },
                        new_value={
                            "resolution": "MERGE",
                            "reasoning": decision.reasoning,
                            "detection_strategy": candidate.detection_strategy,
                            "detected_entities": candidate.detected_entities,
                            "merged_memory": self._serialize_extracted_memory(merged_memory),
                        },
                        memory_id=existing_memory.id,
                    )
                    self._track_candidate_metadata(candidate, new_memory, existing_memory)
                    decision_applied = True
                    break

                if decision.action == "REJECT":
                    self._create_audit_log(
                        user_id=user_id,
                        proxy_user_id=proxy_user_id,
                        action=AuditAction.deleted,
                        old_value=self._serialize_extracted_memory(new_memory),
                        new_value={
                            "resolution": "REJECT",
                            "reasoning": decision.reasoning,
                            "detection_strategy": candidate.detection_strategy,
                            "detected_entities": candidate.detected_entities,
                        },
                        memory_id=existing_memory.id,
                    )
                    self._track_candidate_metadata(candidate, new_memory, existing_memory)
                    decision_applied = True
                    break

            if not decision_applied:
                stored_memories.append(
                    self._store_new_memory_with_shared_context(
                        extracted_memory=new_memory,
                        user_id=user_id,
                        proxy_user_id=proxy_user_id,
                        tenant_id=tenant_id,
                        embedding=embedding,
                        previous_version_id=None,
                        resolution="KEEP_BOTH" if candidates else "NEW",
                        source_conversation_id=source_conversation_id,
                        agent_id=agent_id,
                    )
                )
                self._create_audit_log(
                    user_id=user_id,
                    proxy_user_id=proxy_user_id,
                    action=AuditAction.memory_created,
                    old_value=None,
                    new_value={
                        "resolution": "KEEP_BOTH" if candidates else "NEW",
                        "new_memory": self._serialize_extracted_memory(new_memory),
                    },
                    memory_id=None,
                )

            if auto_commit and hasattr(self.session, "commit"):
                self.session.commit()

        return stored_memories

    def _track_candidate_metadata(
        self,
        candidate: ConflictCandidate,
        new_memory: ExtractedMemory,
        existing_memory: Memory,
    ) -> None:
        if candidate.detection_strategy not in self.last_detection_strategies_used:
            self.last_detection_strategies_used.append(candidate.detection_strategy)
        conflict_type = classify_conflict_type(
            new_content=new_memory.content,
            existing_content=existing_memory.content,
            category=new_memory.category,
            detected_entities=candidate.detected_entities,
        )
        if conflict_type.value not in self.last_conflict_types_found:
            self.last_conflict_types_found.append(conflict_type.value)

    def _fallback_decision_evidence(
        self,
        *,
        decision: ConflictDecision,
        candidate: ConflictCandidate,
        new_memory: ExtractedMemory,
        existing_memory: Memory,
    ) -> dict[str, Any]:
        conflict_type = classify_conflict_type(
            new_content=new_memory.content,
            existing_content=existing_memory.content,
            category=new_memory.category,
            detected_entities=candidate.detected_entities,
        )
        action = decision.action if decision.action != "CLARIFY" else "TENANT_REVIEW"
        reason_codes = [
            "semantic_conflict_detected",
            f"conflict_type:{conflict_type.value}",
            f"decision:{decision.action.lower()}",
        ]
        return automatic_evidence(
            action=action,  # type: ignore[arg-type]
            reason_codes=reason_codes,
            explanation=decision.reasoning,
            confidence=None,
            details={
                "detection_strategy": candidate.detection_strategy,
                "detected_entities": candidate.detected_entities,
                "incoming_category": new_memory.category,
                "existing_memory_id": str(existing_memory.id),
            },
        )
    def _load_existing_memory(self, point: Any) -> Memory | None:
        payload = getattr(point, "payload", {}) or {}
        memory_id = payload.get("memory_id") or getattr(point, "id", None)
        if memory_id is None:
            return None

        if hasattr(self.session, "get"):
            return self.session.get(Memory, memory_id)
        return None

    def _classify_conflict(
        self,
        new_memory: ExtractedMemory,
        existing_memory: Memory,
        candidate: ConflictCandidate | None = None,
    ) -> ConflictDecision:
        conflict_type = classify_conflict_type(
            new_content=new_memory.content,
            existing_content=existing_memory.content,
            category=new_memory.category,
            detected_entities=candidate.detected_entities if candidate is not None else [],
        )
        content = self._build_conflict_user_prompt(
            new_memory=new_memory,
            existing_memory=existing_memory,
            conflict_type=conflict_type,
        )

        try:
            response = self.llm_service.complete_sync(
                system_prompt=self._system_prompt_for_conflict_type(conflict_type),
                user_message=content,
                temperature=0.0,
                max_tokens=200,
                response_format="json",
            )
            raw_content = response.content
        except AllProvidersFailedError:
            raw_content = "{}"

        payload = json.loads(raw_content or "{}")
        action = self._action_from_payload(payload)
        reasoning = str(payload.get("reasoning") or payload.get("reason") or "").strip() or "No reasoning provided."
        merged_payload = payload.get("merged_memory")

        merged_memory = None
        if merged_payload:
            merged_memory = ExtractedMemory(
                content=str(merged_payload["content"]).strip(),
                category=str(merged_payload["category"]).strip().lower(),
                importance_score=float(merged_payload["importance_score"]),
                confidence=float(merged_payload["confidence"]),
                expiry=str(merged_payload["expiry"]).strip().lower(),  # type: ignore[arg-type]
                reasoning=str(merged_payload["reasoning"]).strip(),
            )

        normalized_action = action if action in {"UPDATE", "MERGE", "KEEP_BOTH", "REJECT"} else "KEEP_BOTH"
        return ConflictDecision(
            action=normalized_action,
            reasoning=reasoning,
            merged_memory=merged_memory,
            decision_evidence=automatic_evidence(
                action=normalized_action,  # type: ignore[arg-type]
                reason_codes=[
                    "semantic_conflict_classified",
                    f"conflict_type:{conflict_type.value}",
                    f"decision:{normalized_action.lower()}",
                ],
                explanation=reasoning,
                confidence=None,
                details={
                    "classifier": "llm",
                    "raw_action": action,
                    "conflict_type": conflict_type.value,
                },
            ),
        )

    def _build_conflict_user_prompt(
        self,
        *,
        new_memory: ExtractedMemory,
        existing_memory: Memory,
        conflict_type: ConflictType,
    ) -> str:
        template = TYPE_SPECIFIC_PROMPTS.get(conflict_type)
        if template is None:
            return json.dumps(
                {
                    "existing": self._serialize_memory(existing_memory),
                    "new": self._serialize_extracted_memory(new_memory),
                    "conflict_type": conflict_type.value,
                }
            )

        return template.format(
            existing=existing_memory.content,
            new=new_memory.content,
            days_ago=self._days_since_created(existing_memory),
        )

    def _system_prompt_for_conflict_type(self, conflict_type: ConflictType) -> str:
        if conflict_type == ConflictType.UNKNOWN:
            return self.system_prompt
        return (
            "You are the MemoryOS conflict resolution engine. "
            "Return only valid JSON matching the requested schema. "
            "Do not include markdown or explanation outside JSON."
        )

    @staticmethod
    def _action_from_payload(payload: dict[str, Any]) -> str:
        if payload.get("archive_A") is True:
            return "UPDATE"
        if payload.get("archive_A") is False:
            return "KEEP_BOTH"

        action = payload.get("action")
        if action:
            return str(action).upper()

        keep = str(payload.get("keep", "")).upper()
        conflict_type = str(payload.get("type", "")).lower()
        if keep == "B":
            return "REJECT" if conflict_type == "same" else "UPDATE"
        if keep == "A":
            return "REJECT"
        if keep == "BOTH":
            return "KEEP_BOTH"
        return "KEEP_BOTH"

    @staticmethod
    def _days_since_created(memory: Memory) -> int:
        created_at = getattr(memory, "created_at", None)
        if created_at is None:
            return 0
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return max(0, (datetime.now(UTC) - created_at).days)

    @staticmethod
    def _build_merged_memory(
        *,
        existing_memory: Memory,
        new_memory: ExtractedMemory,
        merged_memory: ExtractedMemory | None,
    ) -> ExtractedMemory:
        merged = merged_memory or new_memory
        return ExtractedMemory(
            content=merged.content,
            category=merged.category,
            importance_score=min(
                10.0,
                max(existing_memory.importance_score, new_memory.importance_score) + 0.5,
            ),
            confidence=merged.confidence,
            expiry=merged.expiry,
            reasoning=merged.reasoning,
        )

    @staticmethod
    def _authority_priority(
        provenance: dict[str, Any] | None,
        category: str,
    ) -> int:
        rules = dict((provenance or {}).get("authority_rules") or {})
        category_rules = dict(rules.get("categories") or {})
        raw_priority = category_rules.get(category, rules.get("default_priority", 50))
        try:
            return max(0, min(int(raw_priority), 100))
        except (TypeError, ValueError):
            return 50

    def _authority_conflict_decision(
        self,
        new_memory: ExtractedMemory,
        existing_memory: Memory,
    ) -> ConflictDecision | None:
        incoming_priority = self._authority_priority(
            self.provenance_snapshot,
            new_memory.category,
        )
        existing_priority = self._authority_priority(
            dict(existing_memory.metadata_json or {}).get("provenance"),
            existing_memory.category.value,
        )
        if incoming_priority > existing_priority:
            return ConflictDecision(
                action="UPDATE",
                reasoning=(
                    f"Incoming writer authority {incoming_priority} exceeds "
                    f"stored writer authority {existing_priority}."
                ),
                decision_evidence=automatic_evidence(
                    action="UPDATE",
                    reason_codes=["higher_authority_source", "incoming_source_wins"],
                    explanation="Incoming source has higher authority for this memory category.",
                    confidence=1.0,
                    winner_source="incoming",
                    details={
                        "incoming_authority": incoming_priority,
                        "existing_authority": existing_priority,
                    },
                ),
            )
        if incoming_priority < existing_priority:
            return ConflictDecision(
                action="REJECT",
                reasoning=(
                    f"Stored writer authority {existing_priority} exceeds "
                    f"incoming writer authority {incoming_priority}."
                ),
                decision_evidence=automatic_evidence(
                    action="REJECT",
                    reason_codes=["higher_authority_source", "stored_source_wins"],
                    explanation="Stored source has higher authority for this memory category.",
                    confidence=1.0,
                    winner_source="stored",
                    details={
                        "incoming_authority": incoming_priority,
                        "existing_authority": existing_priority,
                    },
                ),
            )
        incoming_observed_at = self._provenance_observed_at(self.provenance_snapshot)
        existing_observed_at = self._provenance_observed_at(
            dict(existing_memory.metadata_json or {}).get("provenance")
        )
        if (
            incoming_observed_at is not None
            and existing_observed_at is not None
            and incoming_observed_at < existing_observed_at
        ):
            return ConflictDecision(
                action="REJECT",
                reasoning=(
                    f"Incoming event observed at {incoming_observed_at.isoformat()} is older "
                    f"than stored evidence observed at {existing_observed_at.isoformat()}."
                ),
                decision_evidence=automatic_evidence(
                    action="REJECT",
                    reason_codes=["older_source_event", "recency_safe_to_apply"],
                    explanation="Incoming evidence is older than the currently stored source event.",
                    confidence=1.0,
                    winner_source="stored",
                    details={
                        "incoming_observed_at": incoming_observed_at.isoformat(),
                        "existing_observed_at": existing_observed_at.isoformat(),
                    },
                ),
            )
        return None

    def _is_equal_authority_cross_writer_conflict(
        self,
        new_memory: ExtractedMemory,
        existing_memory: Memory,
    ) -> bool:
        incoming = dict(self.provenance_snapshot or {})
        existing = dict(
            dict(existing_memory.metadata_json or {}).get("provenance") or {}
        )
        incoming_writer = incoming.get("writer_id")
        existing_writer = existing.get("writer_id")
        if (
            not incoming_writer
            or not existing_writer
            or str(incoming_writer) == str(existing_writer)
        ):
            return False
        if new_memory.content.strip().casefold() == existing_memory.content.strip().casefold():
            return False
        return self._authority_priority(
            incoming,
            new_memory.category,
        ) == self._authority_priority(
            existing,
            existing_memory.category.value,
        )

    @staticmethod
    def _provenance_observed_at(provenance: dict[str, Any] | None) -> datetime | None:
        raw_value = (provenance or {}).get("observed_at")
        if not raw_value:
            return None
        try:
            value = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _temporal_conflict_decision(
        new_memory: ExtractedMemory,
        existing_memory: Memory,
    ) -> ConflictDecision | None:
        year_pattern = re.compile(r"\b(?:19|20)\d{2}\b")
        new_years = set(year_pattern.findall(new_memory.content))
        existing_years = set(year_pattern.findall(existing_memory.content))

        if new_years and existing_years and new_years != existing_years:
            return ConflictDecision(
                action="KEEP_BOTH",
                reasoning="Temporal context differs across memories; keep both with temporal context.",
                decision_evidence=automatic_evidence(
                    action="KEEP_BOTH",
                    reason_codes=["temporal_context_differs", "both_claims_contextually_valid"],
                    explanation="The claims describe different time periods, so both are preserved.",
                    confidence=0.9,
                    details={"incoming_years": sorted(new_years), "existing_years": sorted(existing_years)},
                ),
            )
        return None

    def _store_new_memory_with_shared_context(
        self,
        *,
        extracted_memory: ExtractedMemory,
        user_id: str,
        proxy_user_id: str | None,
        tenant_id: str | None,
        embedding: EmbeddingResult,
        previous_version_id: str | None,
        resolution: str,
        source_conversation_id: str | None,
        agent_id: str | None,
        is_archived: bool = False,
        record_shared_context: bool = True,
        decision_evidence: dict[str, Any] | None = None,
    ) -> StoredMemory:
        stored = self._store_new_memory(
            extracted_memory=extracted_memory,
            user_id=user_id,
            proxy_user_id=proxy_user_id,
            tenant_id=tenant_id,
            embedding=embedding,
            previous_version_id=previous_version_id,
            resolution=resolution,
            source_conversation_id=source_conversation_id,
            agent_id=agent_id,
            is_archived=is_archived,
            decision_evidence=decision_evidence,
        )
        if record_shared_context:
            self._record_shared_context_for_stored_memory(
                stored_memory=stored,
                extracted_memory=extracted_memory,
                tenant_id=tenant_id,
                proxy_user_id=proxy_user_id,
            )
        return stored

    def _create_equal_authority_conflict(
        self,
        *,
        tenant_id: str | None,
        proxy_user_id: str | None,
        existing_memory: Memory,
        pending_memory_id: str,
        category: str,
    ) -> None:
        if tenant_id is None or proxy_user_id is None:
            return
        pending_memory = self.session.get(Memory, uuid.UUID(pending_memory_id))
        if pending_memory is None:
            return
        entity_type = {
            "preference": SharedContextEntityType.personal_preference,
            "goal": SharedContextEntityType.individual_goal,
            "expertise": SharedContextEntityType.personal_skill,
        }.get(category, SharedContextEntityType.personal_fact)
        conflict = CrossUserConflict(
            tenant_id=uuid.UUID(tenant_id),
            user_a_memory_id=existing_memory.id,
            user_b_memory_id=pending_memory.id,
            entity_type=entity_type,
            entity_value_a=existing_memory.content,
            entity_value_b=pending_memory.content,
            status=CrossUserConflictStatus.pending,
            auto_resolution="equal_authority_clarification",
            auto_resolution_at=datetime.now(UTC),
            resolution_path="tenant_review",
            requires_attention=True,
            decision_evidence=review_evidence(
                action="TENANT_REVIEW",
                reason_codes=[
                    "equal_authority_disagreement",
                    "cross_writer_conflict",
                    "human_review_required",
                ],
                explanation=(
                    "Two registered services with equal authority reported different values. "
                    "MemoryOS preserved both claims and routed this conflict for review."
                ),
                details={"category": category},
            ),
        )
        conflict.user_a_memory = existing_memory
        conflict.user_b_memory = pending_memory
        self.session.add(conflict)
        if hasattr(self.session, "flush"):
            self.session.flush()
        _queue_user_session_clarification(
            db_session=self.session,
            conflict=conflict,
            target_memory=existing_memory,
            question_context=(
                "Two trusted services reported different values. "
                "Which memory is correct?"
            ),
        )
        self.last_cross_user_conflicts_flagged += 1

    def _record_shared_context_for_stored_memory(
        self,
        *,
        stored_memory: StoredMemory,
        extracted_memory: ExtractedMemory,
        tenant_id: str | None,
        proxy_user_id: str | None,
    ) -> None:
        if tenant_id is None or proxy_user_id is None:
            return
        try:
            memory_id = stored_memory.id
            conflicts = self.conflict_detector.detect_shared_context_conflict(
                session=self.session,
                new_memory=extracted_memory,
                proxy_user_id=proxy_user_id,
                tenant_id=tenant_id,
            )
            self._insert_shared_context_signals(
                extracted_memory=extracted_memory,
                tenant_id=tenant_id,
                proxy_user_id=proxy_user_id,
                memory_id=memory_id,
            )
            if conflicts:
                self.last_cross_user_conflicts_flagged += self._insert_cross_user_conflicts(
                    conflicts=conflicts,
                    tenant_id=tenant_id,
                    new_memory_id=memory_id,
                    proxy_user_id=proxy_user_id,
                )
        except Exception:
            # Cross-user review signals are advisory; never block normal memory storage.
            return

    def _insert_shared_context_signals(
        self,
        *,
        extracted_memory: ExtractedMemory,
        tenant_id: str,
        proxy_user_id: str,
        memory_id: str,
    ) -> None:
        if not hasattr(self.session, "add"):
            return
        for entity in self.conflict_detector.extract_shared_context_entities(extracted_memory):
            self.session.add(
                SharedContextSignal(
                    tenant_id=uuid.UUID(tenant_id),
                    entity_type=entity.entity_type,
                    entity_value=entity.entity_value,
                    source_proxy_user_id=uuid.UUID(proxy_user_id),
                    source_memory_id=uuid.UUID(memory_id),
                    confidence=entity.confidence,
                )
            )

    def _insert_cross_user_conflicts(
        self,
        *,
        conflicts: list[SharedContextConflict],
        tenant_id: str,
        new_memory_id: str,
        proxy_user_id: str,
    ) -> int:
        if not hasattr(self.session, "add"):
            return 0
        inserted = 0
        for conflict in conflicts:
            signal = conflict.conflicting_signal
            user_a_memory_id = str(signal.source_memory_id) if signal.source_memory_id else None
            if self._cross_user_conflict_exists(
                tenant_id=tenant_id,
                user_a_memory_id=user_a_memory_id,
                user_b_memory_id=new_memory_id,
                entity_type=signal.entity_type,
            ):
                continue
            row = build_cross_user_conflict_row(
                tenant_id=tenant_id,
                user_a_memory_id=user_a_memory_id,
                user_b_memory_id=new_memory_id,
                entity_type=signal.entity_type,
                entity_value_a=conflict.entity_value_a,
                entity_value_b=conflict.entity_value_b,
            )
            self.session.add(row)
            if hasattr(self.session, "flush"):
                self.session.flush()
            result = resolve_cross_user_conflict_automatically(
                row,
                self.session,
                domain_schema=self.domain_schema,
            )
            inserted += 1
            if result.requires_attention:
                self._dispatch_context_conflict_webhook(
                    tenant_id=tenant_id,
                    conflict=row,
                    source_proxy_user_id=signal.source_proxy_user_id,
                    current_proxy_user_id=proxy_user_id,
                )
        return inserted

    def _cross_user_conflict_exists(
        self,
        *,
        tenant_id: str,
        user_a_memory_id: str | None,
        user_b_memory_id: str,
        entity_type: Any,
    ) -> bool:
        if not hasattr(self.session, "execute") or user_a_memory_id is None:
            return False

        tenant_uuid = uuid.UUID(str(tenant_id))
        memory_a_uuid = uuid.UUID(str(user_a_memory_id))
        memory_b_uuid = uuid.UUID(str(user_b_memory_id))
        stmt = select(CrossUserConflict.id).where(
            CrossUserConflict.tenant_id == tenant_uuid,
            CrossUserConflict.entity_type == entity_type,
            or_(
                and_(
                    CrossUserConflict.user_a_memory_id == memory_a_uuid,
                    CrossUserConflict.user_b_memory_id == memory_b_uuid,
                ),
                and_(
                    CrossUserConflict.user_a_memory_id == memory_b_uuid,
                    CrossUserConflict.user_b_memory_id == memory_a_uuid,
                ),
            ),
        ).limit(1)
        result = self.session.execute(stmt)
        if hasattr(result, "scalar_one_or_none"):
            return result.scalar_one_or_none() is not None
        if hasattr(result, "scalars"):
            scalars = result.scalars()
            if hasattr(scalars, "first"):
                return scalars.first() is not None
            if hasattr(scalars, "all"):
                return any(
                    isinstance(row, (uuid.UUID, CrossUserConflict, str))
                    for row in scalars.all()
                )
        return False

    @staticmethod
    def _dispatch_context_conflict_webhook(
        *,
        tenant_id: str,
        conflict: CrossUserConflict,
        source_proxy_user_id: uuid.UUID | None,
        current_proxy_user_id: str,
    ) -> None:
        try:
            from api.tasks.quota_tasks import send_webhook_event

            send_webhook_event.delay(
                tenant_id,
                "context.conflict_detected",
                {
                    "entity_type": conflict.entity_type.value,
                    "value_a": conflict.entity_value_a,
                    "value_b": conflict.entity_value_b,
                    "user_a_truncated_id": str(source_proxy_user_id or "")[:8],
                    "user_b_truncated_id": str(current_proxy_user_id)[:8],
                },
            )
        except Exception:
            return

    def _store_new_memory(
        self,
        *,
        extracted_memory: ExtractedMemory,
        user_id: str,
        proxy_user_id: str | None,
        tenant_id: str | None,
        embedding: EmbeddingResult,
        previous_version_id: str | None,
        resolution: str,
        source_conversation_id: str | None,
        agent_id: str | None,
        is_archived: bool = False,
        decision_evidence: dict[str, Any] | None = None,
    ) -> StoredMemory:
        memory_id = uuid.uuid4()
        resolved_source_conversation_id = (
            uuid.UUID(source_conversation_id)
            if source_conversation_id
            else (self.default_source_conversation_id or uuid.uuid4())
        )

        memory = Memory(
            id=memory_id,
            user_id=uuid.UUID(user_id) if self._is_uuid(user_id) else user_id,  # type: ignore[arg-type]
            proxy_user_id=(
                uuid.UUID(proxy_user_id)
                if proxy_user_id is not None
                else uuid.uuid4()
            ),
            agent_id=uuid.UUID(agent_id) if agent_id and self._is_uuid(agent_id) else None,
            content=extracted_memory.content,
            category=MemoryCategory(extracted_memory.category),
            importance_score=extracted_memory.importance_score,
            confidence_score=extracted_memory.confidence,
            embedding_id=str(memory_id),
            embedding_model_id=embedding.model_id,
            previous_version_id=uuid.UUID(previous_version_id) if previous_version_id else None,
            source_conversation_id=resolved_source_conversation_id,
            source_event_id=self.default_source_event_id,
            expires_at=None,
            metadata_json={
                "expiry": extracted_memory.expiry,
                "reasoning": extracted_memory.reasoning,
                "resolution": resolution,
                **({"decision_evidence": decision_evidence} if decision_evidence else {}),
                **(
                    {"provenance": self.provenance_snapshot}
                    if self.provenance_snapshot is not None
                    else {}
                ),
            },
            is_archived=is_archived,
        )

        self.session.add(memory)
        if hasattr(self.session, "flush"):
            self.session.flush()

        VersionService(self.session).safe_record_version(
            memory,
            "created",
            "Extracted from conversation",
            "system",
        )

        self._record_claim_for_memory(
            memory=memory,
            tenant_id=tenant_id,
            proxy_user_id=str(memory.proxy_user_id),
            resolution=resolution,
        )

        if not is_archived:
            enqueue_vector_upsert(
                self.session,
                memory_id=memory_id,
                embedding=embedding.vector,
                payload=build_vector_payload(
                    memory,
                    user_id=str(user_id),
                    tenant_id=tenant_id,
                    proxy_user_id=proxy_user_id,
                    embedding_model_id=embedding.model_id,
                    qdrant_collection=embedding.qdrant_collection,
                ),
            )

        return StoredMemory(
            id=str(memory_id),
            user_id=str(user_id),
            proxy_user_id=proxy_user_id,
            content=memory.content,
            category=memory.category.value,
            importance_score=memory.importance_score,
            confidence_score=memory.confidence_score,
            previous_version_id=previous_version_id,
            resolution=resolution,
        )

    def _record_claim_for_memory(
        self,
        *,
        memory: Memory,
        tenant_id: str | None,
        proxy_user_id: str | None,
        resolution: str,
    ) -> None:
        if tenant_id is None or proxy_user_id is None:
            return
        try:
            ClaimLedgerService(self.session).record_memory(
                memory,
                tenant_id=tenant_id,
                proxy_user_id=proxy_user_id,
                provenance=self.provenance_snapshot,
                resolution=resolution,
                decision_evidence=decision_evidence,
            )
        except Exception:
            # The claim ledger is governance metadata. It must never block the
            # primary memory write path.
            return

    def _archive_memory(self, memory: Memory, change_reason: str | None = None) -> None:
        VersionService(self.session).safe_record_version(
            memory,
            "conflict_update",
            change_reason or "Superseded by a newer extracted memory",
            "system",
        )
        memory.is_archived = True
        qdrant_collection = None
        try:
            qdrant_collection = (
                memory.embedding_model.qdrant_collection
                if getattr(memory, "embedding_model", None) is not None
                else None
            )
        except Exception:
            qdrant_collection = None
        enqueue_vector_delete(
            self.session,
            memory_id=memory.id,
            payload={
                "memory_id": str(memory.id),
                "embedding_model_id": getattr(memory, "embedding_model_id", None),
                "qdrant_collection": qdrant_collection,
            },
        )
        if hasattr(self.session, "add"):
            self.session.add(memory)

    def _create_audit_log(
        self,
        *,
        user_id: str,
        proxy_user_id: str | None,
        action: AuditAction,
        old_value: dict[str, Any] | None,
        new_value: dict[str, Any] | None,
        memory_id: uuid.UUID | None,
    ) -> None:
        audit_log = AuditLog(
            id=uuid.uuid4(),
            user_id=uuid.UUID(user_id) if self._is_uuid(user_id) else user_id,  # type: ignore[arg-type]
            proxy_user_id=uuid.UUID(proxy_user_id) if proxy_user_id else None,
            action=action,
            memory_id=memory_id,
            old_value=old_value,
            new_value=new_value,
            ip_address=None,
        )
        self.session.add(audit_log)

    @staticmethod
    def _serialize_extracted_memory(memory: ExtractedMemory) -> dict[str, Any]:
        return {
            "content": memory.content,
            "category": memory.category,
            "importance_score": memory.importance_score,
            "confidence": memory.confidence,
            "expiry": memory.expiry,
            "reasoning": memory.reasoning,
        }

    @staticmethod
    def _serialize_memory(memory: Memory) -> dict[str, Any]:
        return {
            "id": str(memory.id),
            "content": memory.content,
            "category": memory.category.value if isinstance(memory.category, MemoryCategory) else str(memory.category),
            "importance_score": memory.importance_score,
            "confidence_score": memory.confidence_score,
            "previous_version_id": str(memory.previous_version_id) if memory.previous_version_id else None,
            "is_archived": memory.is_archived,
        }

    @staticmethod
    def _is_uuid(value: str) -> bool:
        try:
            uuid.UUID(str(value))
            return True
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _coerce_embedding_result(value: Any) -> EmbeddingResult:
        if isinstance(value, EmbeddingResult):
            return value
        if isinstance(value, list):
            return EmbeddingResult(
                vector=[float(item) for item in value],
                model_id=DEFAULT_ACTIVE_MODEL_ID,
                dimensions=len(value),
                qdrant_collection=os.getenv("QDRANT_COLLECTION", "memories"),
            )
        raise TypeError("Embedder must return EmbeddingResult or list[float].")
