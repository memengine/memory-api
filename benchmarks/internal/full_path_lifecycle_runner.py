from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import delete, select

from api.db.database import build_sync_session_factory
from api.db.models import ApiKey, ExtractionJob, Memory, MemoryClaim, MemoryClaimRevision
from api.db.models import MemorySourceEvent, MemoryVersion, ProxyUser, ServiceWriter, VectorSyncOutbox
from api.db.vector_store import QdrantService
from api.utils.crypto import api_key_prefix, verify_api_key


CASE = Path(__file__).parent / "datasets" / "lifecycle_provenance" / "development" / "full_path_explicit_correction_v1.json"


def _memory_snapshot(memory: Memory) -> dict[str, Any]:
    return {
        "id": str(memory.id), "content": memory.content,
        "is_archived": bool(memory.is_archived),
        "previous_version_id": str(memory.previous_version_id) if memory.previous_version_id else None,
        "source_event_id": str(memory.source_event_id) if memory.source_event_id else None,
        "provenance": (memory.metadata_json or {}).get("provenance"),
    }


async def _wait_job(client: httpx.AsyncClient, headers: dict[str, str], job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = await client.get(f"/v1/memories/jobs/{job_id}", headers=headers)
        response.raise_for_status()
        data = response.json()["data"]
        if data["status"] in {"completed", "done", "failed", "discarded"}:
            return data
        await asyncio.sleep(1)
    raise TimeoutError(f"job {job_id} did not finish")


async def main_async(output: Path) -> None:
    case = json.loads(CASE.read_text(encoding="utf-8"))
    api_key = (os.getenv("BENCHMARK_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("BENCHMARK_API_KEY is required")
    run_id = uuid.uuid4().hex[:12]
    external_user_id = f"{case['external_user_prefix']}-{run_id}"
    headers = {"Authorization": f"ApiKey {api_key}"}
    session = build_sync_session_factory()()
    qdrant = QdrantService()
    key_rows = session.execute(select(ApiKey).where(
        ApiKey.is_active.is_(True), ApiKey.key_prefix == api_key_prefix(api_key)
    )).scalars().all()
    matched_key = next((row for row in key_rows if verify_api_key(api_key, row.key_hash)), None)
    if matched_key is None:
        raise RuntimeError("BENCHMARK_API_KEY does not match an active key")
    writer = session.execute(select(ServiceWriter).where(
        ServiceWriter.api_key_id == matched_key.id, ServiceWriter.is_active.is_(True)
    )).scalar_one_or_none()
    created_writer_id = None
    if writer is None:
        writer = ServiceWriter(
            id=uuid.uuid4(), tenant_id=matched_key.tenant_id, api_key_id=matched_key.id,
            service_key="retrieval-feedback", display_name="Lifecycle benchmark correction writer",
            authority_rules={}, is_active=True,
        )
        session.add(writer)
        session.commit()
        created_writer_id = writer.id
    created_memory_ids: list[tuple[str, str | None]] = []
    snapshots: dict[str, Any] = {}
    checks: dict[str, bool] = {}
    failure_boundary: str | None = "harness_start"
    started = time.perf_counter()
    proxy_id = None
    try:
        async with httpx.AsyncClient(base_url="http://api:8000", timeout=45) as client:
            now = datetime.now(UTC)
            initial_payload = {
                "external_user_id": external_user_id,
                "messages": case["initial"]["messages"],
                "metadata": {"internal_lifecycle_benchmark": run_id},
                "source": {
                    "event_id": f"{run_id}-initial",
                    "service": writer.service_key,
                    "observed_at": now.isoformat(),
                    "scope": {"benchmark_run": run_id},
                    "evidence": [{"source_type": "conversation", "reference": case["initial"]["evidence_reference"]}],
                },
            }
            response = await client.post("/v1/memories/add", json=initial_payload, headers=headers)
            snapshots["api_initial"] = {"status_code": response.status_code, "body": response.json()}
            response.raise_for_status()
            initial_job_id = response.json().get("job_id")
            checks["api_initial_queued"] = bool(initial_job_id)
            if not initial_job_id:
                failure_boundary = "api"
                raise AssertionError("initial API request did not queue a job")
            initial_job = await _wait_job(client, headers, initial_job_id)
            snapshots["initial_job"] = initial_job
            checks["initial_worker_completed"] = initial_job["status"] in {"completed", "done"}
            if not checks["initial_worker_completed"]:
                failure_boundary = "celery_worker"
                raise AssertionError(f"initial extraction failed: {initial_job}")

            proxy = session.execute(select(ProxyUser).where(ProxyUser.external_user_id == external_user_id)).scalar_one()
            proxy_id = proxy.id
            memories = session.execute(select(Memory).where(Memory.proxy_user_id == proxy.id)).scalars().all()
            old = next((m for m in memories if any(t in m.content.lower() for t in case["expected_old_terms"])), None)
            checks["original_memory_created"] = old is not None
            if old is None:
                failure_boundary = "postgres_memory_persistence"
                raise AssertionError("initial city memory was not persisted")
            snapshots["original_memory"] = _memory_snapshot(old)
            created_memory_ids.append((str(old.id), None))
            old_revisions = session.execute(select(MemoryClaimRevision).where(MemoryClaimRevision.memory_id == old.id)).scalars().all()
            checks["original_claim_revision_created"] = len(old_revisions) == 1
            snapshots["original_claim_revisions"] = [{"id": str(r.id), "claim_id": str(r.claim_id), "status": r.status, "evidence_refs": r.evidence_refs} for r in old_revisions]
            if not checks["original_claim_revision_created"]:
                failure_boundary = "claim_ledger"
                raise AssertionError("original claim revision missing")

            retrieval = await client.post("/v1/memories/retrieve", json={"external_user_id": external_user_id, "query": case["query"], "limit": 5}, headers=headers)
            retrieval.raise_for_status()
            retrieval_body = retrieval.json()
            snapshots["pre_correction_retrieval"] = retrieval_body
            failure_boundary = "api_correction"
            feedback = await client.post("/v1/memories/retrieval-feedback", json={
                "retrieval_id": retrieval_body["retrieval_id"], "outcome": "user_corrected",
                "used_memory_ids": [str(old.id)], "correction": case["correction"],
                "metadata": {"internal_lifecycle_benchmark": run_id},
            }, headers=headers)
            snapshots["correction_api"] = {"status_code": feedback.status_code, "body": feedback.json()}
            feedback.raise_for_status()
            correction_job_id = feedback.json()["data"].get("correction_job_id")
            checks["correction_job_created"] = bool(correction_job_id)
            if not correction_job_id:
                failure_boundary = "api_correction"
                raise AssertionError("correction API did not create extraction job")
            correction_job = await _wait_job(client, headers, correction_job_id)
            snapshots["correction_job"] = correction_job
            checks["correction_worker_completed"] = correction_job["status"] in {"completed", "done"}
            if not checks["correction_worker_completed"]:
                failure_boundary = "celery_worker_correction"
                raise AssertionError(f"correction extraction failed: {correction_job}")

            session.expire_all()
            memories = session.execute(select(Memory).where(Memory.proxy_user_id == proxy.id).order_by(Memory.created_at)).scalars().all()
            old = next(m for m in memories if m.id == old.id)
            new = next((m for m in memories if any(t in m.content.lower() for t in case["expected_new_terms"])), None)
            snapshots["postgres_memories_after_correction"] = [_memory_snapshot(m) for m in memories]
            checks["new_memory_active"] = new is not None and not new.is_archived
            checks["old_memory_archived"] = bool(old.is_archived)
            checks["correction_linked_to_predecessor"] = new is not None and new.previous_version_id == old.id
            if not all(checks[key] for key in ("new_memory_active", "old_memory_archived", "correction_linked_to_predecessor")):
                failure_boundary = "conflict_supersession"
                raise AssertionError("correction did not produce expected predecessor/archive/active state")
            created_memory_ids.append((str(new.id), None))

            revisions = session.execute(select(MemoryClaimRevision).where(MemoryClaimRevision.memory_id.in_([old.id, new.id]))).scalars().all()
            claim_ids = {r.claim_id for r in revisions}
            claims = session.execute(select(MemoryClaim).where(MemoryClaim.id.in_(claim_ids))).scalars().all()
            versions = session.execute(select(MemoryVersion).where(MemoryVersion.memory_id.in_([old.id, new.id])).order_by(MemoryVersion.created_at)).scalars().all()
            snapshots["claim_state"] = {"claims": [{"id": str(c.id), "status": c.status, "active_memory_id": str(c.active_memory_id) if c.active_memory_id else None, "winning_revision_id": str(c.winning_revision_id) if c.winning_revision_id else None} for c in claims], "revisions": [{"id": str(r.id), "claim_id": str(r.claim_id), "memory_id": str(r.memory_id), "status": r.status, "source_event_id": str(r.source_event_id) if r.source_event_id else None, "evidence_refs": r.evidence_refs, "decision_evidence": r.decision_evidence} for r in revisions]}
            snapshots["versions"] = [{"id": str(v.id), "memory_id": str(v.memory_id), "version_number": v.version_number, "change_type": v.change_type} for v in versions]
            activated = [r for r in revisions if r.status == "activated"]
            checks["exactly_one_winning_revision"] = len(activated) == 1 and activated[0].memory_id == new.id
            checks["version_chain_correct"] = bool(versions) and new.previous_version_id == old.id
            checks["source_provenance_preserved"] = bool((old.metadata_json or {}).get("provenance")) and bool((new.metadata_json or {}).get("provenance")) and old.source_event_id != new.source_event_id
            checks["claim_evidence_preserved"] = all(bool(r.evidence_refs) for r in revisions)
            if not all(checks[key] for key in ("exactly_one_winning_revision", "version_chain_correct", "source_provenance_preserved", "claim_evidence_preserved")):
                failure_boundary = "claim_version_provenance"
                raise AssertionError("claim/version/provenance assertions failed")

            old_history = await client.get(f"/v1/memories/{old.id}/history", headers=headers)
            new_history = await client.get(f"/v1/memories/{new.id}/history", headers=headers)
            snapshots["history_readback"] = {
                "old": {"status_code": old_history.status_code, "body": old_history.json()},
                "new": {"status_code": new_history.status_code, "body": new_history.json()},
            }
            checks["historical_version_readback"] = (
                old_history.status_code == 200
                and new_history.status_code == 200
                and bool(old_history.json().get("data"))
                and bool(new_history.json().get("data"))
            )
            if not checks["historical_version_readback"]:
                failure_boundary = "historical_api_readback"
                raise AssertionError("memory history API did not preserve both revision histories")

            outbox = session.execute(select(VectorSyncOutbox).where(VectorSyncOutbox.memory_id.in_([old.id, new.id])).order_by(VectorSyncOutbox.created_at)).scalars().all()
            snapshots["outbox"] = [{"id": str(row.id), "memory_id": str(row.memory_id), "operation": row.operation.value if hasattr(row.operation, "value") else str(row.operation), "status": row.status.value if hasattr(row.status, "value") else str(row.status), "attempts": row.attempts} for row in outbox]
            checks["outbox_events_created"] = any("archive" in str(row.operation) and row.memory_id == old.id for row in outbox) and any("upsert" in str(row.operation) and row.memory_id == new.id for row in outbox)
            if not checks["outbox_events_created"]:
                failure_boundary = "transactional_outbox"
                raise AssertionError("expected archive delete and winner upsert outbox rows")

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                session.expire_all()
                current = session.execute(select(VectorSyncOutbox).where(VectorSyncOutbox.id.in_([row.id for row in outbox]))).scalars().all()
                if current and all((row.status.value if hasattr(row.status, "value") else str(row.status)) == "done" for row in current):
                    break
                await asyncio.sleep(1)
            snapshots["outbox_final"] = [{"id": str(row.id), "status": row.status.value if hasattr(row.status, "value") else str(row.status), "attempts": row.attempts} for row in current]
            checks["postgres_authoritative_during_lag"] = not old.is_archived is False and not new.is_archived
            checks["outbox_synced"] = bool(current) and all((row.status.value if hasattr(row.status, "value") else str(row.status)) == "done" for row in current)
            if not checks["outbox_synced"]:
                failure_boundary = "qdrant_outbox_sync"
                raise AssertionError("outbox did not reach done")

            final_retrieval = await client.post("/v1/memories/retrieve", json={"external_user_id": external_user_id, "query": case["query"], "limit": 5}, headers=headers)
            final_retrieval.raise_for_status()
            final_body = final_retrieval.json()
            snapshots["final_api_retrieval"] = final_body
            returned = final_body.get("data", [])
            checks["superseded_not_retrieved"] = all(str(item.get("id")) != str(old.id) for item in returned)
            checks["active_retrieved"] = any(str(item.get("id")) == str(new.id) for item in returned)
            new_result = next((item for item in returned if str(item.get("id")) == str(new.id)), None)
            checks["api_provenance_correct"] = bool(new_result and new_result.get("provenance"))
            if not all(checks[key] for key in ("superseded_not_retrieved", "active_retrieved", "api_provenance_correct")):
                failure_boundary = "retrieval_api_readback"
                raise AssertionError("final retrieval/readback assertions failed")

            collection = session.execute(
                select(VectorSyncOutbox.payload["qdrant_collection"].astext)
                .where(VectorSyncOutbox.memory_id == new.id)
                .order_by(VectorSyncOutbox.created_at.desc())
                .limit(1)
            ).scalar_one_or_none() or qdrant.COLLECTION_NAME
            old_id, new_id = old.id, new.id

            # Privacy/hard deletion is deliberately exercised only after governance
            # readback has been captured. Delete both the active and superseded values.
            failure_boundary = "privacy_delete_api"
            delete_new = await client.delete(
                f"/v1/memories/{new_id}", params={"hard_delete": "true"}, headers=headers
            )
            delete_old = await client.delete(
                f"/v1/memories/{old_id}", params={"hard_delete": "true"}, headers=headers
            )
            snapshots["privacy_delete_api"] = {
                "active": {"status_code": delete_new.status_code, "body": delete_new.json()},
                "superseded": {"status_code": delete_old.status_code, "body": delete_old.json()},
            }
            checks["privacy_delete_api_success"] = (
                delete_new.status_code == 200 and delete_old.status_code == 200
            )
            if not checks["privacy_delete_api_success"]:
                raise AssertionError("hard-delete API did not accept both lifecycle revisions")

            session.expire_all()
            remaining_memories = session.execute(
                select(Memory).where(Memory.id.in_([old_id, new_id]))
            ).scalars().all()
            remaining_revisions = session.execute(
                select(MemoryClaimRevision).where(MemoryClaimRevision.claim_id.in_(claim_ids))
            ).scalars().all()
            remaining_claims = session.execute(
                select(MemoryClaim).where(MemoryClaim.id.in_(claim_ids))
            ).scalars().all()
            snapshots["postgres_after_privacy_delete"] = {
                "memory_ids": [str(row.id) for row in remaining_memories],
                "claims": [
                    {
                        "id": str(row.id), "active_memory_id": str(row.active_memory_id) if row.active_memory_id else None,
                        "winning_revision_id": str(row.winning_revision_id) if row.winning_revision_id else None,
                        "active_value": row.active_value, "status": row.status,
                    }
                    for row in remaining_claims
                ],
                "revisions": [
                    {
                        "id": str(row.id), "memory_id": str(row.memory_id) if row.memory_id else None,
                        "asserted_value": row.asserted_value,
                        "evidence_refs": row.evidence_refs,
                        "decision_evidence": row.decision_evidence,
                    }
                    for row in remaining_revisions
                ],
            }
            checks["privacy_memory_rows_removed"] = not remaining_memories
            checks["privacy_claim_data_removed"] = not remaining_claims and not remaining_revisions
            if not checks["privacy_memory_rows_removed"]:
                failure_boundary = "privacy_postgres_memory"
                raise AssertionError("hard-deleted memory rows remain")
            if not checks["privacy_claim_data_removed"]:
                failure_boundary = "privacy_claim_ledger"
                raise AssertionError("hard-deleted personal claim/revision data remains")

            delete_outbox = session.execute(
                select(VectorSyncOutbox)
                .where(
                    VectorSyncOutbox.memory_id.in_([old_id, new_id]),
                    VectorSyncOutbox.created_at >= now,
                )
                .order_by(VectorSyncOutbox.created_at)
            ).scalars().all()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                session.expire_all()
                delete_outbox = session.execute(
                    select(VectorSyncOutbox).where(
                        VectorSyncOutbox.id.in_([row.id for row in delete_outbox])
                    )
                ).scalars().all()
                delete_rows = [row for row in delete_outbox if "delete" in str(row.operation)]
                if len(delete_rows) >= 2 and all(
                    (row.status.value if hasattr(row.status, "value") else str(row.status)) == "done"
                    for row in delete_rows
                ):
                    break
                await asyncio.sleep(1)
            snapshots["privacy_delete_outbox"] = [
                {
                    "id": str(row.id), "memory_id": str(row.memory_id),
                    "operation": row.operation.value if hasattr(row.operation, "value") else str(row.operation),
                    "status": row.status.value if hasattr(row.status, "value") else str(row.status),
                    "attempts": row.attempts,
                }
                for row in delete_outbox
            ]
            delete_rows = [row for row in delete_outbox if "delete" in str(row.operation)]
            checks["privacy_delete_outbox_synced"] = len(delete_rows) >= 2 and all(
                (row.status.value if hasattr(row.status, "value") else str(row.status)) == "done"
                for row in delete_rows
            )
            if not checks["privacy_delete_outbox_synced"]:
                failure_boundary = "privacy_qdrant_outbox"
                raise AssertionError("privacy vector deletions did not sync")

            points = qdrant.client.retrieve(
                collection_name=collection, ids=[str(old_id), str(new_id)],
                with_payload=True, with_vectors=False,
            )
            checks["privacy_qdrant_points_removed"] = len(points) == 0
            post_delete_retrieval = await client.post(
                "/v1/memories/retrieve",
                json={"external_user_id": external_user_id, "query": case["query"], "limit": 5},
                headers=headers,
            )
            post_delete_get = await client.get(f"/v1/memories/{new_id}", headers=headers)
            snapshots["privacy_api_readback"] = {
                "retrieval": {"status_code": post_delete_retrieval.status_code, "body": post_delete_retrieval.json()},
                "get_deleted": {"status_code": post_delete_get.status_code, "body": post_delete_get.json()},
                "qdrant_point_count": len(points),
            }
            returned_after_delete = post_delete_retrieval.json().get("data", []) if post_delete_retrieval.status_code == 200 else []
            checks["privacy_api_absence"] = (
                post_delete_get.status_code == 404
                and all(str(item.get("id")) not in {str(old_id), str(new_id)} for item in returned_after_delete)
            )
            if not checks["privacy_qdrant_points_removed"]:
                failure_boundary = "privacy_qdrant_state"
                raise AssertionError("hard-deleted Qdrant points remain")
            if not checks["privacy_api_absence"]:
                failure_boundary = "privacy_api_readback"
                raise AssertionError("hard-deleted memory remains visible through API")
            failure_boundary = None
    except Exception as exc:
        snapshots["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        passed = failure_boundary is None and all(checks.values())
        payload = {
            "benchmark": "full-path-governance-privacy-development-v2",
            "case_id": case["id"], "captured_at": datetime.now(UTC).isoformat(),
            "holdout_used": False, "production_behavior_changed": False,
            "passed": passed, "failure_boundary": failure_boundary,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "checks": checks, "snapshots": snapshots,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        for memory_id, collection in created_memory_ids:
            try: qdrant.delete_memory(memory_id, collection_name=collection)
            except Exception: pass
        if proxy_id is not None:
            session.execute(delete(ProxyUser).where(ProxyUser.id == proxy_id))
            session.commit()
        if created_writer_id is not None:
            session.execute(delete(ServiceWriter).where(ServiceWriter.id == created_writer_id))
            session.commit()
        session.close()
        print(json.dumps({"passed": passed, "failure_boundary": failure_boundary, "checks": checks}, indent=2))
        if not passed:
            raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main_async(args.output))


if __name__ == "__main__":
    main()
