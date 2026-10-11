"""Transport boundary and unchanged watchdog/extraction timing invariants."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from api.celery_app import create_celery_app
from api.services.memory_service import MemoryService, EXTRACTION_TASK_NAME
from api.tasks.watchdog_tasks import DISPATCH_RECOVERY_TASK_NAME, QUEUED_DISPATCH_GRACE


def test_durable_and_legacy_publications_use_correct_transport_identity():
    calls = []
    def dispatch(name, **kwargs):
        calls.append((name, kwargs))
        return SimpleNamespace(id=kwargs.get("task_id", "transport-generated"))
    service = MemoryService(session=MagicMock(), cache_service=MagicMock(),
        qdrant_service=MagicMock(), embedding_service=MagicMock(),
        quota_manager=MagicMock(), dispatch_task=dispatch)
    job = {"job_id": "stable-job", "tenant_id": "synthetic-tenant",
        "proxy_user_id": "synthetic-owner", "queue_name": "starter-extraction",
        "request_key_sha256": "a" * 64, "celery_task_id": "stable-job"}
    result = asyncio.run(service._send_extraction_task(job))
    assert result.id == "stable-job"
    assert calls[0][0] == EXTRACTION_TASK_NAME
    assert calls[0][1]["task_id"] == "stable-job" and calls[0][1]["retry"] is False
    assert calls[0][1]["args"] == [{"job_id": "stable-job", "tenant_id": "synthetic-tenant",
        "queue_name": "starter-extraction", "_payload_reference": "extraction_job"}]
    legacy = dict(job)
    legacy.pop("request_key_sha256")
    asyncio.run(service._send_extraction_task(legacy))
    assert "task_id" not in calls[1][1] and "retry" not in calls[1][1]


def test_new_schedule_preserves_watchdog_and_worker_processing_limits():
    app = create_celery_app()
    assert app.conf.beat_schedule["recover-durable-extraction-dispatch"]["schedule"] == 5
    assert app.conf.beat_schedule["requeue-stale-extraction-jobs"]["schedule"] == 120
    assert QUEUED_DISPATCH_GRACE.total_seconds() == 60
    assert app.conf.task_annotations[DISPATCH_RECOVERY_TASK_NAME] == {"soft_time_limit": 3, "time_limit": 4}
    assert app.conf.task_annotations[EXTRACTION_TASK_NAME]["soft_time_limit"] == 105
    assert app.conf.task_annotations[EXTRACTION_TASK_NAME]["time_limit"] == 120
