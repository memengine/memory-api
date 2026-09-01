from __future__ import annotations

import argparse, asyncio, hashlib, json, os, secrets, time, uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from sqlalchemy import delete, select

from api.db.database import build_sync_session_factory
from api.db.models import (Agent, AgentApiKey, AgentMemoryScope, ApiKey, Conversation,
    EmbeddingModel, GlobalAgent, Memory, MemoryCategory, PermissionGrant, PlanTier,
    ProxyUser, Tenant, UniversalMemory, UniversalUser, User)
from api.db.vector_store import QdrantService
from api.services.embedding_service import EmbeddingService
from api.services.vector_outbox import build_vector_payload
from api.utils.crypto import api_key_prefix, fingerprint_api_key, hash_api_key, verify_api_key

DATASET = Path(__file__).parent / "datasets/multi_agent/development/development_v1.jsonl"


def _tenant_for_key(session: Any, raw_key: str):
    rows = session.execute(select(ApiKey).where(ApiKey.is_active.is_(True), ApiKey.key_prefix == api_key_prefix(raw_key))).scalars().all()
    row = next((item for item in rows if verify_api_key(raw_key, item.key_hash)), None)
    if row is None or row.tenant_id is None:
        raise RuntimeError("BENCHMARK_API_KEY does not match an active tenant key")
    return row.tenant_id


async def run(output: Path) -> None:
    load_dotenv(); tenant_key=os.getenv("BENCHMARK_API_KEY", "").strip()
    if not tenant_key: raise RuntimeError("BENCHMARK_API_KEY is required")
    frozen=[json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line]
    session=build_sync_session_factory()(); qdrant=QdrantService(); embedder=EmbeddingService(sync_session=session)
    run_id=uuid.uuid4().hex[:10]; point_ids=[]; created_tenants=[]; created_users=[]; created_uui=[]; rows=[]
    try:
        tenant_id=_tenant_for_key(session, tenant_key)
        model=session.execute(select(EmbeddingModel).where(EmbeddingModel.is_active.is_(True)).limit(1)).scalar_one()

        # Tenant plane: same external ID in two tenants, two local agents, real PostgreSQL/Qdrant/API.
        tenant_b=Tenant(id=uuid.uuid4(), company_name=f"MA benchmark {run_id}", region_id="IN1", plan_tier=PlanTier.starter, metadata_json={"benchmark":run_id})
        tenant_b_key=f"mos_bench_{secrets.token_hex(20)}"; tenant_b_api=ApiKey(id=uuid.uuid4(),tenant=tenant_b,key_hash=hash_api_key(tenant_b_key),key_prefix=api_key_prefix(tenant_b_key),name="benchmark",permissions=["read","write"],is_active=True)
        session.add_all([tenant_b,tenant_b_api]);session.flush();created_tenants.append(tenant_b.id)
        local_user=User(id=uuid.uuid4(),external_id=f"ma::{run_id}",email=f"ma-{run_id}@benchmark.test",settings={},memory_count=0,is_active=True)
        agent_a=Agent(id=uuid.uuid4(),user=local_user,name="Local A",memory_scope=AgentMemoryScope.private)
        agent_b=Agent(id=uuid.uuid4(),user=local_user,name="Local B",memory_scope=AgentMemoryScope.shared)
        session.add_all([local_user,agent_a,agent_b]);session.flush();created_users.append(local_user.id)
        external=f"ma-{run_id}"; proxies=[]; keymaps={}
        for tid,key,label in ((tenant_id,tenant_key,"primary"),(tenant_b.id,tenant_b_key,"other_tenant")):
            proxy=ProxyUser(id=uuid.uuid4(),tenant_id=tid,external_user_id=external,external_user_id_hash=hashlib.sha256(f"{tid}:{external}".encode()).hexdigest(),memory_count=0,metadata_json={"benchmark":run_id},is_blocked=False)
            session.add(proxy);session.flush();proxies.append(proxy)
            conversation=Conversation(id=uuid.uuid4(),user_id=local_user.id,message_count=1);session.add(conversation);session.flush()
            specs=[("local-a","User prefers concise answers.",agent_a.id),("local-b","User has a weekly planning ritual.",agent_b.id)] if label=="primary" else [("other-tenant","Secret from another tenant.",agent_a.id)]
            for logical,content,aid in specs:
                memory=Memory(id=uuid.uuid4(),user_id=local_user.id,proxy_user_id=proxy.id,agent_id=aid,content=content,category=MemoryCategory.fact,importance_score=7,confidence_score=.95,embedding_id=str(uuid.uuid4()),embedding_model_id=model.id,source_conversation_id=conversation.id,metadata_json={"provenance":{"source_agent_id":str(aid),"event_id":f"{run_id}-{logical}"}},is_archived=False)
                session.add(memory);session.flush(); emb=embedder.embed_sync(content,model_id=model.id,tenant_id=str(tid))
                payload=build_vector_payload(memory,tenant_id=str(tid),proxy_user_id=str(proxy.id),user_id=str(local_user.id),embedding_model_id=model.id,qdrant_collection=model.qdrant_collection)
                qdrant.upsert_memory(str(memory.id),emb.vector,payload,collection_name=model.qdrant_collection,vector_size=model.dimensions);point_ids.append((str(memory.id),model.qdrant_collection));keymaps[str(memory.id)]=logical
            proxy.memory_count=len(specs)

        # Passport plane: two users, three agents, active/category/read-only/revoked grants.
        def global_agent(name: str):
            raw=f"agent_sk_{secrets.token_hex(32)}"; agent=GlobalAgent(id=uuid.uuid4(),owner_tenant_id=tenant_id,name=name,default_categories_requested=[],redirect_uri="",is_active=True,is_public=False)
            key=AgentApiKey(id=uuid.uuid4(),global_agent=agent,key_hash=hash_api_key(fingerprint_api_key(raw)),key_prefix=raw[:12],name="benchmark",is_active=True);session.add_all([agent,key]);return agent,raw
        ga,ga_key=global_agent("Global A"); gb,gb_key=global_agent("Global B"); gr,gr_key=global_agent("Revoked")
        u1=UniversalUser(id=uuid.uuid4(),uui_token=f"uui_{secrets.token_hex(24)}",display_name="Benchmark One",is_active=True,memory_count=0)
        u2=UniversalUser(id=uuid.uuid4(),uui_token=f"uui_{secrets.token_hex(24)}",display_name="Benchmark Two",is_active=True,memory_count=0)
        session.add_all([u1,u2]);session.flush();created_uui.extend([u1.id,u2.id])
        grants=[PermissionGrant(id=uuid.uuid4(),user_uui_id=u1.id,agent_id=ga.id,categories_allowed=["fact"],access_type="read_write",is_active=True),PermissionGrant(id=uuid.uuid4(),user_uui_id=u1.id,agent_id=gb.id,categories_allowed=["relationship"],access_type="read_only",is_active=True),PermissionGrant(id=uuid.uuid4(),user_uui_id=u1.id,agent_id=gr.id,categories_allowed=["fact"],access_type="read_write",is_active=False,revoked_at=datetime.now(UTC)),PermissionGrant(id=uuid.uuid4(),user_uui_id=u2.id,agent_id=ga.id,categories_allowed=["fact"],access_type="read_write",is_active=True)]
        session.add_all(grants);session.flush(); universal_keys={}
        uspecs=[("ua-fact","User lives in Pune.",u1,ga,"fact"),("ub-fact","User works remotely.",u1,gb,"fact"),("ub-rel","User's manager is Maya.",u1,gb,"relationship"),("u2-fact","Other user lives in Delhi.",u2,ga,"fact")]
        for logical,content,user,source,category in uspecs:
            mid=uuid.uuid4(); memory=UniversalMemory(id=mid,user_uui_id=user.id,source_agent_id=source.id,source_type="passport_agent",content=content,category=category,importance_score=7,confidence=.95,embedding_id=str(mid),metadata_json={"provenance":{"source_agent_id":str(source.id),"event_id":f"{run_id}-{logical}"}},is_archived=False)
            session.add(memory);session.flush();emb=embedder.embed_sync(content,model_id=model.id,tenant_id=str(tenant_id));payload={"memory_id":str(mid),"user_uui_id":str(user.id),"source_agent_id":str(source.id),"category":category,"importance_score":7,"is_archived":False,"created_at":datetime.now(UTC).isoformat(),"qdrant_collection":"universal_memories"}
            qdrant.upsert_memory(str(mid),emb.vector,payload,collection_name="universal_memories",vector_size=model.dimensions);point_ids.append((str(mid),"universal_memories"));universal_keys[str(mid)]=logical
        session.commit()

        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000",timeout=60) as client:
            async def tenant_retrieve(key,agent=None):
                payload={"external_user_id":external,"query":"What do you know about this user?","limit":10}
                if agent: payload["agent_id"]=str(agent)
                started=time.perf_counter();resp=await client.post("/v1/memories/retrieve",headers={"Authorization":f"ApiKey {key}"},json=payload);return resp,(time.perf_counter()-started)*1000
            async def universal_retrieve(key,token,query="What do you know about this user?"):
                started=time.perf_counter();resp=await client.post("/v1/universal/memories/retrieve",headers={"Authorization":f"ApiKey {key}","X-MemoryOS-UUI":token},json={"query":query,"limit":10});return resp,(time.perf_counter()-started)*1000
            ra,la=await tenant_retrieve(tenant_key,agent_a.id);rb,lb=await tenant_retrieve(tenant_key,agent_b.id);ru,lu=await tenant_retrieve(tenant_key);rt,lt=await tenant_retrieve(tenant_b_key)
            ids=lambda response,mapping:[mapping.get(str(x.get("id")),"unknown") for x in response.json().get("data",[])] if response.status_code==200 else []
            observed={"tenant_agent_a_filter":ids(ra,keymaps)==["local-a"],"tenant_agent_b_filter":ids(rb,keymaps)==["local-b"],"tenant_unfiltered_shared_pool":set(ids(ru,keymaps))=={"local-a","local-b"},"tenant_other_user_isolation":"other-tenant" not in ids(ru,keymaps),"tenant_other_tenant_isolation":set(ids(rt,keymaps))=={"other-tenant"}}
            ua,lua=await universal_retrieve(ga_key,u1.uui_token);ub,lub=await universal_retrieve(gb_key,u1.uui_token);uo,luo=await universal_retrieve(ga_key,u2.uui_token);ur,lur=await universal_retrieve(gr_key,u1.uui_token)
            ua_ids=ids(ua,universal_keys);ub_ids=ids(ub,universal_keys);uo_ids=ids(uo,universal_keys)
            write=await client.post("/v1/universal/memories/add",headers={"Authorization":f"ApiKey {gb_key}","X-MemoryOS-UUI":u1.uui_token},json={"messages":[{"role":"user","content":"Remember a fact."}]})
            source_storage=all(m.source_agent_id is not None and (m.metadata_json or {}).get("provenance",{}).get("source_agent_id") for m in session.execute(select(UniversalMemory).where(UniversalMemory.user_uui_id==u1.id)).scalars().all())
            source_api=all("source_agent_id" in item or (item.get("provenance") or {}).get("source_agent_id") for item in ua.json().get("data",[]))
            observed.update({"passport_agent_a_category_grant":set(ua_ids)=={"ua-fact","ub-fact"},"passport_agent_b_category_grant":set(ub_ids)=={"ub-rel"},"passport_authorized_cross_source_share":"ub-fact" in ua_ids,"passport_other_user_isolation":set(uo_ids)=={"u2-fact"},"passport_revoked_grant":ur.status_code==200 and ur.json().get("permission_error")=="no_grant_for_user" and not ur.json().get("data"),"passport_read_only_write":write.status_code==403,"passport_source_agent_provenance":bool(source_storage and source_api)})
            latencies={"tenant_agent_a_filter":la,"tenant_agent_b_filter":lb,"tenant_unfiltered_shared_pool":lu,"tenant_other_user_isolation":lu,"tenant_other_tenant_isolation":lt,"passport_agent_a_category_grant":lua,"passport_agent_b_category_grant":lub,"passport_authorized_cross_source_share":lua,"passport_other_user_isolation":luo,"passport_revoked_grant":lur,"passport_read_only_write":0,"passport_source_agent_provenance":lua}
            for case in frozen: rows.append({**case,"passed":bool(observed.get(case["id"],False)),"latency_ms":round(latencies.get(case["id"],0),2)})
        summary={"scenario_count":len(rows),"passed":sum(r["passed"] for r in rows),"end_to_end_success_rate":sum(r["passed"] for r in rows)/len(rows),"cross_tenant_leakage":0 if observed["tenant_other_tenant_isolation"] else 1,"cross_user_leakage":0 if observed["tenant_other_user_isolation"] and observed["passport_other_user_isolation"] else 1,"agent_filter_accuracy":sum(observed[k] for k in ("tenant_agent_a_filter","tenant_agent_b_filter"))/2,"expected_shared_visibility":sum(observed[k] for k in ("tenant_unfiltered_shared_pool","passport_authorized_cross_source_share"))/2,"grant_and_revocation_accuracy":sum(observed[k] for k in ("passport_agent_a_category_grant","passport_agent_b_category_grant","passport_revoked_grant","passport_read_only_write"))/4,"source_agent_storage_preservation":1.0 if source_storage else 0.0,"source_agent_api_readback":1.0 if source_api else 0.0,"mean_latency_ms":sum(r["latency_ms"] for r in rows)/len(rows)}
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps({"benchmark":"multi-agent-isolation-live-development-v1","captured_at":datetime.now(UTC).isoformat(),"holdout_used":False,"production_behavior_changed":False,"summary":summary,"cases":rows},indent=2),encoding="utf-8");print(json.dumps(summary,indent=2))
    finally:
        for pid,collection in point_ids:
            try:qdrant.delete_memory(pid,collection_name=collection)
            except Exception:pass
        try:
            if created_uui:session.execute(delete(UniversalUser).where(UniversalUser.id.in_(created_uui)))
            if created_users:session.execute(delete(User).where(User.id.in_(created_users)))
            if created_tenants:session.execute(delete(Tenant).where(Tenant.id.in_(created_tenants)))
            session.commit()
        finally:session.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--output",type=Path,required=True);args=parser.parse_args();asyncio.run(run(args.output))


if __name__ == "__main__": main()
