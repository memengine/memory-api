from __future__ import annotations

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import CrossUserConflict
from api.db.models import Memory
from api.services.embedding_service import EmbeddingService
from api.services.claim_ledger_service import ClaimLedgerService
from api.services.vector_outbox import build_vector_payload
from api.services.vector_outbox import enqueue_vector_delete
from api.services.vector_outbox import enqueue_vector_upsert
from api.services.version_service import VersionService


ConflictSelection = Literal["A", "B", "both", "neither"]


async def apply_conflict_selection(
    session: AsyncSession,
    *,
    conflict: CrossUserConflict,
    selection: ConflictSelection,
    changed_by: Literal["user", "operator"],
    reason: str,
) -> str:
    memory_a = conflict.user_a_memory
    memory_b = conflict.user_b_memory
    if memory_a is None or memory_b is None:
        raise ValueError("conflict_memory_missing")

    desired_states = {
        "A": {memory_a.id: False, memory_b.id: True},
        "B": {memory_a.id: True, memory_b.id: False},
        "both": {memory_a.id: False, memory_b.id: False},
        "neither": {memory_a.id: True, memory_b.id: True},
    }[selection]

    embedder: EmbeddingService | None = None
    transitions: list[str] = []
    for memory in (memory_a, memory_b):
        should_archive = desired_states[memory.id]
        was_archived = bool(memory.is_archived)
        metadata = dict(memory.metadata_json or {})
        metadata["conflict_resolution"] = {
            "conflict_id": str(conflict.id),
            "selection": selection,
            "reason": reason,
        }
        memory.metadata_json = metadata

        if should_archive:
            if not was_archived:
                await VersionService(session).asafe_record_version(
                    memory,
                    "conflict_resolved",
                    reason,
                    changed_by,
                )
                memory.is_archived = True
                enqueue_vector_delete(
                    session,
                    memory_id=memory.id,
                    payload={
                        "memory_id": str(memory.id),
                        "embedding_model_id": memory.embedding_model_id,
                    },
                )
                transitions.append(f"archived_{_memory_label(memory, memory_a)}")
            continue

        if was_archived:
            await VersionService(session).asafe_record_version(
                memory,
                "conflict_resolved",
                reason,
                changed_by,
            )
            memory.is_archived = False
            embedder = embedder or EmbeddingService(async_session=session)
            embedding = await embedder.embed(
                memory.content,
                model_id=memory.embedding_model_id,
                tenant_id=str(conflict.tenant_id),
            )
            memory.embedding_model_id = embedding.model_id
            enqueue_vector_upsert(
                session,
                memory_id=memory.id,
                embedding=embedding.vector,
                payload=build_vector_payload(
                    memory,
                    tenant_id=str(conflict.tenant_id),
                    proxy_user_id=str(memory.proxy_user_id),
                    user_id=str(memory.user_id),
                    embedding_model_id=embedding.model_id,
                    qdrant_collection=embedding.qdrant_collection,
                ),
            )
            transitions.append(f"activated_{_memory_label(memory, memory_a)}")

    await ClaimLedgerService(session).apply_conflict_selection(
        memory_a=memory_a,
        memory_b=memory_b,
        selection=selection,
        reason=reason,
    )

    if not transitions:
        return "memory_states_unchanged"
    return "_and_".join(transitions)


def _memory_label(memory: Memory, memory_a: Memory) -> str:
    return "memory_a" if memory.id == memory_a.id else "memory_b"
