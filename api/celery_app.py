from __future__ import annotations

import os

from celery import Celery

from api.settings import get_settings
from api.tasks.decay_tasks import DECAY_TASK_BEAT_SCHEDULE
from api.tasks.backfill_tasks import BACKFILL_STATUS_TASK_NAME
from api.tasks.deprecation_tasks import DEPRECATION_ALERT_BEAT_SCHEDULE
from api.tasks.quota_tasks import QUOTA_TASK_BEAT_SCHEDULE
from api.tasks.reconciliation_tasks import RECONCILIATION_TASK_BEAT_SCHEDULE
from api.tasks.reembedding_tasks import REEMBED_TASK_NAME
from api.tasks.vector_sync_tasks import VECTOR_SYNC_TASK_BEAT_SCHEDULE
from api.tasks.watchdog_tasks import WATCHDOG_BEAT_SCHEDULE

CELERY_IMPORTS = (
    "api.tasks.decay_tasks",
    "api.tasks.backfill_tasks",
    "api.tasks.deprecation_tasks",
    "api.tasks.watchdog_tasks",
    "api.tasks.extraction_tasks",
    "api.tasks.quality_gate_tasks",
    "api.tasks.quota_tasks",
    "api.tasks.reembedding_tasks",
    "api.tasks.reconciliation_tasks",
    "api.tasks.retrieval_tasks",
    "api.tasks.vector_sync_tasks",
)


def create_celery_app() -> Celery:
    app = Celery("memoryos")
    settings = get_settings()
    broker_url = os.getenv("CELERY_BROKER_URL") or settings.celery_broker_url or os.getenv("REDIS_URL") or settings.redis_url
    result_backend = os.getenv("CELERY_RESULT_BACKEND") or settings.celery_result_backend or os.getenv("REDIS_URL") or settings.redis_url
    app.conf.update(
        broker_url=broker_url,
        result_backend=result_backend,
        imports=CELERY_IMPORTS,
        beat_schedule={
            **DECAY_TASK_BEAT_SCHEDULE,
            **DEPRECATION_ALERT_BEAT_SCHEDULE,
            **QUOTA_TASK_BEAT_SCHEDULE,
            **WATCHDOG_BEAT_SCHEDULE,
            **VECTOR_SYNC_TASK_BEAT_SCHEDULE,
            **RECONCILIATION_TASK_BEAT_SCHEDULE,
        },
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        enable_utc=True,
        timezone="UTC",
        task_track_started=True,
        task_default_queue="celery",
        task_routes={
            REEMBED_TASK_NAME: {"queue": "reembedding"},
        },
    )
    return app


celery_app = create_celery_app()


__all__ = [
    "CELERY_IMPORTS",
    "celery_app",
    "create_celery_app",
]
