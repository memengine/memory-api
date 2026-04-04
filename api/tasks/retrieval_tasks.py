from __future__ import annotations

import uuid

from celery import shared_task
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from api.db.database import get_sync_database_url
from api.db.models import AuditAction
from api.db.models import AuditLog
from api.db.models import Memory
from api.services.importance_scorer import ImportanceScorer


RETRIEVAL_TASK_NAME = "api.tasks.retrieval_tasks.update_memory_accesses"


def build_retrieval_session_factory() -> sessionmaker[Session]:
    engine = create_engine(get_sync_database_url(), pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


def run_access_update(memory_ids: list[str]) -> int:
    if not memory_ids:
        return 0

    session_factory = build_retrieval_session_factory()
    session = session_factory()
    scorer = ImportanceScorer()

    try:
        parsed_ids = [uuid.UUID(memory_id) for memory_id in memory_ids]
        result = session.execute(
            select(Memory).where(
                Memory.id.in_(parsed_ids),
                Memory.is_archived.is_(False),
            )
        )
        memories = list(result.scalars().all())

        for memory in memories:
            previous_count = int(memory.access_count or 0)
            scorer.increment_access(memory)
            session.add(memory)
            session.add(
                AuditLog(
                    user_id=memory.user_id,
                    action=AuditAction.retrieved,
                    memory_id=memory.id,
                    old_value={"access_count": previous_count},
                    new_value={"access_count": memory.access_count},
                    ip_address=None,
                )
            )

        session.commit()
        return len(memories)
    finally:
        session.close()


@shared_task(name=RETRIEVAL_TASK_NAME)
def update_memory_accesses(memory_ids: list[str]) -> int:
    return run_access_update(memory_ids)
