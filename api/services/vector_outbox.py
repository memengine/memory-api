from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from typing import Any

from api.db.models import Memory
from api.db.models import VectorSyncOperation
from api.db.models import VectorSyncOutbox


def build_vector_payload(
    memory: Memory,
    *,
    tenant_id: str | None,
    proxy_user_id: str | None,
    user_id: str | None = None,
    embedding_model_id: str | None = None,
    qdrant_collection: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "memory_id": str(memory.id),
        "agent_id": str(memory.agent_id) if memory.agent_id else None,
        "content": memory.content,
        "category": memory.category.value if hasattr(memory.category, "value") else str(memory.category),
        "importance_score": float(memory.importance_score),
        "confidence_score": float(getattr(memory, "confidence_score", 1.0) or 1.0),
        "is_archived": bool(memory.is_archived),
        "created_at": (
            memory.created_at.isoformat()
            if memory.created_at
            else datetime.now(UTC).isoformat()
        ),
        "last_accessed_at": (
            getattr(memory, "last_accessed_at", None).isoformat()
            if getattr(memory, "last_accessed_at", None)
            else None
        ),
        "previous_version_id": (
            str(getattr(memory, "previous_version_id"))
            if getattr(memory, "previous_version_id", None)
            else None
        ),
        "source_event_id": (
            str(getattr(memory, "source_event_id"))
            if getattr(memory, "source_event_id", None)
            else None
        ),
        "provenance": (getattr(memory, "metadata_json", None) or {}).get("provenance"),
        "embedding_model_id": embedding_model_id or getattr(memory, "embedding_model_id", None),
        "qdrant_collection": (
            qdrant_collection
            or getattr(getattr(memory, "embedding_model", None), "qdrant_collection", None)
        ),
    }
    if tenant_id and proxy_user_id:
        payload["tenant_id"] = str(tenant_id)
        payload["proxy_user_id"] = str(proxy_user_id)
    elif user_id is not None:
        payload["user_id"] = str(user_id)
    return payload


def enqueue_vector_upsert(
    session: Any,
    *,
    memory_id: uuid.UUID | str,
    embedding: list[float],
    payload: dict[str, Any],
) -> VectorSyncOutbox:
    row = VectorSyncOutbox(
        id=uuid.uuid4(),
        operation=VectorSyncOperation.upsert,
        memory_id=_as_uuid(memory_id),
        embedding=[float(value) for value in embedding],
        payload=payload,
    )
    session.add(row)
    return row


def enqueue_vector_delete(
    session: Any,
    *,
    memory_id: uuid.UUID | str,
    payload: dict[str, Any] | None = None,
) -> VectorSyncOutbox:
    row = VectorSyncOutbox(
        id=uuid.uuid4(),
        operation=VectorSyncOperation.delete,
        memory_id=_as_uuid(memory_id),
        embedding=None,
        payload=payload or {},
    )
    session.add(row)
    return row


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
