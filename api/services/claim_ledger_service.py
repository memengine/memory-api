from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from api.db.models import ExtractionJob
from api.db.models import Memory
from api.db.models import MemorySourceEvent
from api.db.models import MemoryCategory
from api.db.models import MemoryClaim
from api.db.models import MemoryClaimRevision
from api.services.claim_versions import CLAIM_SCHEMA_VERSION
from api.services.claim_versions import processor_version_for_resolution
from api.services.provenance_service import build_provenance_snapshot


_SPACE_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[\s.?!]+$")
_POSSESSIVE_PREFIX_RE = re.compile(
    r"^(?:the\s+)?(?:user|customer|student|person|learner)'?s\s+", re.I
)
_CLAIM_SPLIT_RE = re.compile(
    r"\b(?:is|are|was|were|uses|use|prefers|prefer|needs|need|wants|want|has|have)\b",
    re.I,
)


@dataclass(slots=True)
class ClaimIdentity:
    fingerprint: str
    subject_key: str
    predicate_key: str
    value: str
    scope: dict[str, Any]


class ClaimLedgerService:
    """Maintains a compact source-backed claim ledger.

    The ledger is write-side only for now. Retrieval remains backed by the
    existing memory/vector path, so this does not add graph traversal latency to
    normal mem.get() calls.
    """

    def __init__(self, session: Any) -> None:
        self.session = session

    def record_memory(
        self,
        memory: Memory,
        *,
        tenant_id: str | uuid.UUID | None,
        proxy_user_id: str | uuid.UUID | None,
        provenance: dict[str, Any] | None,
        resolution: str,
        decision_evidence: dict[str, Any] | None = None,
    ) -> MemoryClaim | None:
        if tenant_id is None or proxy_user_id is None:
            return None
        if not hasattr(self.session, "add") or not hasattr(self.session, "execute"):
            return None

        identity = build_claim_identity(
            content=memory.content,
            category=memory.category.value
            if hasattr(memory.category, "value")
            else str(memory.category),
            scope=(provenance or {}).get("scope") or {},
        )
        claim = self._find_claim(
            tenant_id=uuid.UUID(str(tenant_id)),
            proxy_user_id=uuid.UUID(str(proxy_user_id)),
            fingerprint=identity.fingerprint,
        )
        priority = authority_priority(
            provenance,
            memory.category.value
            if hasattr(memory.category, "value")
            else str(memory.category),
        )
        observed_at = parse_observed_at((provenance or {}).get("observed_at"))
        should_activate = not memory.is_archived
        revision_status = "activated" if should_activate else "disputed"

        if claim is None:
            claim = MemoryClaim(
                tenant_id=uuid.UUID(str(tenant_id)),
                proxy_user_id=uuid.UUID(str(proxy_user_id)),
                category=memory.category
                if isinstance(memory.category, MemoryCategory)
                else MemoryCategory(str(memory.category)),
                claim_fingerprint=identity.fingerprint,
                subject_key=identity.subject_key,
                predicate_key=identity.predicate_key,
                scope=identity.scope,
                active_value=identity.value if should_activate else None,
                status="active" if should_activate else "disputed",
                active_memory_id=memory.id if should_activate else None,
                authority_priority=priority,
                confidence_score=float(memory.confidence_score or 0.0),
                observed_at=observed_at,
                effective_at=datetime.now(UTC),
            )
            self.session.add(claim)
            self._flush()
        else:
            active_value = normalize_text(claim.active_value or "")
            incoming_value = normalize_text(identity.value)
            if should_activate and self._incoming_wins(
                claim=claim,
                incoming_priority=priority,
                incoming_confidence=float(memory.confidence_score or 0.0),
                incoming_observed_at=observed_at,
            ):
                self._mark_existing_winner_superseded(claim)
                claim.active_value = identity.value
                claim.status = "active"
                claim.active_memory_id = memory.id
                claim.authority_priority = priority
                claim.confidence_score = float(memory.confidence_score or 0.0)
                claim.observed_at = observed_at or claim.observed_at
                claim.effective_at = datetime.now(UTC)
                claim.updated_at = datetime.now(UTC)
                revision_status = "activated"
            elif should_activate and active_value and incoming_value != active_value:
                claim.status = "disputed"
                claim.updated_at = datetime.now(UTC)
                revision_status = "disputed"
            elif should_activate:
                revision_status = "asserted"

        revision = MemoryClaimRevision(
            claim_id=claim.id,
            memory_id=memory.id,
            source_event_id=memory.source_event_id,
            source_writer_id=parse_uuid((provenance or {}).get("writer_id")),
            asserted_value=identity.value,
            status=revision_status,
            authority_priority=priority,
            confidence_score=float(memory.confidence_score or 0.0),
            observed_at=observed_at,
            evidence_refs=list((provenance or {}).get("evidence") or []),
            resolution_reason=resolution,
            decision_evidence=decision_evidence or {},
            schema_version=CLAIM_SCHEMA_VERSION,
            processor_version=processor_version_for_resolution(resolution),
        )
        self.session.add(revision)
        self._flush()
        if revision_status == "activated":
            claim.winning_revision_id = revision.id
        return claim

    def record_domain_fields(
        self,
        *,
        domain_record: Any,
        domain: str,
        fields_updated: set[str] | list[str],
        field_categories: dict[str, str],
        job_id: str,
    ) -> list[MemoryClaim]:
        provenance = self._provenance_for_job(job_id)
        claims: list[MemoryClaim] = []
        for field in sorted(set(fields_updated)):
            if field not in field_categories:
                continue
            raw_value = getattr(domain_record, field, None)
            value = serialize_claim_value(raw_value)
            if value is None:
                continue
            claim = self._record_domain_field(
                tenant_id=domain_record.tenant_id,
                proxy_user_id=domain_record.proxy_user_id,
                domain=domain,
                record_id=str(domain_record.id),
                field=field,
                category=field_categories[field],
                value=value,
                provenance=provenance,
            )
            if claim is not None:
                claims.append(claim)
        return claims

    def _record_domain_field(
        self,
        *,
        tenant_id: uuid.UUID,
        proxy_user_id: uuid.UUID,
        domain: str,
        record_id: str,
        field: str,
        category: str,
        value: str,
        provenance: dict[str, Any] | None,
    ) -> MemoryClaim | None:
        scope = {**dict((provenance or {}).get("scope") or {}), "domain": domain}
        identity = build_domain_claim_identity(
            domain=domain,
            field=field,
            category=category,
            value=value,
            scope=scope,
        )
        claim = self._find_claim(
            tenant_id=uuid.UUID(str(tenant_id)),
            proxy_user_id=uuid.UUID(str(proxy_user_id)),
            fingerprint=identity.fingerprint,
        )
        priority = domain_authority_priority(provenance, domain, field, category)
        confidence = domain_field_confidence(provenance)
        observed_at = parse_observed_at((provenance or {}).get("observed_at"))
        writer_id = parse_uuid((provenance or {}).get("writer_id"))
        revision_status = "activated"

        if claim is None:
            claim = MemoryClaim(
                tenant_id=uuid.UUID(str(tenant_id)),
                proxy_user_id=uuid.UUID(str(proxy_user_id)),
                category=MemoryCategory(category),
                claim_fingerprint=identity.fingerprint,
                subject_key=identity.subject_key,
                predicate_key=identity.predicate_key,
                scope=identity.scope,
                active_value=value,
                status="active",
                active_memory_id=None,
                authority_priority=priority,
                confidence_score=confidence,
                observed_at=observed_at,
                effective_at=datetime.now(UTC),
            )
            self.session.add(claim)
            self._flush()
        else:
            winner = self._winning_revision(claim)
            same_writer = winner is None or winner.source_writer_id == writer_id
            same_value = claim.active_value == value
            if same_value:
                revision_status = "asserted"
            elif same_writer or self._incoming_wins(
                claim=claim,
                incoming_priority=priority,
                incoming_confidence=confidence,
                incoming_observed_at=observed_at,
            ):
                self._mark_existing_winner_superseded(claim)
                claim.active_value = value
                claim.status = "active"
                claim.authority_priority = priority
                claim.confidence_score = confidence
                claim.observed_at = observed_at or claim.observed_at
                claim.effective_at = datetime.now(UTC)
                claim.updated_at = datetime.now(UTC)
                revision_status = "activated"
            else:
                claim.status = "disputed"
                claim.updated_at = datetime.now(UTC)
                revision_status = "disputed"

        revision = MemoryClaimRevision(
            claim_id=claim.id,
            memory_id=None,
            source_event_id=parse_uuid((provenance or {}).get("source_event_id")),
            source_writer_id=writer_id,
            source_domain=domain,
            source_domain_record_id=record_id,
            source_field=field,
            asserted_value=value,
            status=revision_status,
            authority_priority=priority,
            confidence_score=confidence,
            observed_at=observed_at,
            evidence_refs=list((provenance or {}).get("evidence") or []),
            resolution_reason="domain_field_extraction",
            decision_evidence={},
            schema_version=CLAIM_SCHEMA_VERSION,
            processor_version=processor_version_for_resolution("domain_field_extraction"),
        )
        self.session.add(revision)
        self._flush()
        if revision_status == "activated":
            claim.winning_revision_id = revision.id
        return claim

    def _provenance_for_job(self, job_id: str) -> dict[str, Any] | None:
        try:
            job_uuid = uuid.UUID(str(job_id))
        except (TypeError, ValueError):
            return None
        job = self.session.execute(
            select(ExtractionJob)
            .where(ExtractionJob.id == job_uuid)
            .options(
                selectinload(ExtractionJob.source_event).selectinload(
                    MemorySourceEvent.writer
                )
            )
        ).scalar_one_or_none()
        if job is None or job.source_event is None:
            return None
        return build_provenance_snapshot(job.source_event)

    def _winning_revision(self, claim: MemoryClaim) -> MemoryClaimRevision | None:
        if claim.winning_revision_id is None or not hasattr(self.session, "get"):
            return None
        return self.session.get(MemoryClaimRevision, claim.winning_revision_id)

    async def apply_conflict_selection(
        self,
        *,
        memory_a: Memory,
        memory_b: Memory,
        selection: str,
        reason: str,
    ) -> None:
        if not hasattr(self.session, "execute"):
            return
        rows = (
            (
                await self.session.execute(
                    select(MemoryClaimRevision)
                    .where(
                        MemoryClaimRevision.memory_id.in_([memory_a.id, memory_b.id])
                    )
                    .options(selectinload(MemoryClaimRevision.claim))
                )
            )
            .scalars()
            .all()
        )
        affected_claims = {revision.claim_id: revision.claim for revision in rows}
        selected = {
            "A": {memory_a.id},
            "B": {memory_b.id},
            "both": {memory_a.id, memory_b.id},
            "neither": set(),
        }[selection]
        for revision in rows:
            revision.status = (
                "activated" if revision.memory_id in selected else "rejected"
            )
            revision.resolution_reason = reason
            revision.decision_evidence = {
                "action": "manual_resolution",
                "decision_level": "manual",
                "reason_codes": ["human_review_completed"],
                "explanation": reason,
                "details": {"selection": selection},
            }

        for claim in affected_claims.values():
            claim_revisions = [
                revision for revision in rows if revision.claim_id == claim.id
            ]
            active_revisions = [
                revision
                for revision in claim_revisions
                if revision.memory_id in selected
            ]
            if len(active_revisions) == 1:
                winner = active_revisions[0]
                claim.status = "active"
                claim.active_value = winner.asserted_value
                claim.active_memory_id = winner.memory_id
                claim.winning_revision_id = winner.id
                claim.authority_priority = winner.authority_priority
                claim.confidence_score = winner.confidence_score
                claim.observed_at = winner.observed_at or claim.observed_at
            elif len(active_revisions) > 1:
                claim.status = "active"
                claim.active_value = "; ".join(
                    revision.asserted_value for revision in active_revisions
                )
                claim.active_memory_id = None
                claim.winning_revision_id = None
            else:
                claim.status = "archived"
                claim.active_value = None
                claim.active_memory_id = None
                claim.winning_revision_id = None
            claim.updated_at = datetime.now(UTC)

    def _find_claim(
        self,
        *,
        tenant_id: uuid.UUID,
        proxy_user_id: uuid.UUID,
        fingerprint: str,
    ) -> MemoryClaim | None:
        result = self.session.execute(
            select(MemoryClaim).where(
                MemoryClaim.tenant_id == tenant_id,
                MemoryClaim.proxy_user_id == proxy_user_id,
                MemoryClaim.claim_fingerprint == fingerprint,
            )
        )
        if hasattr(result, "scalar_one_or_none"):
            return result.scalar_one_or_none()
        return None

    @staticmethod
    def _incoming_wins(
        *,
        claim: MemoryClaim,
        incoming_priority: int,
        incoming_confidence: float,
        incoming_observed_at: datetime | None,
    ) -> bool:
        if incoming_priority != int(claim.authority_priority or 50):
            return incoming_priority > int(claim.authority_priority or 50)
        current_observed_at = parse_observed_at(claim.observed_at)
        if incoming_observed_at is not None and current_observed_at is not None:
            return incoming_observed_at > current_observed_at
        if abs(incoming_confidence - float(claim.confidence_score or 0.0)) > 0.20:
            return incoming_confidence > float(claim.confidence_score or 0.0)
        return False

    def _mark_existing_winner_superseded(self, claim: MemoryClaim) -> None:
        winning_revision_id = claim.winning_revision_id
        if winning_revision_id is None:
            return
        revision = None
        if hasattr(self.session, "get"):
            revision = self.session.get(MemoryClaimRevision, winning_revision_id)
        if revision is not None:
            revision.status = "superseded"

    def _flush(self) -> None:
        if hasattr(self.session, "flush"):
            self.session.flush()


def build_claim_identity(
    *,
    content: str,
    category: str,
    scope: dict[str, Any] | None = None,
) -> ClaimIdentity:
    normalized = normalize_text(content)
    subject_key = "user"
    predicate_key = normalized
    value = normalized

    split = _CLAIM_SPLIT_RE.search(normalized)
    if split is not None:
        left = normalized[: split.start()].strip()
        right = normalized[split.end() :].strip()
        if left and right:
            predicate_key = _POSSESSIVE_PREFIX_RE.sub("", left).strip() or left
            value = right

    scope_key = normalize_scope(scope or {})
    fingerprint_base = {
        "category": category,
        "subject": subject_key,
        "predicate": predicate_key,
        "scope": scope_key,
    }
    canonical = repr(sorted(fingerprint_base.items()))
    return ClaimIdentity(
        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        subject_key=subject_key,
        predicate_key=predicate_key[:255],
        value=value,
        scope=scope or {},
    )


def build_domain_claim_identity(
    *,
    domain: str,
    field: str,
    category: str,
    value: str,
    scope: dict[str, Any] | None = None,
) -> ClaimIdentity:
    subject_key = "user"
    predicate_key = f"{domain}.{field}"
    scope_key = normalize_scope(scope or {})
    canonical = repr(
        sorted(
            {
                "category": category,
                "subject": subject_key,
                "predicate": predicate_key,
                "scope": scope_key,
            }.items()
        )
    )
    return ClaimIdentity(
        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        subject_key=subject_key,
        predicate_key=predicate_key[:255],
        value=value,
        scope=scope or {},
    )


def serialize_claim_value(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def domain_authority_priority(
    provenance: dict[str, Any] | None,
    domain: str,
    field: str,
    category: str,
) -> int:
    rules = dict((provenance or {}).get("authority_rules") or {})
    field_rules = dict(rules.get("domain_fields") or {})
    for key in (f"{domain}.{field}", field):
        if key in field_rules:
            try:
                return max(0, min(int(field_rules[key]), 100))
            except (TypeError, ValueError):
                break
    return authority_priority(provenance, category)


def domain_field_confidence(provenance: dict[str, Any] | None) -> float:
    processing = dict((provenance or {}).get("processing") or {})
    try:
        return max(0.0, min(float(processing.get("extraction_confidence", 1.0)), 1.0))
    except (TypeError, ValueError):
        return 1.0


def normalize_text(value: str) -> str:
    lowered = value.casefold().strip()
    lowered = _TRAILING_PUNCT_RE.sub("", lowered)
    return _SPACE_RE.sub(" ", lowered)


def normalize_scope(scope: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    stable_items: list[tuple[str, str]] = []
    for key, value in sorted(scope.items(), key=lambda item: str(item[0])):
        if value is None:
            continue
        stable_items.append((str(key), str(value)))
    return tuple(stable_items)


def authority_priority(provenance: dict[str, Any] | None, category: str) -> int:
    rules = dict((provenance or {}).get("authority_rules") or {})
    category_rules = dict(rules.get("categories") or {})
    raw_priority = category_rules.get(category, rules.get("default_priority", 50))
    try:
        return max(0, min(int(raw_priority), 100))
    except (TypeError, ValueError):
        return 50


def parse_observed_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
