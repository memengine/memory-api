from __future__ import annotations

import argparse, json, os, time, uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import delete, select

from api.db.database import build_sync_session_factory
from api.db.models import ApiKey, ExtractionJob, ExtractionJobStatus, Memory, MemoryClaimRevision, MemorySourceEvent, PlanTier, ProxyUser, ServiceWriter, Tenant, VectorSyncOutbox
from api.db.vector_store import QdrantService
from api.tasks.extraction_tasks import _redis_client
from api.tasks.watchdog_tasks import run_watchdog_cycle
from api.utils.crypto import api_key_prefix, verify_api_key


def _key_and_tenant(session):
    raw=(os.getenv("BENCHMARK_API_KEY") or "").strip()
    if not raw: raise RuntimeError("BENCHMARK_API_KEY required")
    rows=session.execute(select(ApiKey).where(ApiKey.is_active.is_(True),ApiKey.key_prefix==api_key_prefix(raw))).scalars().all()
    key=next((row for row in rows if verify_api_key(raw,row.key_hash)),None)
    if key is None: raise RuntimeError("benchmark key mismatch")
    return raw, key, session.get(Tenant,key.tenant_id)


def prepare(output: Path) -> None:
    session=build_sync_session_factory()(); raw,key,tenant=_key_and_tenant(session)
    original=tenant.plan_tier.value; run_id=uuid.uuid4().hex; external=f"celery-crash-{run_id[:12]}"; event=f"crash-{run_id}"
    artifact={"benchmark":"celery-crash-watchdog-development-v2","run_id":run_id,"external_user_id":external,"event_id":event,"tenant_id":str(tenant.id),"original_plan":original,"queue":None,"worker_service":"celery-starter","holdout_used":False,"production_behavior_changed":False,"timestamps":{"started":datetime.now(UTC).isoformat()}}
    try:
        active=session.execute(select(ExtractionJob).where(ExtractionJob.queue_name.in_(["free-extraction","starter-extraction"]),ExtractionJob.status==ExtractionJobStatus.processing)).scalars().all()
        if active: raise RuntimeError("free/starter worker has processing jobs")
        writer=session.execute(select(ServiceWriter).where(ServiceWriter.tenant_id==tenant.id,ServiceWriter.api_key_id==key.id,ServiceWriter.is_active.is_(True))).scalar_one_or_none()
        if writer is None:
            writer=ServiceWriter(tenant_id=tenant.id,api_key_id=key.id,service_key=f"celery-crash-{run_id[:12]}",display_name="Internal Celery crash benchmark",authority_rules={})
            session.add(writer); session.commit(); session.refresh(writer); artifact["created_writer_id"]=str(writer.id)
        source={"event_id":event,"observed_at":datetime.now(UTC).isoformat(),"scope":{"benchmark_run":run_id},"evidence":[{"source_type":"conversation","reference":f"crash-evidence-{run_id}"}]}
        if writer is not None: source["service"]=writer.service_key
        payload={"external_user_id":external,"messages":[{"role":"user","content":"My durable benchmark recovery preference is concise incident summaries with UTC timestamps. " + "This is one stable preference for crash recovery validation. "*5}],"metadata":{"internal_fault_experiment":run_id,"_internal_celery_crash_barrier":True},"source":source}
        response=httpx.post("http://api:8000/v1/memories/add",headers={"Authorization":f"ApiKey {raw}"},json=payload,timeout=30)
        if response.is_error:
            raise RuntimeError(f"memory add failed with HTTP {response.status_code}: {response.text}")
        job_id=response.json()["job_id"]
        artifact["job_id"]=job_id; deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            session.expire_all(); job=session.get(ExtractionJob,uuid.UUID(job_id))
            barrier_key=f"internal-benchmark:celery-crash-barrier:{job_id}"
            if job and job.status==ExtractionJobStatus.processing and _redis_client().get(barrier_key)=="armed":
                artifact["queue"]=job.queue_name; artifact["timestamps"]["processing_observed"]=datetime.now(UTC).isoformat(); artifact["celery_task_id"]=job.celery_task_id; artifact["attempts_before_crash"]=job.attempts; artifact["stale_after_before_crash"]=job.stale_after.isoformat() if job.stale_after else None; break
            if job and job.status==ExtractionJobStatus.completed:
                raise RuntimeError(f"job completed before interruption window queue={job.queue_name} metadata={(job.payload or {}).get('metadata')}")
            time.sleep(.02)
        else: raise TimeoutError("processing state not observed")
        output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(artifact,indent=2),encoding="utf-8")
        print(json.dumps({"job_id":job_id,"processing":True,"queue":artifact["queue"],"worker_service":artifact["worker_service"]}))
    except Exception:
        if artifact.get("created_writer_id"):
            created_writer=session.get(ServiceWriter,uuid.UUID(artifact["created_writer_id"]))
            if created_writer is not None: session.delete(created_writer)
        tenant.plan_tier=PlanTier(original); session.add(tenant); session.commit(); raise
    finally: session.close()


def recover(output: Path) -> None:
    artifact=json.loads(output.read_text(encoding="utf-8")); session_factory=build_sync_session_factory(); session=session_factory(); started=time.perf_counter()
    q=QdrantService(); point_locations=[]
    try:
        job=session.get(ExtractionJob,uuid.UUID(artifact["job_id"])); artifact["state_after_restart"]=job.status.value
        job.stale_after=datetime.now(UTC)-timedelta(seconds=1); session.add(job); session.commit()
        artifact["watchdog_result"]=run_watchdog_cycle(session_factory=session_factory); artifact["timestamps"]["watchdog_triggered"]=datetime.now(UTC).isoformat()
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            session.expire_all(); job=session.get(ExtractionJob,uuid.UUID(artifact["job_id"]))
            if job.status in {ExtractionJobStatus.completed,ExtractionJobStatus.dead,ExtractionJobStatus.failed}: break
            time.sleep(.25)
        artifact["terminal_status"]=job.status.value; artifact["attempts_after_recovery"]=job.attempts; artifact["timestamps"]["terminal"]=datetime.now(UTC).isoformat(); artifact["recovery_time_ms"]=round((time.perf_counter()-started)*1000,2)
        proxy=session.execute(select(ProxyUser).where(ProxyUser.external_user_id==artifact["external_user_id"])).scalar_one_or_none(); memories=[] if proxy is None else session.execute(select(Memory).where(Memory.proxy_user_id==proxy.id)).scalars().all(); mids=[m.id for m in memories]
        revisions=[] if not mids else session.execute(select(MemoryClaimRevision).where(MemoryClaimRevision.memory_id.in_(mids))).scalars().all(); outbox=[] if not mids else session.execute(select(VectorSyncOutbox).where(VectorSyncOutbox.memory_id.in_(mids))).scalars().all(); event=session.execute(select(MemorySourceEvent).where(MemorySourceEvent.tenant_id==uuid.UUID(artifact["tenant_id"]),MemorySourceEvent.source_event_id==artifact["event_id"])).scalar_one_or_none()
        points=[]
        for memory in memories:
            collection=session.execute(select(VectorSyncOutbox.payload["qdrant_collection"].astext).where(VectorSyncOutbox.memory_id==memory.id).order_by(VectorSyncOutbox.created_at.desc()).limit(1)).scalar_one_or_none() or q.COLLECTION_NAME
            retrieved=[]; qdrant_deadline=time.monotonic()+30
            while time.monotonic()<qdrant_deadline:
                try:
                    retrieved=q.client.retrieve(collection_name=collection,ids=[str(memory.id)],with_payload=True,with_vectors=False)
                    artifact.pop("qdrant_measurement_error",None); break
                except Exception as exc:
                    artifact["qdrant_measurement_error"]=f"{type(exc).__name__}: {exc}"; time.sleep(1)
            points.extend(retrieved)
            if retrieved: point_locations.append((str(memory.id),collection))
        artifact["measurements"]={"memory_rows":len(memories),"claim_revision_rows":len(revisions),"activated_revisions":sum(r.status=="activated" for r in revisions),"outbox_rows":len(outbox),"qdrant_points":len(points),"source_event_rows":1 if event else 0,"provenance_preserved":bool(memories) and all(m.source_event_id==event.id and (m.metadata_json or {}).get("provenance") for m in memories)}
        artifact["checks"]={"watchdog_requeued_once":artifact["watchdog_result"].get("requeued")==1,"job_completed":job.status==ExtractionJobStatus.completed,"single_logical_memory":len(memories)==1,"single_claim_revision":len(revisions)==1,"single_winner":sum(r.status=="activated" for r in revisions)==1,"single_source_event":event is not None,"single_vector_point":len(points)==1,"provenance_preserved":artifact["measurements"]["provenance_preserved"]}
        artifact["passed"]=all(artifact["checks"].values()); output.write_text(json.dumps(artifact,indent=2,default=str),encoding="utf-8"); print(json.dumps({"passed":artifact["passed"],"checks":artifact["checks"],"measurements":artifact["measurements"],"recovery_time_ms":artifact["recovery_time_ms"]},indent=2))
    finally:
        _redis_client().delete(f"internal-benchmark:celery-crash-barrier:{artifact['job_id']}",f"internal-benchmark:celery-crash-barrier:{artifact['job_id']}:release")
        for memory_id,collection in point_locations:
            try: q.delete_memory(memory_id,collection_name=collection)
            except Exception: pass
        tenant=session.get(Tenant,uuid.UUID(artifact["tenant_id"])); tenant.plan_tier=PlanTier(artifact["original_plan"]); session.add(tenant)
        proxy=session.execute(select(ProxyUser).where(ProxyUser.external_user_id==artifact["external_user_id"])).scalar_one_or_none()
        if proxy is not None: session.execute(delete(ProxyUser).where(ProxyUser.id==proxy.id))
        if artifact.get("created_writer_id"):
            created_writer=session.get(ServiceWriter,uuid.UUID(artifact["created_writer_id"]))
            if created_writer is not None: session.delete(created_writer)
        session.commit(); session.close()


def cleanup(output: Path) -> None:
    artifact=json.loads(output.read_text(encoding="utf-8")); session=build_sync_session_factory()(); q=QdrantService()
    try:
        _redis_client().delete(f"internal-benchmark:celery-crash-barrier:{artifact['job_id']}",f"internal-benchmark:celery-crash-barrier:{artifact['job_id']}:release")
        proxy=session.execute(select(ProxyUser).where(ProxyUser.external_user_id==artifact["external_user_id"])).scalar_one_or_none()
        memories=[] if proxy is None else session.execute(select(Memory).where(Memory.proxy_user_id==proxy.id)).scalars().all()
        for memory in memories:
            collection=session.execute(select(VectorSyncOutbox.payload["qdrant_collection"].astext).where(VectorSyncOutbox.memory_id==memory.id).order_by(VectorSyncOutbox.created_at.desc()).limit(1)).scalar_one_or_none() or q.COLLECTION_NAME
            try: q.delete_memory(str(memory.id),collection_name=collection)
            except Exception: pass
        if proxy is not None: session.execute(delete(ProxyUser).where(ProxyUser.id==proxy.id))
        if artifact.get("created_writer_id"):
            session.execute(delete(ServiceWriter).where(ServiceWriter.id==uuid.UUID(artifact["created_writer_id"])))
        tenant=session.get(Tenant,uuid.UUID(artifact["tenant_id"])); tenant.plan_tier=PlanTier(artifact["original_plan"]); session.add(tenant); session.commit()
        print(json.dumps({"cleaned":True,"memory_rows":len(memories),"plan_restored":artifact["original_plan"]}))
    finally: session.close()


def main():
    p=argparse.ArgumentParser(); p.add_argument("mode",choices=["prepare","recover","cleanup"]); p.add_argument("--output",type=Path,required=True); a=p.parse_args()
    if a.mode=="prepare": prepare(a.output)
    elif a.mode=="recover": recover(a.output)
    else: cleanup(a.output)


if __name__=="__main__": main()
