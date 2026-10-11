"""Disposable local child-process runtime for approved recovery checks only."""
import importlib.util
import os
import re
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

# Running a saved script sets sys.path to this directory, not the backend root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from celery import Celery, signals
from celery.beat import Service
import redis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.services import memory_service
from api.services.embedding_service import EmbeddingResult, DEFAULT_ACTIVE_MODEL_ID
from api.settings import get_settings
from api.tasks import extraction_tasks, watchdog_tasks
from api.tasks import queue_router

spec = importlib.util.spec_from_file_location("dispatch_child_helpers",
    Path(__file__).with_name("test_durable_add_replay.py"))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def main():
    mode, schema, tenant_id, proxy_id, key_id, external, *extra = sys.argv[1:]
    if not re.fullmatch(r"replay_[a-f0-9]{32}", schema):
        raise ValueError("Only disposable test schema accepted")
    original_connect = socket.socket.connect
    def local_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "localhost"} or address[1] not in {55471, 55472}:
            raise AssertionError("Only disposable database/broker network authorized")
        return original_connect(sock, address)
    socket.socket.connect = local_only
    engine = create_engine(helpers.PG_URL, connect_args={"connect_timeout": 3,
        "options": f"-c search_path={schema} -c statement_timeout=1000 -c lock_timeout=500"})
    factory = sessionmaker(engine, expire_on_commit=False)
    client = redis.Redis.from_url(helpers.REDIS_URL, decode_responses=True, socket_timeout=2)
    scope = SimpleNamespace(factory=factory, client=client, tenant_id=tenant_id,
        proxy_id=proxy_id, key_id=key_id, external=external, schema=schema)
    controlled = get_settings().model_copy(update={"explicit_preference_claims_enabled": True,
        "runtime_preference_enabled": False, "phase3a_confirmation_enabled": False,
        "phase3a_confirmation_shadow_enabled": False})
    memory_service.get_settings = lambda: controlled
    extraction_tasks.get_settings = lambda: controlled
    from api.celery_app import create_celery_app
    app = create_celery_app()
    app.set_current()
    app.set_default()
    app.conf.update(broker_url=helpers.REDIS_URL, result_backend=helpers.REDIS_URL,
        task_serializer="json", accept_content=["json"],
        result_serializer="json", worker_prefetch_multiplier=1, worker_enable_remote_control=False,
        broker_connection_retry_on_startup=False, broker_connection_timeout=1,
        broker_transport_options={"socket_timeout": 1, "socket_connect_timeout": 1},
        beat_schedule=watchdog_tasks.DISPATCH_RECOVERY_BEAT_SCHEDULE,
        beat_max_loop_interval=1, task_default_queue="celery")
    app.finalize()
    real_recovery = watchdog_tasks.run_durable_dispatch_recovery
    watchdog_tasks.run_durable_dispatch_recovery = lambda: real_recovery(session_factory=factory)
    extraction_tasks.build_extraction_session_factory = lambda: factory
    extraction_tasks._redis_client = lambda: redis.Redis.from_url(helpers.REDIS_URL, decode_responses=True)
    extraction_tasks._run_runtime_preference_job = lambda _: None
    queue_router._redis_sync_client = lambda: redis.Redis.from_url(helpers.REDIS_URL, decode_responses=True)

    if mode == "api-kill":
        with factory() as session:
            svc = helpers.service(scope, session)
            async def interrupted(job): os._exit(73)
            svc._dispatch_extraction_job = interrupted
            helpers.run(svc.queue_memory_add(**helpers.args(scope)))
        raise AssertionError("Expected API exit after committed admission")
    if mode.startswith("publisher-kill"):
        def publisher(payload, task_id, queue):
            if mode == "publisher-kill-before": os._exit(74)
            watchdog_tasks._publish_durable_reference(payload, task_id, queue)
            os._exit(74)
        real_recovery(session_factory=factory, publisher=publisher)
        raise AssertionError("Expected publisher exit")
    if mode == "duplicate":
        job_id, queue = extra
        watchdog_tasks._publish_durable_reference({"job_id": job_id, "tenant_id": tenant_id,
            "queue_name": queue, "_payload_reference": "extraction_job"}, job_id, queue)
        return
    if mode == "beat":
        beat = Service(app=app, scheduler_cls="celery.beat:Scheduler", max_interval=1)
        beat.scheduler  # Construct successfully before reporting readiness.
        client.set("dispatch-test-beat-ready", "1", ex=60)
        # In-memory schedule: no persistent scheduler artifacts/production jobs.
        beat.start()
        return
    if mode != "worker": raise ValueError("Unknown isolated runtime mode")

    class ControlledEmbedding:
        def __init__(self, **kwargs): pass
        def embed_sync(self, text, **kwargs):
            return EmbeddingResult([.01] * 1536, DEFAULT_ACTIVE_MODEL_ID, 1536, "memories")
    extraction_tasks.EmbeddingService = ControlledEmbedding
    pipeline = extraction_tasks.run_extraction_pipeline
    extraction_tasks.run_extraction_pipeline = lambda job: pipeline(job, session_factory=factory,
        extractor=SimpleNamespace(proposal_confirmation_enabled=False),
        scorer=helpers.Forbidden(), qdrant_service=helpers.Forbidden())
    @signals.worker_ready.connect(weak=False)
    def ready(**kwargs): client.set("dispatch-test-worker-ready", "1", ex=60)
    @signals.task_postrun.connect(weak=False)
    def observed(retval=None, **kwargs):
        if isinstance(retval, dict) and retval.get("status") == "duplicate_ignored":
            client.set("dispatch-test-duplicate-ignored", "1", ex=60)
    app.worker_main(["worker", "--pool=solo", "--concurrency=1", "--loglevel=INFO",
        "--without-gossip", "--without-mingle", "--without-heartbeat", "-Q", "celery,starter-extraction"])


if __name__ == "__main__": main()
