from __future__ import annotations

from celery import shared_task

from api.tasks.retrieval_tasks import run_access_update


ACCESS_TASK_NAME = "api.tasks.access_tasks.update_access_stats"


@shared_task(name=ACCESS_TASK_NAME)
def update_access_stats(memory_id: str) -> int:
    return run_access_update([memory_id])
