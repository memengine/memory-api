"""Focused real projection check on disposable PostgreSQL; no API/evaluation."""
import asyncio
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

from api.db.models import Base, Region, Tenant, ProxyUser, ExtractionJob, PlanTier, ExtractionJobStatus
from api.services.proxy_user_service import ProxyUserService

spec=importlib.util.spec_from_file_location("sql_observer",Path(__file__).resolve().parents[2]/"benchmarks/internal/explicit_operations_pilot.py")
observer=importlib.util.module_from_spec(spec); sys.modules[spec.name]=observer; spec.loader.exec_module(observer)
PG="postgresql+psycopg2://replay_test:replay_test_only@127.0.0.1:55471/replay_test"
APG=PG.replace("postgresql+psycopg2","postgresql+asyncpg")


def test_real_scoped_receipt_projections_and_read_only_sql():
    schema="observer_"+uuid.uuid4().hex
    admin=create_engine(PG,connect_args={"connect_timeout":3})
    with admin.begin() as connection: connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine=create_engine(PG,connect_args={"connect_timeout":3,"options":"-c search_path="+schema})
    try:
        Base.metadata.create_all(engine)
        factory=sessionmaker(engine,expire_on_commit=False)
        owners=[uuid.uuid4(),uuid.uuid4()]
        tenant=uuid.uuid4()
        with factory.begin() as session:
            session.add(Region(id="IN1",aws_region="local",postgres_url_secret="unused",redis_url_secret="unused",qdrant_url_secret="unused"))
            session.flush()
            session.add(Tenant(id=tenant,company_name="Synthetic projection",region_id="IN1",plan_tier=PlanTier.starter));session.flush()
            for i,owner in enumerate(owners):
                external=f"synthetic-owner-{i}"
                session.add(ProxyUser(id=owner,tenant_id=tenant,external_user_id=external,
                    external_user_id_hash=ProxyUserService.hash_external_user_id(str(tenant),external)))
            session.flush()
            for i,owner in enumerate(owners):
                session.add(ExtractionJob(id=uuid.uuid4(),tenant_id=tenant,proxy_user_id=owner,external_user_id=f"synthetic-owner-{i}",
                    status=ExtractionJobStatus.queued,
                    request_key_sha256=str(i)*64,request_binding={"version":1,"owner_id":f"synthetic-owner-{i}","payload_sha256":"a"*64},
                    payload={"messages":[{"content":"raw payload must not be projected"}]},runtime_withholding_footprint={"broad":True}))
        async def run():
            asynchronous=create_async_engine(APG,connect_args={"timeout":3,"server_settings":{"search_path":schema}})
            try:
                return await observer.sql_snapshot(asynchronous,[owners[0]])
            finally: await asynchronous.dispose()
        snapshot=asyncio.run(run())
        assert len(snapshot["jobs"])==1 and snapshot["jobs"][0]["proxy_user_id"]==str(owners[0])
        assert snapshot["jobs"][0]["binding_payload_sha256"]=="a"*64
        assert snapshot["jobs"][0]["runtime_withholding_footprint"]=={"broad":True}
        assert "payload" not in snapshot["jobs"][0]
        assert set(snapshot)=={"memories","claims","revisions","audit","versions","jobs","sources","outbox"}
    finally:
        engine.dispose()
        with admin.begin() as connection: connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
