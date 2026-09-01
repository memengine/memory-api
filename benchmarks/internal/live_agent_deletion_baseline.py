from __future__ import annotations

import argparse, json, os, secrets, uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from sqlalchemy import delete, select

from api.db.database import build_sync_session_factory
from api.db.models import (AgentApiKey, ApiKey, GlobalAgent, MemoryCategory, PermissionGrant,
    UniversalMemory, UniversalMemoryClaim, UniversalMemoryClaimRevision, UniversalMemoryVersion,
    UniversalUser)
from api.db.vector_store import QdrantService
from api.services.embedding_service import EmbeddingService
from api.services.universal_claim_ledger_service import UniversalClaimLedgerService
from api.services.version_service import VersionService
from api.tasks.universal_extraction_tasks import run_universal_extraction_pipeline
from api.utils.crypto import api_key_prefix, fingerprint_api_key, hash_api_key, verify_api_key

DATASET=Path(__file__).parent/"datasets/agent_deletion/development/development_v1.jsonl"


def _tenant(session,raw):
    rows=session.execute(select(ApiKey).where(ApiKey.key_prefix==api_key_prefix(raw),ApiKey.is_active.is_(True))).scalars().all()
    row=next((r for r in rows if verify_api_key(raw,r.key_hash)),None)
    if row is None:raise RuntimeError("BENCHMARK_API_KEY is invalid")
    return row.tenant_id


def _agent(session,tenant_id,name):
    raw=f"agent_sk_{secrets.token_hex(32)}"; agent=GlobalAgent(id=uuid.uuid4(),owner_tenant_id=tenant_id,name=name,default_categories_requested=[],redirect_uri="",is_active=True,is_public=False)
    key=AgentApiKey(id=uuid.uuid4(),global_agent=agent,key_hash=hash_api_key(fingerprint_api_key(raw)),key_prefix=raw[:12],name="deletion baseline",is_active=True);session.add_all([agent,key]);return agent,raw


def _memory(user,agent,content,category="fact",archived=False,snapshot=True,event=None,source_type="passport_agent"):
    event=event or str(uuid.uuid4()); provenance={"source_agent_id":str(agent),"event_id":event} if snapshot else None
    return UniversalMemory(id=uuid.uuid4(),user_uui_id=user,source_agent_id=agent,source_type=source_type,content=content,category=MemoryCategory(category),importance_score=7,confidence=.95,embedding_id=None,is_archived=archived,metadata_json={"source_event_id":event,**({"provenance":provenance} if provenance else {})})


async def run(output:Path):
    load_dotenv(); raw=os.getenv("BENCHMARK_API_KEY","").strip();factory=build_sync_session_factory();s=factory();q=QdrantService();embed=EmbeddingService(sync_session=s);points=[];users=[];agents=[]
    cases=[json.loads(x) for x in DATASET.read_text(encoding="utf-8").splitlines() if x];results={};snap={}
    try:
        tid=_tenant(s,raw);a,key_a=_agent(s,tid,"Delete Source A");b,key_b=_agent(s,tid,"Surviving Reader B");agents.extend([a.id,b.id])
        user=UniversalUser(id=uuid.uuid4(),uui_token=f"uui_delete_{uuid.uuid4().hex}",display_name="Agent deletion benchmark",is_active=True,memory_count=0);other=UniversalUser(id=uuid.uuid4(),uui_token=f"uui_other_{uuid.uuid4().hex}",display_name="Agent deletion other",is_active=True,memory_count=0);s.add_all([user,other]);s.flush();users.extend([user.id,other.id])
        ga=PermissionGrant(id=uuid.uuid4(),user_uui_id=user.id,agent_id=a.id,categories_allowed=["fact","preference"],access_type="read_write",is_active=True);gb=PermissionGrant(id=uuid.uuid4(),user_uui_id=user.id,agent_id=b.id,categories_allowed=["fact"],access_type="read_write",is_active=True);s.add_all([ga,gb]);s.flush()
        shared=_memory(user.id,a.id,"User works remotely",snapshot=True);private=_memory(user.id,a.id,"User prefers private paper notes",category="preference",snapshot=True);historical=_memory(user.id,a.id,"User's current plan is Starter",archived=False,snapshot=False);updated=_memory(user.id,b.id,"User's current plan is Growth",source_type="user_correction",snapshot=True);duplicate=_memory(user.id,a.id,"User uses Helix editor",snapshot=True,event="durable-duplicate-event")
        for m,g in ((shared,ga),(private,ga),(historical,ga)):
            s.add(m);s.flush();UniversalClaimLedgerService.record_sync(s,m,grant=g,source_tenant_id=tid,resolution_reason="deletion baseline");VersionService(s).record_universal_version_sync(m,"created","deletion baseline","agent",changed_by_agent_id=str(m.source_agent_id),db_session=s)
        # Build a resolved chain fixture using the real correction transition, then mark predecessor lifecycle.
        updated.user_uui_id=user.id;s.add(updated);s.flush();claim=s.execute(select(UniversalMemoryClaim).where(UniversalMemoryClaim.user_uui_id==user.id,UniversalMemoryClaim.active_memory_id==historical.id)).scalar_one()
        UniversalClaimLedgerService._record(s,updated,claim=claim,identity=UniversalClaimLedgerService._identity(updated),grant=gb,source_tenant_id=tid,resolution_reason="cross-agent update before deletion")
        historical.is_archived=True
        oldrev=s.execute(select(UniversalMemoryClaimRevision).where(UniversalMemoryClaimRevision.universal_memory_id==historical.id)).scalar_one();oldrev.status="superseded"
        VersionService(s).record_universal_version_sync(updated,"created","updated before deletion","agent",changed_by_agent_id=str(b.id),db_session=s)
        s.add(duplicate);s.flush();UniversalClaimLedgerService.record_sync(s,duplicate,grant=ga,source_tenant_id=tid,resolution_reason="deduplicated delivery");VersionService(s).record_universal_version_sync(duplicate,"created","one delivery","agent",changed_by_agent_id=str(a.id),db_session=s)
        other_mem=_memory(other.id,b.id,"Other user's secret",snapshot=True);s.add(other_mem);s.commit()
        active=[shared,private,updated,duplicate,other_mem]
        for m in active:
            e=embed.embed_sync(m.content,tenant_id=str(tid));payload={"memory_id":str(m.id),"user_uui_id":str(m.user_uui_id),"source_agent_id":str(m.source_agent_id),"category":m.category.value,"importance_score":m.importance_score,"is_archived":False,"created_at":datetime.now(UTC).isoformat(),"qdrant_collection":"universal_memories"};q.upsert_memory(str(m.id),e.vector,payload,collection_name="universal_memories",vector_size=e.dimensions);points.append(str(m.id))

        # Exercise the governed tenant API retirement path.
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000",timeout=60) as retire_client:
            retire_response=await retire_client.post(f"/v1/agents/global/{a.id}/retire",headers={"Authorization":f"ApiKey {raw}"})
        if retire_response.status_code != 200: raise RuntimeError(f"retirement failed: {retire_response.status_code} {retire_response.text}")
        s.expire_all();mems={m.id:s.get(UniversalMemory,m.id) for m in (shared,private,historical,updated,duplicate)}
        revisions=s.execute(select(UniversalMemoryClaimRevision).where(UniversalMemoryClaimRevision.universal_memory_id.in_([shared.id,private.id,historical.id,updated.id,duplicate.id]))).scalars().all();versions=s.execute(select(UniversalMemoryVersion).where(UniversalMemoryVersion.universal_memory_id.in_([shared.id,private.id,historical.id,updated.id,duplicate.id]))).scalars().all();claims=s.execute(select(UniversalMemoryClaim).where(UniversalMemoryClaim.user_uui_id==user.id)).scalars().all()
        source_grant_count=len(s.execute(select(PermissionGrant).where(PermissionGrant.agent_id==a.id,PermissionGrant.is_active.is_(True))).scalars().all());reader_grant=s.execute(select(PermissionGrant).where(PermissionGrant.id==gb.id)).scalar_one_or_none()
        qpoints=q.client.retrieve(collection_name="universal_memories",ids=[str(shared.id),str(private.id),str(updated.id),str(duplicate.id)],with_payload=True,with_vectors=False);tombstone=s.get(GlobalAgent,a.id);stale_payload=sum(str((p.payload or {}).get("source_agent_id"))==str(a.id) and tombstone is None for p in qpoints)

        class NeverExtractor:
            def extract(self,*args,**kwargs):raise AssertionError("deleted agent reached extraction")
        queued=run_universal_extraction_pipeline({"job_id":str(uuid.uuid4()),"user_uui_id":str(user.id),"agent_id":str(a.id),"messages":[{"role":"user","content":"remember"}]},session_factory=factory,extractor=NeverExtractor())
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000",timeout=60) as client:
            response=await client.post("/v1/universal/memories/retrieve",headers={"Authorization":f"ApiKey {key_b}","X-MemoryOS-UUI":user.uui_token},json={"query":"work plan editor", "limit":10});data=response.json().get("data",[]) if response.status_code==200 else [];returned={x["id"]:x for x in data}
        shared_visible=str(shared.id) in returned;other_leak=str(other_mem.id) in returned;private_visible=str(private.id) in returned
        snapshot_visible=bool((returned.get(str(shared.id),{}).get("provenance") or {}).get("source_agent_id"))
        plan_claim=next(c for c in claims if c.active_memory_id==updated.id)
        results.update({
            "private_only_agent_delete":mems[private.id] is not None and mems[private.id].source_agent_id==a.id and not private_visible,
            "shared_memory_continuity":shared_visible and not private_visible,
            "delete_after_cross_agent_update":plan_claim.active_memory_id==updated.id and mems[historical.id].is_archived,
            "delete_current_winner_source":any(c.active_memory_id==shared.id for c in claims) and mems[shared.id].source_agent_id==a.id,
            "delete_historical_source":mems[historical.id].source_agent_id==a.id and oldrev.status=="superseded",
            "revoke_before_delete":source_grant_count==0 and reader_grant is not None,
            "delete_with_queued_work":queued.get("blocked_reason")=="write_not_permitted",
            "delete_after_duplicate_retry":mems[duplicate.id] is not None and len([r for r in revisions if r.universal_memory_id==duplicate.id])==1,
            "provenance_after_delete":snapshot_visible and all(v.changed_by_agent_id is not None for v in versions),
            "qdrant_after_delete":stale_payload==0,
            "deletion_isolation":not other_leak,
        })
        # Privacy control: user deletion removes vectors and database state, unlike source deletion.
        privacy=UniversalUser(id=uuid.uuid4(),uui_token=f"uui_priv_{uuid.uuid4().hex}",display_name="Privacy control",is_active=True,memory_count=1);s.add(privacy);s.flush();users.append(privacy.id);pm=_memory(privacy.id,b.id,"Privacy control memory",snapshot=True);s.add(pm);s.commit();pe=embed.embed_sync(pm.content,tenant_id=str(tid));q.upsert_memory(str(pm.id),pe.vector,{"memory_id":str(pm.id),"user_uui_id":str(privacy.id),"source_agent_id":str(b.id),"category":"fact","is_archived":False},collection_name="universal_memories",vector_size=pe.dimensions);points.append(str(pm.id));deleted_vectors=q.delete_universal_user_memories(str(privacy.id),collection_name="universal_memories");s.delete(privacy);s.commit();users.remove(privacy.id)
        results["privacy_delete_distinct"]=deleted_vectors==1 and s.get(UniversalUser,privacy.id) is None
        snap={"memory_rows_surviving":sum(m is not None for m in mems.values()),"source_agent_ids_nulled":sum(m.source_agent_id is None for m in mems.values()),"agent_tombstone_preserved":tombstone is not None and not tombstone.is_active,"claim_count":len(claims),"revision_count":len(revisions),"revision_source_ids_nulled":sum(r.source_agent_id is None for r in revisions),"version_source_ids_nulled":sum(v.changed_by_agent_id is None for v in versions),"active_source_grants_remaining":source_grant_count,"reader_grant_survives":reader_grant is not None,"qdrant_points_retained":len(qpoints),"qdrant_stale_source_payloads":stale_payload,"api_shared_visible":shared_visible,"api_source_snapshot_visible":snapshot_visible}
        rows=[{**case,"passed":bool(results[case["id"]])} for case in cases];n=len(rows);summary={"scenario_count":n,"passed":sum(r["passed"] for r in rows),"end_to_end_lifecycle_success":sum(r["passed"] for r in rows)/n,"private_memory_deletion_correctness":1.0 if results["private_only_agent_delete"] else 0.0,"shared_memory_continuity_correctness":1.0 if results["shared_memory_continuity"] else 0.0,"provenance_source_preservation":1.0 if results["provenance_after_delete"] else 0.0,"claim_chain_integrity":sum(results[k] for k in ("delete_after_cross_agent_update","delete_historical_source"))/2,"current_winner_correctness":1.0 if results["delete_current_winner_source"] else 0.0,"grant_cleanup_correctness":1.0 if results["revoke_before_delete"] else 0.0,"queued_work_revocation_correctness":1.0 if results["delete_with_queued_work"] else 0.0,"qdrant_cleanup_retention_correctness":1.0 if results["qdrant_after_delete"] else 0.0,"cross_agent_user_tenant_leakage":0 if results["deletion_isolation"] else 1,"privacy_deletion_correctness":1.0 if results["privacy_delete_distinct"] else 0.0}
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps({"benchmark":"originating-agent-retirement-development-v2","captured_at":datetime.now(UTC).isoformat(),"holdout_used":False,"production_behavior_changed":True,"summary":summary,"state_snapshot":snap,"cases":rows},indent=2),encoding="utf-8");print(json.dumps(summary,indent=2))
    finally:
        for pid in points:
            try:q.delete_memory(pid,collection_name="universal_memories")
            except Exception:pass
        try:
            if users:s.execute(delete(UniversalUser).where(UniversalUser.id.in_(users)))
            if agents:s.execute(delete(GlobalAgent).where(GlobalAgent.id.in_(agents)))
            s.commit()
        finally:s.close()


def main():
    p=argparse.ArgumentParser();p.add_argument("--output",type=Path,required=True);a=p.parse_args();import asyncio;asyncio.run(run(a.output))
if __name__=="__main__":main()
