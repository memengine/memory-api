from __future__ import annotations

import inspect

from api.celery_app import celery_app
from api.services.lifecycle_manager import MemoryLifecycleManager
from api.services.retriever import RetrieverService


def test_current_retrieval_enforces_effective_validity_without_as_of() -> None:
    source = inspect.getsource(RetrieverService.retrieve)
    current_branch = source.split("if as_of is not None:", maxsplit=1)[1]
    assert "effective_from" in current_branch
    assert "effective_until" in current_branch


def test_lifecycle_manager_processes_scheduled_activation_and_expiration() -> None:
    source = inspect.getsource(MemoryLifecycleManager)
    assert "effective_from" in source
    assert "effective_until" in source


def test_lifecycle_transitions_claim_winner_state() -> None:
    source = inspect.getsource(MemoryLifecycleManager)
    assert "MemoryClaim" in source
    assert "MemoryClaimRevision" in source
    assert "winning_revision" in source


def test_lifecycle_vector_changes_use_transactional_outbox() -> None:
    source = inspect.getsource(MemoryLifecycleManager)
    assert "enqueue_vector_archive" in source
    assert "enqueue_vector_delete" in source
    assert ".delete_memory(" not in source


def test_hot_tier_payload_preserves_temporal_validity() -> None:
    source = inspect.getsource(MemoryLifecycleManager._memory_cache_payload)
    assert "effective_from" in source
    assert "effective_until" in source


def test_celery_has_semantic_validity_transition_schedule() -> None:
    scheduled_tasks = {
        value.get("task")
        for value in celery_app.conf.beat_schedule.values()
        if isinstance(value, dict)
    }
    assert "api.tasks.lifecycle_tasks.run_temporal_validity_transitions" in scheduled_tasks
