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
from api.db.models import Memory
from api.db.models import MemoryCategory
from api.infra.llm_providers import AnthropicProvider
from api.infra.llm_providers import GeminiProvider
from api.infra.llm_router import ExtractionUnavailableError
from api.infra.llm_router import LLMRouter
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID
from api.services.embedding_service import EmbeddingResult
from api.services.extractor import ExtractedMemory
from api.services.vector_outbox import build_vector_payload
from api.services.vector_outbox import enqueue_vector_delete
from api.services.vector_outbox import enqueue_vector_upsert


PROMPT_PATH = Path(__file__).with_name("prompts") / "conflict_prompt.txt"
SIMILARITY_THRESHOLD = 0.85


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
    ) -> None:
        self.session = session
        self.qdrant_service = qdrant_service
        self.embedder = embedder
        self.client = client
        self.model = model or "gemini-2.5-flash-lite"
        self.prompt_path = prompt_path or PROMPT_PATH
        self.system_prompt = self.prompt_path.read_text(encoding="utf-8")
        self.default_source_conversation_id = default_source_conversation_id
        self.llm_router = llm_router or LLMRouter(
            provider_factories={
                "gemini": lambda **overrides: GeminiProvider(
                    client=overrides.pop("client", None) or self.client,
                    extract_model=overrides.pop("extract_model", self.model),
                    **overrides,
                ),
                "anthropic": lambda **overrides: AnthropicProvider(
                    extract_model=overrides.pop("extract_model", "claude-haiku-4-5-20251001"),
                    **overrides,
                ),
            }
        )

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

        for new_memory in new_memories:
            embedding = self._coerce_embedding_result(self.embedder(new_memory.content))
            search_kwargs: dict[str, Any] = {
                "query_embedding": embedding.vector,
                "limit": 5,
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

            candidates = [
                point
                for point in raw_candidates
                if getattr(point, "score", 0.0) > SIMILARITY_THRESHOLD
            ]

            decision_applied = False
            for candidate in candidates:
                existing_memory = self._load_existing_memory(candidate)
                if existing_memory is None:
                    continue

                decision = self._temporal_conflict_decision(new_memory, existing_memory)
                if decision is None:
                    decision = self._classify_conflict(new_memory, existing_memory)

                if decision.action == "UPDATE":
                    self._archive_memory(existing_memory)
                    stored_memories.append(
                        self._store_new_memory(
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
                            "new_memory": self._serialize_extracted_memory(new_memory),
                        },
                        memory_id=existing_memory.id,
                    )
                    decision_applied = True
                    break

                if decision.action == "MERGE":
                    merged_memory = self._build_merged_memory(
                        existing_memory=existing_memory,
                        new_memory=new_memory,
                        merged_memory=decision.merged_memory,
                    )
                    self._archive_memory(existing_memory)
                    stored_memories.append(
                        self._store_new_memory(
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
                            "merged_memory": self._serialize_extracted_memory(merged_memory),
                        },
                        memory_id=existing_memory.id,
                    )
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
                        },
                        memory_id=existing_memory.id,
                    )
                    decision_applied = True
                    break

            if not decision_applied:
                stored_memories.append(
                        self._store_new_memory(
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
    ) -> ConflictDecision:
        content = json.dumps(
            {
                "existing": self._serialize_memory(existing_memory),
                "new": self._serialize_extracted_memory(new_memory),
            }
        )

        try:
            provider = self.llm_router.get_extract_provider()
            raw_content = provider.extract(
                [{"role": "user", "content": content}],
                self.system_prompt,
            )
        except ExtractionUnavailableError:
            raw_content = "{}"

        payload = json.loads(raw_content or "{}")
        action = str(payload.get("action", "KEEP_BOTH")).upper()
        reasoning = str(payload.get("reasoning", "")).strip() or "No reasoning provided."
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

    def _archive_memory(self, memory: Memory) -> None:
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
