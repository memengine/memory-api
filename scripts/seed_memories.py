from __future__ import annotations

import argparse
import hashlib
import math
import os
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from qdrant_client.http import models as qmodels

from api.db.database import get_sync_database_url
from api.db.models import Conversation
from api.db.models import ConversationProcessingStatus
from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import User
from api.db.vector_store import QdrantService


BENCHMARK_USER_EXTERNAL_ID = "benchmark-user-10k"
BENCHMARK_USER_EMAIL = "benchmark-user-10k@memoryos.local"
COLD_START_USER_EXTERNAL_ID = "benchmark-user-cold-start"
COLD_START_USER_EMAIL = "benchmark-user-cold-start@memoryos.local"
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
BATCH_SIZE = 250

TOPICS = [
    "pricing",
    "launch",
    "retention",
    "onboarding",
    "postgres",
    "fastapi",
    "python",
    "go",
    "redis",
    "qdrant",
]


def load_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def deterministic_embedding(text: str, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    vector = [0.0] * dimensions
    for token in text.lower().split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        magnitude = ((digest[4] / 255) * 2.0) - 1.0
        vector[index] += magnitude

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def upsert_benchmark_user(session: Session, external_id: str, email: str) -> User:
    statement = (
        insert(User)
        .values(
            id=uuid.uuid4(),
            external_id=external_id,
            email=email,
            settings={},
            is_active=True,
        )
        .on_conflict_do_update(
            index_elements=[User.external_id],
            set_={
                "email": email,
                "is_active": True,
            },
        )
        .returning(User.id)
    )
    user_id = session.execute(statement).scalar_one()
    user = session.get(User, user_id)
    if user is None:
        raise RuntimeError(f"Unable to load benchmark user for external_id={external_id}")
    return user


def replace_conversation(session: Session, user: User) -> Conversation:
    existing_conversations = session.execute(
        select(Conversation).where(Conversation.user_id == user.id)
    ).scalars().all()
    for conversation in existing_conversations:
        session.delete(conversation)
    session.flush()

    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=user.id,
        agent_id=None,
        message_count=0,
        processing_status=ConversationProcessingStatus.done,
    )
    session.add(conversation)
    session.flush()
    return conversation


def clear_user_memories(session: Session, qdrant_service: QdrantService, user: User) -> None:
    qdrant_service.delete_user_memories(str(user.id))
    existing_memories = session.execute(
        select(Memory).where(Memory.user_id == user.id)
    ).scalars().all()
    for memory in existing_memories:
        session.delete(memory)
    session.flush()


def memory_content(index: int) -> tuple[str, MemoryCategory]:
    if index == 0:
        return ("User prefers Python for backend work", MemoryCategory.preference)
    if index == 1:
        return ("User prefers Go for systems programming", MemoryCategory.preference)
    if index == 2:
        return ("User uses FastAPI with PostgreSQL", MemoryCategory.expertise)

    topic = TOPICS[index % len(TOPICS)]
    if topic in {"python", "go", "fastapi", "postgres", "redis", "qdrant"}:
        category = MemoryCategory.expertise
        content = f"User works with {topic} in production systems {index}"
    elif topic in {"pricing", "launch", "retention", "onboarding"}:
        category = MemoryCategory.goal
        content = f"User is focused on {topic} experiments for product growth {index}"
    else:
        category = MemoryCategory.fact
        content = f"User tracks {topic} related business context {index}"
    return content, category


def seed_user_memories(
    *,
    session: Session,
    qdrant_service: QdrantService,
    user: User,
    conversation: Conversation,
    count: int,
) -> None:
    now = datetime.now(UTC)
    pending_memories: list[Memory] = []
    pending_points: list[qmodels.PointStruct] = []

    def flush_batch() -> None:
        if pending_memories:
            session.add_all(pending_memories)
            pending_memories.clear()
        if pending_points:
            qdrant_service._with_retries(
                qdrant_service.client.upsert,
                collection_name=qdrant_service.COLLECTION_NAME,
                points=list(pending_points),
                wait=True,
            )
            pending_points.clear()

    for index in range(count):
        content, category = memory_content(index)
        memory_id = uuid.uuid4()
        created_at = now - timedelta(days=index % 90)
        last_accessed_at = now - timedelta(days=index % 45)
        importance_score = 5.0 + (index % 5)

        memory = Memory(
            id=memory_id,
            user_id=user.id,
            agent_id=None,
            content=content,
            category=category,
            importance_score=importance_score,
            confidence_score=0.9,
            embedding_id=str(memory_id),
            source_conversation_id=conversation.id,
            previous_version_id=None,
            expires_at=None,
            metadata_json={},
            created_at=created_at,
            updated_at=created_at,
            last_accessed_at=last_accessed_at,
            access_count=index % 25,
            is_archived=False,
        )
        pending_memories.append(memory)
        pending_points.append(
            qmodels.PointStruct(
                id=str(memory_id),
                vector=deterministic_embedding(content),
                payload={
                    "memory_id": str(memory_id),
                    "user_id": str(user.id),
                    "agent_id": None,
                    "content": content,
                    "category": category.value,
                    "importance_score": importance_score,
                    "is_archived": False,
                    "created_at": created_at.isoformat(),
                },
            )
        )

        if len(pending_memories) >= BATCH_SIZE:
            flush_batch()

    flush_batch()


def seed_cold_start_user(
    *,
    session: Session,
    qdrant_service: QdrantService,
    user: User,
    conversation: Conversation,
) -> None:
    cold_start_memories = [
        ("User works in healthcare", MemoryCategory.fact, 6.0),
        ("User is an engineer", MemoryCategory.fact, 7.0),
    ]
    now = datetime.now(UTC)
    pending_memories: list[Memory] = []
    pending_points: list[qmodels.PointStruct] = []
    for content, category, importance_score in cold_start_memories:
        memory_id = uuid.uuid4()
        memory = Memory(
            id=memory_id,
            user_id=user.id,
            agent_id=None,
            content=content,
            category=category,
            importance_score=importance_score,
            confidence_score=0.9,
            embedding_id=str(memory_id),
            source_conversation_id=conversation.id,
            previous_version_id=None,
            expires_at=None,
            metadata_json={},
            created_at=now,
            updated_at=now,
            last_accessed_at=now,
            access_count=0,
            is_archived=False,
        )
        pending_memories.append(memory)
        pending_points.append(
            qmodels.PointStruct(
                id=str(memory_id),
                vector=deterministic_embedding(content),
                payload={
                    "memory_id": str(memory_id),
                    "user_id": str(user.id),
                    "agent_id": None,
                    "content": content,
                    "category": category.value,
                    "importance_score": importance_score,
                    "is_archived": False,
                    "created_at": now.isoformat(),
                },
            )
        )

    if pending_memories:
        session.add_all(pending_memories)
    if pending_points:
        qdrant_service._with_retries(
            qdrant_service.client.upsert,
            collection_name=qdrant_service.COLLECTION_NAME,
            points=pending_points,
            wait=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed benchmark memories into PostgreSQL and Qdrant.")
    parser.add_argument("--count", type=int, default=10_000, help="Number of memories to seed.")
    args = parser.parse_args()

    load_env()
    engine = create_engine(get_sync_database_url(), pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    qdrant_service = QdrantService()

    with session_factory() as session:
        benchmark_user = upsert_benchmark_user(
            session,
            external_id=BENCHMARK_USER_EXTERNAL_ID,
            email=BENCHMARK_USER_EMAIL,
        )
        clear_user_memories(session, qdrant_service, benchmark_user)
        benchmark_conversation = replace_conversation(session, benchmark_user)
        seed_user_memories(
            session=session,
            qdrant_service=qdrant_service,
            user=benchmark_user,
            conversation=benchmark_conversation,
            count=args.count,
        )

        cold_start_user = upsert_benchmark_user(
            session,
            external_id=COLD_START_USER_EXTERNAL_ID,
            email=COLD_START_USER_EMAIL,
        )
        clear_user_memories(session, qdrant_service, cold_start_user)
        cold_start_conversation = replace_conversation(session, cold_start_user)
        seed_cold_start_user(
            session=session,
            qdrant_service=qdrant_service,
            user=cold_start_user,
            conversation=cold_start_conversation,
        )

        session.commit()

        print(f"Seeded {args.count} benchmark memories for user {benchmark_user.external_id}")
        print(f"Benchmark user id: {benchmark_user.id}")
        print(f"Cold-start user id: {cold_start_user.id}")


if __name__ == "__main__":
    main()
