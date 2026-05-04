from __future__ import annotations

import traceback
import uuid
from datetime import UTC
from datetime import datetime
from typing import Any

from celery import shared_task
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from api.db.database import build_sync_session_factory
from api.db.models import MemoryCategory
from api.db.models import PermissionGrant
from api.db.models import UniversalMemory
from api.db.models import UniversalUser
from api.db.vector_store import QdrantService
from api.services.embedding_service import EmbeddingService
from api.services.extractor import ExtractionService
from api.services.importance_scorer import ImportanceScorer
from api.services.uui_service import ALLOWED_MEMORY_CATEGORIES


UNIVERSAL_EXTRACTION_TASK_NAME = "api.tasks.universal_extraction_tasks.extract_universal_memory"
UNIVERSAL_COLLECTION_NAME = "universal_memories"


def build_universal_session_factory() -> sessionmaker[Session]:
    return build_sync_session_factory()


def _capture_error_detail() -> str:
    error_detail = traceback.format_exc()
    if len(error_detail) > 2000:
        error_detail = error_detail[-2000:]
    return error_detail


def _active_write_grant(session: Session, *, user_uui_id: str, agent_id: str) -> PermissionGrant | None:
    result = session.execute(
        select(PermissionGrant).where(
            PermissionGrant.user_uui_id == uuid.UUID(user_uui_id),
            PermissionGrant.agent_id == uuid.UUID(agent_id),
            PermissionGrant.is_active.is_(True),
            PermissionGrant.access_type == "read_write",
            (PermissionGrant.expires_at.is_(None) | (PermissionGrant.expires_at > func.now())),
        )
    )
    return result.scalar_one_or_none()


def run_universal_extraction_pipeline(
    job_payload: dict[str, Any],
    *,
    session_factory: sessionmaker[Session] | None = None,
    extractor: ExtractionService | None = None,
    scorer: ImportanceScorer | None = None,
    qdrant_service: QdrantService | None = None,
) -> dict[str, Any]:
    user_uui_id = str(job_payload.get("user_uui_id") or "").strip()
    agent_id = str(job_payload.get("agent_id") or "").strip()
    messages = list(job_payload.get("messages", []))

    if not user_uui_id or not agent_id:
        raise ValueError("Universal extraction job requires user_uui_id and agent_id.")

    session_factory = session_factory or build_universal_session_factory()
    extractor = extractor or ExtractionService()
    scorer = scorer or ImportanceScorer()
    qdrant_service = qdrant_service or QdrantService()

    session = session_factory()
    try:
        universal_user = session.get(UniversalUser, uuid.UUID(user_uui_id))
        if universal_user is None or not bool(universal_user.is_active):
            raise ValueError(f"Universal user {user_uui_id} not found.")

        grant = _active_write_grant(session, user_uui_id=user_uui_id, agent_id=agent_id)
        if grant is None:
            return {
                **job_payload,
                "status": "blocked",
                "blocked_reason": "write_not_permitted",
                "memories_created": 0,
            }

        extracted_memories = extractor.extract(messages=messages, user_id=user_uui_id)
        allowed_categories = {
            category for category in (grant.categories_allowed or []) if category in ALLOWED_MEMORY_CATEGORIES
        }
        embedder = EmbeddingService(sync_session=session)
        stored_count = 0

        for extracted_memory in extracted_memories:
            if extracted_memory.category not in allowed_categories:
                continue

            extracted_memory.importance_score = scorer.score(
                extracted_memory,
                {"similar_access_count": int(universal_user.memory_count or 0)},
            )
            embedding = embedder.embed_sync(extracted_memory.content)
            memory_id = uuid.uuid4()
            created_at = datetime.now(UTC)

            universal_memory = UniversalMemory(
                id=memory_id,
                user_uui_id=universal_user.id,
                source_agent_id=uuid.UUID(agent_id),
                content=extracted_memory.content,
                category=MemoryCategory(extracted_memory.category),
                importance_score=float(extracted_memory.importance_score),
                confidence=float(extracted_memory.confidence),
                embedding_id=str(memory_id),
                created_at=created_at,
                last_accessed_at=created_at,
                is_archived=False,
                metadata_json={
                    "reasoning": extracted_memory.reasoning,
                    "expiry": extracted_memory.expiry,
                    "source_metadata": dict(job_payload.get("metadata") or {}),
                },
            )
            session.add(universal_memory)
            session.flush()

            qdrant_service.upsert_memory(
                memory_id=str(memory_id),
                embedding=embedding.vector,
                payload={
                    "memory_id": str(memory_id),
                    "user_uui_id": str(universal_user.id),
                    "source_agent_id": str(agent_id),
                    "category": extracted_memory.category,
                    "importance_score": float(extracted_memory.importance_score),
                    "is_archived": False,
                    "created_at": created_at.isoformat(),
                },
                collection_name=UNIVERSAL_COLLECTION_NAME,
                vector_size=embedding.dimensions,
            )
            stored_count += 1

        universal_user.memory_count = int(universal_user.memory_count or 0) + stored_count
        session.add(universal_user)
        session.commit()
        return {
            **job_payload,
            "status": "processed",
            "memories_created": stored_count,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@shared_task(bind=True, name=UNIVERSAL_EXTRACTION_TASK_NAME, max_retries=3, default_retry_delay=2)
def extract_universal_memory(self, job_payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return run_universal_extraction_pipeline(job_payload)
    except Exception as exc:
        error_detail = _capture_error_detail()
        if int(getattr(self.request, "retries", 0) or 0) >= int(getattr(self, "max_retries", 3) or 3) - 1:
            return {
                **job_payload,
                "status": "dead",
                "error": error_detail,
                "memories_created": 0,
            }
        raise self.retry(exc=exc, countdown=60 * (int(getattr(self.request, "retries", 0) or 0) + 1))
