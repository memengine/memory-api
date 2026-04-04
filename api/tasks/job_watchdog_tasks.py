from api.tasks.watchdog_tasks import WATCHDOG_BEAT_SCHEDULE
from api.tasks.watchdog_tasks import WATCHDOG_TASK_NAME
from api.tasks.watchdog_tasks import check_stale_jobs as requeue_stale_extraction_jobs
from api.tasks.watchdog_tasks import run_watchdog_cycle as _run_watchdog_cycle
from api.tasks.watchdog_tasks import process_extraction_job


STALE_PROCESSING_SECONDS = 600


__all__ = [
    "WATCHDOG_BEAT_SCHEDULE",
    "WATCHDOG_TASK_NAME",
    "STALE_PROCESSING_SECONDS",
    "process_extraction_job",
    "requeue_stale_extraction_jobs",
    "run_watchdog_cycle",
]


def run_watchdog_cycle(*args, **kwargs):
    result = _run_watchdog_cycle(*args, **kwargs)
    return {
        "checked": int(result.get("checked", 0)),
        "requeued": int(result.get("requeued", 0)),
    }
