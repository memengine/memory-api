"""Non-activating reviews for verified source claims without a usable option.

Reuse the pending-candidate store. A review is not a Memory, and answering it
cannot promote its source text or a model-authored value into trusted context.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, literal_column, select, text, update

from api.db.models import Memory, PendingExtractionCandidate
from api.schemas.responses import MemorySourceReview

REVIEW_TTL = timedelta(days=7)
MAX_REVIEWS = 3


class ReviewCapacityExceeded(ValueError):
    """No pending text was retained; the job must report this outcome."""


def is_source_review(candidate) -> bool:
    return (
        candidate.candidate_reason == "source_decision_pending"
        and isinstance((candidate.validated_evidence or {}).get("source_review"), dict)
        and candidate.validated_evidence["source_review"].get("kind") == "restate_source"
    )


def review_predicate():
    # Fixed policy constants, not caller input. Generic prepared plans must
    # prove the partial-index predicate and JSON path without bound parameters.
    return text("pending_extraction_candidates.candidate_reason = 'source_decision_pending' AND (pending_extraction_candidates.metadata['extraction_evidence']['source_review'] ->> 'kind') = 'restate_source'")


def reserve_review_capacity(session, *, tenant_id, proxy_user_id, fingerprint, legacy_fingerprint=None, generation=None, generation_created_at=None):
    from api.settings import get_settings
    # Same transaction as candidate insertion. A tenant-scoped advisory lock
    # serializes counts/inserts across profiles without another quota store.
    lock_key = int.from_bytes(hashlib.sha256(f"source-review:{tenant_id}".encode()).digest()[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    fingerprint_filter = PendingExtractionCandidate.candidate_fingerprint == fingerprint
    if legacy_fingerprint is not None and generation is not None:
        fingerprint_filter |= (
            (PendingExtractionCandidate.candidate_fingerprint == legacy_fingerprint)
            & (PendingExtractionCandidate.extraction_job_id == generation)
        )
    existing = session.execute(select(PendingExtractionCandidate).where(
        PendingExtractionCandidate.tenant_id == tenant_id,
        PendingExtractionCandidate.proxy_user_id == proxy_user_id,
        fingerprint_filter,
    ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if existing is not None:
        return existing
    # Identical new evidence can reinforce an OPEN generation, not duplicate
    # cards. A CLOSED generation's timestamp is a fence for queued/retried jobs.
    if legacy_fingerprint is not None:
        matching = session.execute(select(PendingExtractionCandidate).where(
            PendingExtractionCandidate.tenant_id == tenant_id,
            PendingExtractionCandidate.proxy_user_id == proxy_user_id,
            review_predicate(),
            (
                (PendingExtractionCandidate.candidate_fingerprint == legacy_fingerprint)
                | (PendingExtractionCandidate.metadata_json["review_base_fingerprint"].astext == legacy_fingerprint)
            ),
        ).order_by(PendingExtractionCandidate.created_at.desc(), PendingExtractionCandidate.id)
            .limit(1).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if matching is not None:
            deadline = _utc(matching.created_at) + REVIEW_TTL
            if matching.status == "pending" and deadline > datetime.now(UTC):
                return matching
            fence = max(deadline if matching.status == "pending" else _utc(matching.updated_at), _utc(matching.created_at))
            if generation_created_at is None or _utc(generation_created_at) <= fence:
                return matching
    settings = get_settings()
    owned = select(PendingExtractionCandidate.id).where(
        PendingExtractionCandidate.tenant_id == tenant_id, review_predicate(),
    )
    tenant_count = select(func.count()).select_from(owned.limit(settings.source_review_tenant_row_limit).subquery()).scalar_subquery()
    profile_count = select(func.count()).select_from(owned.where(
        PendingExtractionCandidate.proxy_user_id == proxy_user_id,
    ).limit(settings.source_review_profile_row_limit).subquery()).scalar_subquery()
    counts = session.execute(select(tenant_count, profile_count)).one()
    if counts[0] >= settings.source_review_tenant_row_limit or counts[1] >= settings.source_review_profile_row_limit:
        raise ReviewCapacityExceeded("source_review_capacity_exceeded")
    return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def target_version(memory: Memory) -> str:
    # Exclude mutable retrieval counters, but bind content, ownership, authority,
    # scope and temporal validity. Never expose this underlying snapshot.
    snapshot = {
        name: getattr(memory, name, None)
        for name in (
            "id", "user_id", "proxy_user_id", "content", "category", "is_archived",
            "expires_at", "effective_from", "effective_until", "metadata_json",
        )
    }
    for key in ("expires_at", "effective_from", "effective_until"):
        if isinstance(snapshot[key], datetime):
            snapshot[key] = _utc(snapshot[key]).isoformat()
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()


def review_intent(target: Memory | None) -> dict[str, Any]:
    return {
        "kind": "restate_source",
        "target_memory_id": str(target.id) if target is not None else None,
        "target_version": target_version(target) if target is not None else None,
    }


def intent_for(candidate: PendingExtractionCandidate) -> dict[str, Any] | None:
    if candidate.candidate_reason != "source_decision_pending":
        return None
    evidence = (candidate.metadata_json or {}).get("extraction_evidence") or {}
    intent = evidence.get("source_review")
    if (
        evidence.get("grounding_mode") != "verified_source_spans"
        or not isinstance(intent, dict)
        or intent.get("kind") != "restate_source"
        or set(intent) != {"kind", "target_memory_id", "target_version"}
    ):
        return None
    target_id, version = intent["target_memory_id"], intent["target_version"]
    if target_id is None:
        return intent if version is None else None
    if not isinstance(target_id, str) or not isinstance(version, str) or len(version) != 64:
        return None
    try:
        uuid.UUID(target_id)
        int(version, 16)
    except ValueError:
        return None
    return intent


def review_version(candidate: PendingExtractionCandidate) -> str:
    evidence = (candidate.metadata_json or {}).get("extraction_evidence") or {}
    return hashlib.sha256(json.dumps({
        "id": str(candidate.id), "content": candidate.content,
        "source_spans": evidence.get("source_spans"), "intent": intent_for(candidate),
    }, sort_keys=True, default=str).encode()).hexdigest()


def view_review(
    candidate: PendingExtractionCandidate, target: Memory | None, *, now: datetime,
) -> MemorySourceReview | None:
    intent = intent_for(candidate)
    if intent is None or candidate.status != "pending":
        return None
    expires_at = _utc(candidate.created_at) + REVIEW_TTL
    if expires_at <= now:
        return None
    target_id = intent.get("target_memory_id")
    if target_id is not None and (
        target is None or str(target.id) != target_id
        or str(target.proxy_user_id) != str(candidate.proxy_user_id)
        or target.is_archived or target_version(target) != intent.get("target_version")
        or any(
            getattr(target, name, None) is not None and _utc(getattr(target, name)) <= now
            for name in ("expires_at", "effective_until")
        )
        or (target.effective_from is not None and _utc(target.effective_from) > now)
    ):
        return None
    return MemorySourceReview(
        id=str(candidate.id), version=review_version(candidate),
        kind="restate_source", target_memory_id=target_id,
        current_memory_content=(
            target.content if len(target.content) <= 1000 else target.content[:999] + "…"
        ) if target_id is not None else None,
        question=(
            "Should the stored memory remain current, or what should be remembered instead?"
            if target_id is not None else "What should be remembered as current? Please state the preference or fact and its scope."
        ),
        actions=["keep_current", "restate", "dismiss"] if target_id is not None else ["restate", "dismiss"],
        expires_at=expires_at,
    )


def source_review_query(*, tenant_id: str, proxy_user_id: str, preference_context=None, now=None):
    from api.services.preference_claim_service import (
        PREFERENCE_MARKER,
        preference_fingerprint,
    )
    evidence = PendingExtractionCandidate.metadata_json["extraction_evidence"]
    fingerprint_column = literal_column("(pending_extraction_candidates.metadata['extraction_evidence'] ->> 'preference_fingerprint')")
    if preference_context is None:
        applicability_filter = (
            evidence[PREFERENCE_MARKER].astext.is_(None)
            & fingerprint_column.is_(None)
        )
    else:
        fingerprints = {
            preference_fingerprint(key, preference_context.applicability)
            for key in preference_context.property_keys
        }
        applicability_filter = fingerprint_column.in_(fingerprints)
    return select(PendingExtractionCandidate).where(
        PendingExtractionCandidate.tenant_id == uuid.UUID(tenant_id),
        PendingExtractionCandidate.proxy_user_id == uuid.UUID(proxy_user_id),
        PendingExtractionCandidate.status == "pending",
        applicability_filter,
        review_predicate(),
        PendingExtractionCandidate.created_at > (now or datetime.now(UTC)) - REVIEW_TTL,
    ).order_by(PendingExtractionCandidate.created_at.desc(), PendingExtractionCandidate.id).limit(MAX_REVIEWS)


async def list_source_reviews(
    session: Any, *, tenant_id: str, proxy_user_id: str,
    preference_context=None,
) -> list[MemorySourceReview]:
    rows = (await session.execute(source_review_query(
        tenant_id=tenant_id, proxy_user_id=proxy_user_id, preference_context=preference_context,
    ))).scalars().all()
    target_ids = [uuid.UUID(intent["target_memory_id"]) for row in rows
                  if (intent := intent_for(row)) and intent.get("target_memory_id")]
    targets = {}
    if target_ids:
        targets = {str(row.id): row for row in (await session.execute(select(Memory).where(
            Memory.id.in_(target_ids), Memory.proxy_user_id == uuid.UUID(proxy_user_id),
        ).execution_options(populate_existing=True))).scalars().all()}
    now = datetime.now(UTC)
    views = []
    expired = False
    for row in rows:
        view = view_review(row, targets.get((intent_for(row) or {}).get("target_memory_id")), now=now)
        if view is not None:
            views.append(view)
        else:
            # Conditional update takes a DB row lock and cannot expire a review
            # refreshed by a concurrent worker after our read. Clearing stale
            # rows lets the next bounded read advance instead of being blocked
            # permanently by the same recent stale records.
            result = await session.execute(update(PendingExtractionCandidate).where(
                PendingExtractionCandidate.id == row.id,
                PendingExtractionCandidate.tenant_id == uuid.UUID(tenant_id),
                PendingExtractionCandidate.proxy_user_id == uuid.UUID(proxy_user_id),
                PendingExtractionCandidate.status == "pending",
                PendingExtractionCandidate.content == row.content,
                PendingExtractionCandidate.last_seen_at == row.last_seen_at,
                PendingExtractionCandidate.metadata_json == row.metadata_json,
            ).values(status="expired").execution_options(synchronize_session=False))
            expired = expired or bool(result.rowcount)
    if expired:
        await session.commit()
    return views
