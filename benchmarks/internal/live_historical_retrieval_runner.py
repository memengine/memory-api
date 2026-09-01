from __future__ import annotations

import argparse, asyncio, hashlib, json, math, os, time, uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from sqlalchemy import delete, select

from api.db.database import build_sync_session_factory
from api.db.models import ApiKey, Conversation, EmbeddingModel, Memory, MemoryCategory, ProxyUser, User
from api.db.vector_store import QdrantService
from api.services.embedding_service import EmbeddingService
from api.services.vector_outbox import build_vector_payload
from api.utils.crypto import api_key_prefix, verify_api_key
from benchmarks.internal.historical_retrieval_eval import DATASET, _dcg


def _tenant(session: Any, raw_key: str):
    rows = session.execute(select(ApiKey).where(ApiKey.is_active.is_(True), ApiKey.key_prefix == api_key_prefix(raw_key))).scalars().all()
    row = next((item for item in rows if verify_api_key(raw_key, item.key_hash)), None)
    if row is None: raise RuntimeError("BENCHMARK_API_KEY does not match an active key")
    return row.tenant_id


async def run(output: Path) -> None:
    load_dotenv()
    raw_key = os.getenv("BENCHMARK_API_KEY", "").strip()
    base_user = os.getenv("BENCHMARK_EXTERNAL_USER_ID", "historical-benchmark").strip()
    if not raw_key: raise RuntimeError("BENCHMARK_API_KEY is required")
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line]
    session = build_sync_session_factory()(); qdrant = QdrantService(); embedder = EmbeddingService(sync_session=session)
    run_id = uuid.uuid4().hex[:12]; point_ids=[]; proxy_ids=[]; user_ids=[]; rows=[]; calls=0; chars=0
    try:
        tenant_id = _tenant(session, raw_key)
        model = session.execute(select(EmbeddingModel).where(EmbeddingModel.is_active.is_(True)).limit(1)).scalar_one()
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=60) as client:
            for index, case in enumerate(cases):
                external_id=f"{base_user}-hist-{run_id}-{index}"; proxy=ProxyUser(id=uuid.uuid4(),tenant_id=tenant_id,external_user_id=external_id,external_user_id_hash=hashlib.sha256(f"{tenant_id}:{external_id}".encode()).hexdigest(),memory_count=0,metadata_json={"benchmark":run_id},is_blocked=False)
                user=User(id=uuid.uuid4(),external_id=f"hist::{run_id}::{index}",email=f"hist-{run_id}-{index}@benchmark.test",settings={},memory_count=0,is_active=True)
                session.add_all([proxy,user]);session.flush();proxy_ids.append(proxy.id);user_ids.append(user.id)
                conversation=Conversation(id=uuid.uuid4(),user_id=user.id,message_count=1);session.add(conversation);session.flush();key_by_id={}
                for candidate in case["candidates"]:
                    memory=Memory(id=uuid.uuid4(),user_id=user.id,proxy_user_id=proxy.id,content=candidate["content"],category=MemoryCategory.fact,importance_score=float(candidate["importance"]),confidence_score=.95,embedding_id=str(uuid.uuid4()),embedding_model_id=model.id,source_conversation_id=conversation.id,effective_from=datetime.fromisoformat(candidate["effective_from"].replace("Z","+00:00")) if candidate.get("effective_from") else None,effective_until=datetime.fromisoformat(candidate["effective_until"].replace("Z","+00:00")) if candidate.get("effective_until") else None,metadata_json={"benchmark_key":candidate["id"],"provenance":{"service":"historical-benchmark","event_id":f"{run_id}-{candidate['id']}"}},created_at=datetime(2026,1,1,tzinfo=UTC),last_accessed_at=datetime(2026,1,1,tzinfo=UTC),is_archived=bool(candidate["archived"]))
                    session.add(memory);session.flush();embedding=embedder.embed_sync(memory.content,model_id=model.id,tenant_id=str(tenant_id));calls+=1;chars+=len(memory.content)
                    payload=build_vector_payload(memory,tenant_id=str(tenant_id),proxy_user_id=str(proxy.id),user_id=str(user.id),embedding_model_id=model.id,qdrant_collection=model.qdrant_collection);payload["lifecycle_state"]="superseded" if memory.is_archived else "active"
                    qdrant.upsert_memory(str(memory.id),embedding.vector,payload,collection_name=model.qdrant_collection,vector_size=model.dimensions);point_ids.append((str(memory.id),model.qdrant_collection));key_by_id[str(memory.id)]=candidate["id"]
                proxy.memory_count=len(case["candidates"]);session.commit()
                started=time.perf_counter();response=await client.post("/v1/memories/retrieve",headers={"Authorization":f"ApiKey {raw_key}"},json={"external_user_id":external_id,"query":case["query"],"as_of":case["as_of"],"limit":case["limit"]});latency=(time.perf_counter()-started)*1000
                data=response.json().get("data",[]) if response.status_code==200 else [];ids=[key_by_id.get(str(item.get("id")),"unknown") for item in data];relevant=set(case["relevant_ids"]);hits=[1 if key in relevant else 0 for key in ids];first=next((i+1 for i,v in enumerate(hits) if v),None);ideal=[1]*min(len(relevant),len(ids))+[0]*max(0,len(ids)-len(relevant));idcg=_dcg(ideal)
                current=await client.post("/v1/memories/retrieve",headers={"Authorization":f"ApiKey {raw_key}"},json={"external_user_id":external_id,"query":case["query"],"limit":case["limit"]});current_ids=[key_by_id.get(str(item.get("id")),"unknown") for item in (current.json().get("data",[]) if current.status_code==200 else [])];archived={c["id"] for c in case["candidates"] if c["archived"]}
                rows.append({"id":case["id"],"status_code":response.status_code,"returned_ids":ids,"expected_relevant_ids":sorted(relevant),"precision_at_k":sum(hits)/len(ids) if ids else 0.0,"recall_at_k":sum(hits)/len(relevant),"mrr":1/first if first else 0.0,"ndcg":_dcg(hits)/idcg if idcg else 1.0,"incorrect_filler_results":len(ids)-sum(hits),"historical_state_leakage_into_current":sum(key in archived for key in current_ids),"provenance_preserved":all(item.get("provenance") for item in data),"latency_ms":round(latency,2)})
        n=len(rows);summary={"scenario_count":n,"historical_precision_at_k":sum(r["precision_at_k"] for r in rows)/n,"historical_recall_at_k":sum(r["recall_at_k"] for r in rows)/n,"historical_mrr":sum(r["mrr"] for r in rows)/n,"historical_ndcg":sum(r["ndcg"] for r in rows)/n,"incorrect_historical_filler_results":sum(r["incorrect_filler_results"] for r in rows),"historical_state_leakage_into_current":sum(r["historical_state_leakage_into_current"] for r in rows),"provenance_preservation":sum(r["provenance_preserved"] for r in rows)/n,"mean_latency_ms":sum(r["latency_ms"] for r in rows)/n,"max_latency_ms":max(r["latency_ms"] for r in rows),"embedding_provider_calls":calls+n,"estimated_embedding_tokens":math.ceil((chars+sum(len(c["query"]) for c in cases))/4)}
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps({"benchmark":"historical-retained-vector-live-development-v1","captured_at":datetime.now(UTC).isoformat(),"provider":str(model.provider.value if hasattr(model.provider,"value") else model.provider),"model":model.model_name,"holdout_used":False,"production_behavior_changed":True,"summary":summary,"cases":rows},indent=2),encoding="utf-8");print(json.dumps(summary,indent=2))
    finally:
        for point_id,collection in point_ids:
            try:qdrant.delete_memory(point_id,collection_name=collection)
            except Exception:pass
        try:
            if proxy_ids:session.execute(delete(ProxyUser).where(ProxyUser.id.in_(proxy_ids)))
            if user_ids:session.execute(delete(User).where(User.id.in_(user_ids)))
            session.commit()
        finally:session.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--output",type=Path,required=True);args=parser.parse_args();asyncio.run(run(args.output))


if __name__ == "__main__": main()
