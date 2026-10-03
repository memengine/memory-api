"""Bounded, restart-safe expiration and retention for non-activating reviews."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from celery import shared_task
from sqlalchemy import delete, select, update

from api.db.database import build_sync_session_factory
from api.db.models import PendingExtractionCandidate as Candidate
from api.services.source_review_service import REVIEW_TTL, review_predicate
from api.settings import get_settings

SOURCE_REVIEW_MAINTENANCE_TASK = "api.tasks.source_review_tasks.maintain_source_reviews"
SOURCE_REVIEW_BEAT_SCHEDULE = {
    "maintain-source-reviews": {"task": SOURCE_REVIEW_MAINTENANCE_TASK, "schedule": 300.0},
}


def run_source_review_maintenance(*, session_factory=None, now=None, batch_size=1000, tenant_id=None):
    now = now or datetime.now(UTC)
    size = max(1, min(int(batch_size), 5000))
    factory = session_factory or build_sync_session_factory()
    owner = [Candidate.tenant_id == uuid.UUID(str(tenant_id))] if tenant_id is not None else []
    with factory() as session:
        expired = session.scalars(select(Candidate.id).where(
            review_predicate(), *owner, Candidate.status == "pending",
            Candidate.created_at <= now - REVIEW_TTL,
        ).order_by(Candidate.created_at, Candidate.id).limit(size).with_for_update(skip_locked=True)).all()
        if expired:
            session.execute(update(Candidate).where(Candidate.id.in_(expired)).values(status="expired", updated_at=now))
        session.commit()
        cutoff = now - REVIEW_TTL - timedelta(days=get_settings().source_review_closed_retention_days)
        purged = session.scalars(select(Candidate.id).where(
            review_predicate(), *owner, Candidate.status.in_(["dismissed", "expired"]),
            Candidate.created_at <= cutoff,
        ).order_by(Candidate.created_at, Candidate.id).limit(size).with_for_update(skip_locked=True)).all()
        if purged:
            session.execute(delete(Candidate).where(Candidate.id.in_(purged)))
        session.commit()
    return {"expired": len(expired), "purged": len(purged)}


@shared_task(name=SOURCE_REVIEW_MAINTENANCE_TASK)
def maintain_source_reviews(batch_size=1000):
    return run_source_review_maintenance(batch_size=batch_size)
