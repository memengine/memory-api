from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from celery import shared_task
from celery.schedules import crontab
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from api.db.database import get_sync_database_url
from api.db.models import AuditAction
from api.db.models import AuditLog
from api.db.models import Memory
from api.services.vector_outbox import enqueue_vector_delete


DECAY_TASK_NAME = "api.tasks.decay_tasks.archive_stale_low_importance_memories"
DECAY_TASK_BEAT_SCHEDULE = {
    "archive-stale-low-importance-memories": {
        "task": DECAY_TASK_NAME,
        "schedule": crontab(hour=2, minute=0),
    }
}


def build_decay_session_factory() -> sessionmaker[Session]:
    engine = create_engine(get_sync_database_url(), pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


def run_decay_cycle(
    *,
    session_factory: Callable[[], Any] | None = None,
    now: datetime | None = None,
) -> int:
    reference_time = now or datetime.now(UTC)
    factory = session_factory or build_decay_session_factory()
    session = factory()

    try:
        stale_memories = (
            session.execute(
                select(Memory).where(
                    Memory.is_archived.is_(False),
                    Memory.importance_score < 1.5,
                    Memory.last_accessed_at < reference_time - timedelta(days=90),
                    Memory.access_count == 0,
                )
            )
            .scalars()
            .all()
        )
        stale_memories = [
            memory
            for memory in stale_memories
            if not memory.is_archived
            and float(memory.importance_score) < 1.5
            and int(memory.access_count or 0) == 0
            and memory.last_accessed_at < reference_time - timedelta(days=90)
        ]

        archived_count = 0
        for memory in stale_memories:
            memory.is_archived = True
            session.add(memory)
            enqueue_vector_delete(
                session,
                memory_id=memory.id,
                payload={"memory_id": str(memory.id)},
            )
            session.add(
                AuditLog(
                    user_id=memory.user_id,
                    action=AuditAction.archived,
                    memory_id=memory.id,
                    old_value={
                        "importance_score": memory.importance_score,
                        "last_accessed_at": (
                            memory.last_accessed_at.isoformat()
                            if memory.last_accessed_at
                            else None
                        ),
                        "is_archived": False,
                    },
                    new_value={
                        "is_archived": True,
                        "reason": "legacy_auto_archive_low_importance_inactive_memory",
                    },
                    ip_address=None,
                )
            )
            archived_count += 1

        session.commit()
        return archived_count
    finally:
        if hasattr(session, "close"):
            session.close()


@shared_task(name=DECAY_TASK_NAME)
def archive_stale_low_importance_memories() -> int:
    return run_decay_cycle()
