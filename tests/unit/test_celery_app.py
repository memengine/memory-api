from __future__ import annotations

from api.celery_app import CELERY_IMPORTS
from api.celery_app import create_celery_app
from api.tasks.decay_tasks import DECAY_TASK_NAME
from api.tasks.job_watchdog_tasks import WATCHDOG_TASK_NAME
from api.tasks.reembedding_tasks import REEMBED_TASK_NAME


def test_celery_app_has_expected_broker_and_imports(monkeypatch) -> None:
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://custom-broker:6379/5")
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://custom-backend:6379/6")

    app = create_celery_app()

    assert app.conf.broker_url == "redis://custom-broker:6379/5"
    assert app.conf.result_backend == "redis://custom-backend:6379/6"
    assert tuple(app.conf.imports) == CELERY_IMPORTS


def test_celery_app_preserves_verified_redis_tls_urls(monkeypatch) -> None:
    broker_url = "rediss://:token@redis.internal:6379/0?ssl_cert_reqs=required"
    result_backend = "rediss://:token@redis.internal:6379/1?ssl_cert_reqs=required"
    monkeypatch.setenv("CELERY_BROKER_URL", broker_url)
    monkeypatch.setenv("CELERY_RESULT_BACKEND", result_backend)

    app = create_celery_app()

    assert app.conf.broker_url == broker_url
    assert app.conf.result_backend == result_backend


def test_celery_app_registers_decay_schedule() -> None:
    app = create_celery_app()

    schedule = app.conf.beat_schedule["archive-stale-low-importance-memories"]
    watchdog_schedule = app.conf.beat_schedule["requeue-stale-extraction-jobs"]

    assert schedule["task"] == DECAY_TASK_NAME
    assert watchdog_schedule["task"] == WATCHDOG_TASK_NAME
    assert app.conf.task_default_queue == "celery"
    assert app.conf.task_routes[REEMBED_TASK_NAME]["queue"] == "reembedding"
