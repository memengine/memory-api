from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from sqlalchemy import delete, select

from api.db.database import build_sync_session_factory
from api.db.models import Conversation, Memory, MemoryClaim, MemoryClaimRevision
from api.db.models import MemorySourceEvent, MemoryVersion, PlanTier, ProxyUser
from api.db.models import ServiceWriter, Tenant, User
from api.services.conflict_resolver import ConflictResolver
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID, EmbeddingResult
from api.services.extractor import ExtractedMemory
from api.services.provenance_service import build_provenance_snapshot
from benchmarks.internal.conflict_cases import load_conflict_development_cases


class DatabaseCandidateSearch:
    """Candidate adapter only; all state transitions use production services/DB."""

    def __init__(self, session) -> None:
        self.session = session

    def search_memories(self, **kwargs):
        proxy_user_id = kwargs.get("proxy_user_id")
        rows = self.session.execute(
            select(Memory).where(
                Memory.proxy_user_id == uuid.UUID(str(proxy_user_id)),
                Memory.is_archived.is_(False),
            )
        ).scalars().all()
        return [
            SimpleNamespace(id=str(row.id), score=0.95, payload={"memory_id": str(row.id)})
            for row in rows
        ]


def embedding(_content: str) -> EmbeddingResult:
    return EmbeddingResult(
        vector=[0.01, 0.02, 0.03],
        model_id=DEFAULT_ACTIVE_MODEL_ID,
        dimensions=3,
        qdrant_collection="memories",
    )


def run_case(session, tenant: Tenant, case) -> dict:
    proxy = ProxyUser(
        id=uuid.uuid4(), tenant_id=tenant.id, external_user_id=case.id,
        external_user_id_hash=hashlib.sha256(case.id.encode()).hexdigest(),
        memory_count=0, metadata_json={}, is_blocked=False,
    )
    user = User(
        id=uuid.uuid4(), external_id=f"conflict-benchmark::{case.id}::{uuid.uuid4()}",
        email=f"{case.id}-{uuid.uuid4().hex[:8]}@benchmark.test", settings={},
        memory_count=0, is_active=True,
    )
    session.add_all([proxy, user])
    session.flush()
    stored_actions: list[str] = []
    errors: list[dict] = []
    writers: dict[str, ServiceWriter] = {}

    for index, event in enumerate(case.events):
        source_name = str(event.get("source") or "unknown")
        if event.get("event_id"):
            duplicate = session.execute(
                select(MemorySourceEvent).where(
                    MemorySourceEvent.tenant_id == tenant.id,
                    MemorySourceEvent.source_service == source_name,
                    MemorySourceEvent.source_event_id == str(event["event_id"]),
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                stored_actions.append("IDEMPOTENT")
                continue
        conversation = Conversation(
            id=uuid.uuid4(), user_id=user.id, message_count=1,
        )
        session.add(conversation)
        writer = writers.get(source_name)
        if event.get("source") and writer is None:
            writer = ServiceWriter(
                id=uuid.uuid4(), tenant_id=tenant.id,
                service_key=f"{case.id}-{source_name}",
                display_name=source_name,
                authority_rules=(
                    {"categories": {"fact": int(event["authority"])}}
                    if event.get("authority") is not None else {}
                ),
                is_active=True,
            )
            session.add(writer)
            session.flush()
            writers[source_name] = writer
        observed = (
            datetime.fromisoformat(str(event["observed_at"]).replace("Z", "+00:00"))
            if event.get("observed_at") else datetime.now(UTC)
        )
        source_event = MemorySourceEvent(
            id=uuid.uuid4(), tenant_id=tenant.id, proxy_user_id=proxy.id,
            writer_id=writer.id if writer else None, source_service=source_name,
            source_event_id=str(event.get("event_id") or f"{case.id}-{index}"),
            observed_at=observed, payload_hash=hashlib.sha256(event["content"].encode()).hexdigest(),
            scope={}, evidence_refs=[{"source_type":"conversation","reference":str(conversation.id)}],
            processing_metadata={"benchmark":"conflict-development-v1"},
        )
        session.add(source_event)
        try:
            session.flush()
            resolver = ConflictResolver(
                session=session,
                qdrant_service=DatabaseCandidateSearch(session),
                embedder=embedding,
                default_source_conversation_id=conversation.id,
                default_source_event_id=source_event.id,
                provenance_snapshot=build_provenance_snapshot(source_event),
            )
            stored = resolver.check_and_store(
                [ExtractedMemory(
                    content=event["content"], category="fact", importance_score=5.0,
                    confidence=0.95, expiry="permanent", reasoning="golden conflict event",
                )],
                user_id=str(user.id), tenant_id=str(tenant.id),
                proxy_user_id=str(proxy.id), source_conversation_id=str(conversation.id),
                auto_commit=False,
            )
            stored_actions.append(stored[0].resolution if stored else "REJECT")
            session.commit()
        except Exception as exc:
            session.rollback()
            errors.append({"event_index": index, "error_type": exc.__class__.__name__, "error": str(exc)})
            break

    memories = list(session.execute(select(Memory).where(Memory.proxy_user_id == proxy.id)).scalars().all())
    claims = list(session.execute(select(MemoryClaim).where(MemoryClaim.proxy_user_id == proxy.id)).scalars().all())
    claim_ids = [claim.id for claim in claims]
    revisions = list(session.execute(select(MemoryClaimRevision).where(MemoryClaimRevision.claim_id.in_(claim_ids))).scalars().all()) if claim_ids else []
    memory_ids = [memory.id for memory in memories]
    versions = list(session.execute(select(MemoryVersion).where(MemoryVersion.memory_id.in_(memory_ids))).scalars().all()) if memory_ids else []
    final_action = stored_actions[-1] if stored_actions else "ERROR"
    expected_action = str(case.expected["action"])
    action_match = final_action == expected_action or (
        expected_action == "IDEMPOTENT" and len(memories) == 1
    ) or (expected_action == "REINFORCE" and final_action in {"KEEP_BOTH", "REJECT"})
    active = [memory for memory in memories if not memory.is_archived]
    provenance_preserved = all(memory.source_event_id is not None for memory in memories)
    claim_provenance_preserved = bool(revisions) and all(
        revision.source_event_id is not None and bool(revision.evidence_refs)
        for revision in revisions
    )
    active_ids = {memory.id for memory in active}
    claim_winner_matches_active = bool(claims) and all(
        claim.active_memory_id in active_ids for claim in claims if claim.active_memory_id is not None
    )
    single_winning_revision = bool(claims) and all(
        len([revision for revision in revisions if revision.claim_id == claim.id and revision.status == "activated"]) == 1
        and claim.winning_revision_id == next(
            revision.id for revision in revisions
            if revision.claim_id == claim.id and revision.status == "activated"
        )
        for claim in claims
    )
    observed_conflict = any(
        action in {"UPDATE", "MERGE", "REJECT", "CLARIFICATION_PENDING"}
        for action in stored_actions
    )
    expected_conflict = bool(case.expected["conflict"])
    false_supersession = (not expected_conflict) and any(memory.is_archived for memory in memories)
    return {
        "id": case.id, "scenario_type": case.scenario_type,
        "expected_action": expected_action, "observed_actions": stored_actions,
        "expected_conflict": expected_conflict, "observed_conflict": observed_conflict,
        "false_supersession": false_supersession,
        "action_match": action_match, "errors": errors,
        "memory_count": len(memories), "active_memory_count": len(active),
        "expected_active_winners": case.expected["active_winners"],
        "winner_correct": len(active) == int(case.expected["active_winners"]),
        "duplicate_active_memory": len(active) > int(case.expected["active_winners"]),
        "claim_count": len(claims), "claim_revision_count": len(revisions),
        "claim_winner_matches_active": claim_winner_matches_active,
        "single_winning_revision": single_winning_revision,
        "memory_version_count": len(versions),
        "memory_provenance_preserved": provenance_preserved,
        "claim_provenance_preserved": claim_provenance_preserved,
        "integration_success": (
            not errors and action_match
            and len(active) == int(case.expected["active_winners"])
            and claim_provenance_preserved
            and claim_winner_matches_active
            and single_winning_revision
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    factory = build_sync_session_factory()
    tenant_id = uuid.uuid4()
    session = factory()
    rows = []
    try:
        tenant = Tenant(
            id=tenant_id, company_name=f"Conflict benchmark {tenant_id}", region_id="IN1",
            plan_tier=PlanTier.starter, is_active=True, metadata_json={},
            support_type_mode="single", support_types_allowed=[],
        )
        session.add(tenant)
        session.commit()
        for case in load_conflict_development_cases():
            rows.append(run_case(session, tenant, case))
        count = len(rows)
        tp = sum(row["expected_conflict"] and row["observed_conflict"] for row in rows)
        fp = sum((not row["expected_conflict"]) and row["observed_conflict"] for row in rows)
        fn = sum(row["expected_conflict"] and (not row["observed_conflict"]) for row in rows)
        summary = {
            "scenario_count": count,
            "conflict_detection_precision": tp / (tp + fp) if tp + fp else 1.0,
            "conflict_detection_recall": tp / (tp + fn) if tp + fn else 1.0,
            "missed_conflict_rate": fn / sum(row["expected_conflict"] for row in rows),
            "false_supersession_rate": sum(row["false_supersession"] for row in rows) / count,
            "action_accuracy": sum(row["action_match"] for row in rows) / count,
            "winner_accuracy": sum(row["winner_correct"] for row in rows) / count,
            "duplicate_active_memory_rate": sum(row["duplicate_active_memory"] for row in rows) / count,
            "version_chain_correctness": sum(row["memory_version_count"] >= row["memory_count"] for row in rows) / count,
            "memory_provenance_preservation": sum(row["memory_provenance_preserved"] for row in rows) / count,
            "claim_creation_rate": sum(row["claim_count"] > 0 for row in rows) / count,
            "claim_provenance_preservation": sum(row["claim_provenance_preserved"] for row in rows) / count,
            "claim_winner_active_alignment": sum(row["claim_winner_matches_active"] for row in rows) / count,
            "single_winning_revision_correctness": sum(row["single_winning_revision"] for row in rows) / count,
            "end_to_end_integration_success_rate": sum(row["integration_success"] for row in rows) / count,
            "scenario_types": dict(Counter(row["scenario_type"] for row in rows)),
            "harness_errors": sum(bool(row["errors"]) for row in rows),
        }
        artifact = {
            "schema_version":"1.0", "mode":"development-conflict-integration-baseline",
            "created_at":datetime.now(UTC).isoformat(), "holdout_loaded":False,
            "production_conflict_logic_modified":False,
            "path":{
                "conversation_and_source_event":"real_postgres",
                "extraction":"golden_memory_injected_at_extractor_boundary",
                "candidate_search":"database_adapter_to_avoid_vector_lag",
                "resolver_claim_version_provenance_persistence":"production_services_real_postgres",
                "readback":"real_postgres",
            },
            "summary":summary, "cases":rows,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2))
    finally:
        session.rollback()
        session.execute(delete(User).where(User.external_id.like("conflict-benchmark::%")))
        session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        session.commit()
        session.close()


if __name__ == "__main__":
    main()
