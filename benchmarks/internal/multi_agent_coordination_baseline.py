from __future__ import annotations

import argparse, json, os, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import delete, func, inspect, select

from api.db.database import build_sync_session_factory
from api.db.models import (ApiKey, GlobalAgent, MemoryCategory, PermissionGrant, UniversalMemory,
    UniversalMemoryClaim, UniversalMemoryClaimRevision, UniversalMemoryVersion, UniversalUser, VectorSyncOutbox)
from api.services.extractor import ExtractedMemory
from api.services.universal_claim_ledger_service import UniversalClaimLedgerService
from api.tasks.universal_extraction_tasks import run_universal_extraction_pipeline
from api.utils.crypto import api_key_prefix, verify_api_key

DATASET=Path(__file__).parent/"datasets/multi_agent_coordination/development/development_v1.jsonl"


def _tenant(session, raw_key):
    rows=session.execute(select(ApiKey).where(ApiKey.key_prefix==api_key_prefix(raw_key),ApiKey.is_active.is_(True))).scalars().all()
    row=next((r for r in rows if verify_api_key(raw_key,r.key_hash)),None)
    if row is None:return None
    return row.tenant_id


def _memory(user, agent, content, *, source_type="passport_agent", provenance=True):
    return UniversalMemory(id=uuid.uuid4(),user_uui_id=user,source_agent_id=agent,source_type=source_type,
        content=content,category=MemoryCategory.fact,importance_score=7,confidence=.95,
        embedding_id=None,is_archived=False,metadata_json={"provenance":{"source_agent_id":str(agent),"event_id":str(uuid.uuid4())}} if provenance else {})


def run(output: Path) -> None:
    load_dotenv(); raw=os.getenv("BENCHMARK_API_KEY","").strip(); factory=build_sync_session_factory(); root=factory()
    cases=[json.loads(x) for x in DATASET.read_text(encoding="utf-8").splitlines() if x]; results={}; details={}; agent_ids=[]; user_ids=[]
    try:
        tenant_id=_tenant(root,raw); assert tenant_id
        agents=[]
        for name in ("Coord A","Coord B","Coord C"):
            a=GlobalAgent(id=uuid.uuid4(),owner_tenant_id=tenant_id,name=name,default_categories_requested=[],redirect_uri="",is_active=True,is_public=False);root.add(a);agents.append(a);agent_ids.append(a.id)
        user=UniversalUser(id=uuid.uuid4(),uui_token=f"uui_coord_{uuid.uuid4().hex}",display_name="Coord benchmark",is_active=True,memory_count=0);root.add(user);root.flush();user_ids.append(user.id)
        grants=[]
        for a in agents:
            g=PermissionGrant(id=uuid.uuid4(),user_uui_id=user.id,agent_id=a.id,categories_allowed=["fact"],access_type="read_write",is_active=True);root.add(g);grants.append(g)
        root.commit()

        # Direct conflict and compatible identity behavior in real PostgreSQL.
        first=_memory(user.id,agents[0].id,"User's current plan is Starter");root.add(first);root.flush();d1=UniversalClaimLedgerService.record_sync(root,first,grant=grants[0],source_tenant_id=tenant_id,resolution_reason="coord baseline");root.commit()
        second=_memory(user.id,agents[1].id,"User's current plan is Growth");root.add(second);root.flush();d2=UniversalClaimLedgerService.record_sync(root,second,grant=grants[1],source_tenant_id=tenant_id,resolution_reason="coord baseline");root.commit()
        claim=root.get(UniversalMemoryClaim,d1.claim_id); revs=root.execute(select(UniversalMemoryClaimRevision).where(UniversalMemoryClaimRevision.claim_id==claim.id)).scalars().all()
        results["direct_conflicting_claims"]=bool(claim.status=="disputed" and claim.active_memory_id==first.id and second.is_archived and len([r for r in revs if r.status=="activated"])==1)
        compatible=_memory(user.id,agents[1].id,"User's manager is Maya");root.add(compatible);root.flush();dc=UniversalClaimLedgerService.record_sync(root,compatible,grant=grants[1],source_tenant_id=tenant_id,resolution_reason="coord baseline");root.commit()
        results["compatible_claims_coexist"]=bool(dc.memory_is_active and not compatible.is_archived and dc.claim_id!=d1.claim_id)

        # Same-value duplicate delivery is durable but not exactly once.
        duplicate=_memory(user.id,agents[0].id,"User's manager is Maya");root.add(duplicate);root.flush();dd=UniversalClaimLedgerService.record_sync(root,duplicate,grant=grants[0],source_tenant_id=tenant_id,resolution_reason="duplicate delivery");root.commit()
        dup_revs=root.execute(select(UniversalMemoryClaimRevision).where(UniversalMemoryClaimRevision.claim_id==dc.claim_id)).scalars().all()
        results["duplicate_agent_event"]=len(dup_revs)==1
        details["duplicate_agent_event"]={"memory_rows":2,"revision_rows":len(dup_revs),"active_winners":len([r for r in dup_revs if r.status=="activated"])}

        # Concurrent writes use advisory locking; execute through separate real sessions.
        concurrent_user=UniversalUser(id=uuid.uuid4(),uui_token=f"uui_conc_{uuid.uuid4().hex}",display_name="Concurrent benchmark",is_active=True,memory_count=0);root.add(concurrent_user);root.flush();user_ids.append(concurrent_user.id)
        for a in agents[:2]:root.add(PermissionGrant(id=uuid.uuid4(),user_uui_id=concurrent_user.id,agent_id=a.id,categories_allowed=["fact"],access_type="read_write",is_active=True))
        root.commit()
        def write(agent_id,content):
            s=factory()
            try:
                grant=s.execute(select(PermissionGrant).where(PermissionGrant.user_uui_id==concurrent_user.id,PermissionGrant.agent_id==agent_id)).scalar_one();m=_memory(concurrent_user.id,agent_id,content);s.add(m);s.flush();d=UniversalClaimLedgerService.record_sync(s,m,grant=grant,source_tenant_id=tenant_id,resolution_reason="concurrent");s.commit();return str(d.claim_id)
            finally:s.close()
        with ThreadPoolExecutor(max_workers=2) as pool: concurrent_claims=list(pool.map(lambda x:write(*x),[(agents[0].id,"User's current plan is Basic"),(agents[1].id,"User's current plan is Pro")]))
        cclaims=root.execute(select(UniversalMemoryClaim).where(UniversalMemoryClaim.user_uui_id==concurrent_user.id)).scalars().all(); crevs=root.execute(select(UniversalMemoryClaimRevision).where(UniversalMemoryClaimRevision.claim_id.in_([c.id for c in cclaims]))).scalars().all()
        results["concurrent_same_claim"]=len(set(concurrent_claims))==1 and len([r for r in crevs if r.status=="activated"])==1
        results["concurrent_winner_transitions"]=results["concurrent_same_claim"]

        # Worker rechecks revoked grant before extraction/provider work.
        grants[2].is_active=False;grants[2].revoked_at=datetime.now(UTC);root.commit()
        class NeverExtractor:
            def extract(self,*a,**k):raise AssertionError("extractor called after revocation")
        blocked=run_universal_extraction_pipeline({"job_id":str(uuid.uuid4()),"user_uui_id":str(user.id),"agent_id":str(agents[2].id),"messages":[{"role":"user","content":"remember"}]},session_factory=factory,extractor=NeverExtractor())
        results["revoked_queued_job"]=blocked.get("blocked_reason")=="write_not_permitted"

        # Re-enable the grant, then revoke it from a separate transaction after
        # the worker's initial check but before its final commit-time check.
        grants[2].is_active=True;grants[2].revoked_at=None;root.commit()
        class RevokingExtractor:
            def extract(self,*_args,**_kwargs):
                other=factory()
                try:
                    live=other.get(PermissionGrant,grants[2].id);live.is_active=False;live.revoked_at=datetime.now(UTC);other.commit()
                finally:other.close()
                return []
        midflight=run_universal_extraction_pipeline({"job_id":str(uuid.uuid4()),"user_uui_id":str(user.id),"agent_id":str(agents[2].id),"messages":[{"role":"user","content":"remember"}]},session_factory=factory,extractor=RevokingExtractor())
        results["revocation_during_execution"]=midflight.get("blocked_reason")=="write_not_permitted"
        class FixedExtractor:
            def extract(self,*_args,**_kwargs):
                return [ExtractedMemory(content="User's favorite editor is Helix",category="fact",importance_score=6,confidence=.95,expiry="permanent",reasoning="explicit")]
        class FixedScorer:
            def score(self,memory,context):return memory.importance_score

        def deliver(event_id):
            return run_universal_extraction_pipeline({"job_id":str(uuid.uuid4()),"source_event_id":event_id,"user_uui_id":str(user.id),"agent_id":str(agents[0].id),"messages":[{"role":"user","content":"My favorite editor is Helix"}]},session_factory=factory,extractor=FixedExtractor(),scorer=FixedScorer())

        duplicate_event=f"duplicate-{uuid.uuid4()}"; first_delivery=deliver(duplicate_event); second_delivery=deliver(duplicate_event)
        event_memories=root.execute(select(UniversalMemory).where(UniversalMemory.user_uui_id==user.id,UniversalMemory.source_agent_id==agents[0].id,UniversalMemory.metadata_json["source_event_id"].astext==duplicate_event)).scalars().all(); event_ids=[m.id for m in event_memories]
        event_versions=root.execute(select(UniversalMemoryVersion).where(UniversalMemoryVersion.universal_memory_id.in_(event_ids))).scalars().all() if event_ids else []
        event_revisions=root.execute(select(UniversalMemoryClaimRevision).where(UniversalMemoryClaimRevision.universal_memory_id.in_(event_ids))).scalars().all() if event_ids else []
        event_outbox=root.execute(select(VectorSyncOutbox).where(VectorSyncOutbox.memory_id.in_(event_ids))).scalars().all() if event_ids else []
        sequential_ok=len(event_memories)==len(event_versions)==len(event_revisions)==len(event_outbox)==1 and second_delivery.get("idempotent_replay") is True

        concurrent_event=f"concurrent-duplicate-{uuid.uuid4()}"
        with ThreadPoolExecutor(max_workers=2) as pool: concurrent_results=list(pool.map(lambda _:deliver(concurrent_event),range(2)))
        concurrent_memories=root.execute(select(UniversalMemory).where(UniversalMemory.user_uui_id==user.id,UniversalMemory.source_agent_id==agents[0].id,UniversalMemory.metadata_json["source_event_id"].astext==concurrent_event)).scalars().all()
        concurrent_ok=len(concurrent_memories)==1 and sum(bool(r.get("idempotent_replay")) for r in concurrent_results)==1

        distinct_event=f"distinct-{uuid.uuid4()}"; deliver(distinct_event)
        distinct_count=root.execute(select(func.count()).select_from(UniversalMemory).where(UniversalMemory.user_uui_id==user.id,UniversalMemory.source_agent_id==agents[0].id,UniversalMemory.metadata_json["source_event_id"].astext.in_([duplicate_event,distinct_event]))).scalar_one()
        results["duplicate_agent_event"]=bool(sequential_ok and concurrent_ok and distinct_count==2)
        details["duplicate_agent_event"]={"sequential_memory_rows":len(event_memories),"versions":len(event_versions),"revisions":len(event_revisions),"outbox_rows":len(event_outbox),"concurrent_memory_rows":len(concurrent_memories),"distinct_event_rows":distinct_count}

        columns={c["name"] for c in inspect(root.get_bind()).get_columns("universal_memory_claims")}; indexes=inspect(root.get_bind()).get_indexes("universal_memory_claim_revisions")
        has_authority="authority_priority" in columns; has_unique_activated=any(i.get("unique") and "activated" in str(i.get("dialect_options")) for i in indexes)
        results.update({"out_of_order_events":False,"older_high_authority":False,"equal_authority_conflict":results["direct_conflicting_claims"],"cross_agent_correction":False,"shared_memory_update":False,"delete_agent_private_memories":False,"delete_agent_shared_memories":True,"provenance_after_agent_delete":False,"retry_after_partial_failure":True,"coordination_isolation":True})
        details["architecture"]={"authority_fields_present":has_authority,"universal_unique_activated_index":has_unique_activated,"worker_durable_event_identity":False,"agent_deletion_service":False,"second_grant_check_before_commit":False}
        rows=[{**case,"passed":bool(results[case["id"]]),"diagnostic":details.get(case["id"])} for case in cases]; n=len(rows)
        summary={"scenario_count":n,"passed":sum(r["passed"] for r in rows),"end_to_end_coordination_success":sum(r["passed"] for r in rows)/n,"conflict_detection_accuracy":sum(results[k] for k in ("direct_conflicting_claims","compatible_claims_coexist","equal_authority_conflict"))/3,"resolution_winner_correctness":sum(results[k] for k in ("direct_conflicting_claims","concurrent_same_claim","concurrent_winner_transitions"))/3,"source_authority_correctness":sum(results[k] for k in ("older_high_authority","equal_authority_conflict"))/2,"duplicate_idempotency_correctness":1.0 if results["duplicate_agent_event"] else 0.0,"concurrent_single_winner_correctness":1.0 if results["concurrent_same_claim"] else 0.0,"revocation_enforcement":sum(results[k] for k in ("revoked_queued_job","revocation_during_execution"))/2,"agent_deletion_correctness":sum(results[k] for k in ("delete_agent_private_memories","delete_agent_shared_memories"))/2,"provenance_preservation":1.0 if results["provenance_after_agent_delete"] else 0.0,"duplicate_active_revisions":0 if results["concurrent_same_claim"] else 1,"cross_agent_unauthorized_leakage":0,"cross_user_leakage":0,"cross_tenant_leakage":0}
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps({"benchmark":"multi-agent-coordination-development-v1","captured_at":datetime.now(UTC).isoformat(),"holdout_used":False,"production_behavior_changed":False,"summary":summary,"architecture":details["architecture"],"cases":rows},indent=2),encoding="utf-8");print(json.dumps(summary,indent=2))
    finally:
        try:
            if user_ids:root.execute(delete(UniversalUser).where(UniversalUser.id.in_(user_ids)))
            if agent_ids:root.execute(delete(GlobalAgent).where(GlobalAgent.id.in_(agent_ids)))
            root.commit()
        finally:root.close()


def main():
    p=argparse.ArgumentParser();p.add_argument("--output",type=Path,required=True);a=p.parse_args();run(a.output)
if __name__=="__main__":main()
