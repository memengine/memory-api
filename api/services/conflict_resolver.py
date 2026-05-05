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

from api.db.models import AuditAction
from api.db.models import AuditLog
from api.db.models import CrossUserConflict
from api.db.models import Memory
from api.db.models import MemoryCategory
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
    action: Literal["UPDATE", "MERGE", "KEEP_BOTH", "REJECT"]
    reasoning: str
    merged_memory: ExtractedMemory | None = None


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
        llm_router: LLMRouter | None = None,
        llm_service: LLMService | None = None,
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
        self.llm_router = llm_router
        self.llm_service = llm_service or LLMService(
            provider_clients={LLMProvider.GEMINI: self.client} if self.client is not None else None,
            require_provider=False,
            use_state_store=self.client is None,
        )
        self.conflict_detector = ConflictDetector()
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

                decision = self._temporal_conflict_decision(new_memory, existing_memory)
                if decision is None:
                    decision = self._classify_conflict(new_memory, existing_memory, candidate)

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

        return ConflictDecision(
            action=action if action in {"UPDATE", "MERGE", "KEEP_BOTH", "REJECT"} else "KEEP_BOTH",
            reasoning=reasoning,
            merged_memory=merged_memory,
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
        )
        self._record_shared_context_for_stored_memory(
            stored_memory=stored,
            extracted_memory=extracted_memory,
            tenant_id=tenant_id,
            proxy_user_id=proxy_user_id,
        )
        return stored

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
            row = build_cross_user_conflict_row(
                tenant_id=tenant_id,
                user_a_memory_id=str(signal.source_memory_id) if signal.source_memory_id else None,
                user_b_memory_id=new_memory_id,
                entity_type=signal.entity_type,
                entity_value_a=conflict.entity_value_a,
                entity_value_b=conflict.entity_value_b,
            )
            self.session.add(row)
            inserted += 1
            self._dispatch_context_conflict_webhook(
                tenant_id=tenant_id,
                conflict=row,
                source_proxy_user_id=signal.source_proxy_user_id,
                current_proxy_user_id=proxy_user_id,
            )
        return inserted

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
            agent_id=uuid.UUID(agent_id) if agent_id else None,
            content=extracted_memory.content,
            category=MemoryCategory(extracted_memory.category),
            importance_score=extracted_memory.importance_score,
            confidence_score=extracted_memory.confidence,
            embedding_id=str(memory_id),
            embedding_model_id=embedding.model_id,
            previous_version_id=uuid.UUID(previous_version_id) if previous_version_id else None,
            source_conversation_id=resolved_source_conversation_id,
            expires_at=None,
            metadata_json={
                "expiry": extracted_memory.expiry,
                "reasoning": extracted_memory.reasoning,
                "resolution": resolution,
            },
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
