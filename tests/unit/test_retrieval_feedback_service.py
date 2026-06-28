from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from api.db.models import RetrievalEvent
from api.services.retrieval_feedback_service import LOW_RELEVANCE_THRESHOLD
from api.services.retrieval_feedback_service import RetrievalFeedbackService


class FakeSession:
    def __init__(self, retrieval_event=None) -> None:
        self.retrieval_event = retrieval_event
        self.added = []
        self.commits = 0
        self.flushes = 0
        self.refreshed = []

    def add(self, item) -> None:
        if getattr(item, "id", None) is None:
            item.id = uuid4()
        self.added.append(item)

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        self.flushes += 1

    async def refresh(self, item) -> None:
        self.refreshed.append(item)

    async def get(self, model, identifier):
        if model is RetrievalEvent and self.retrieval_event is not None and self.retrieval_event.id == identifier:
            return self.retrieval_event
        return None


def test_query_hash_normalizes_whitespace_and_case() -> None:
    assert RetrievalFeedbackService.query_hash("  Prefers Hindi  ") == RetrievalFeedbackService.query_hash("prefers   hindi")


@pytest.mark.asyncio
async def test_log_retrieval_stores_privacy_safe_signal() -> None:
    session = FakeSession()
    service = RetrievalFeedbackService(session=session)
    event = await service.log_retrieval(
        tenant_id=str(uuid4()),
        proxy_user_id=str(uuid4()),
        external_user_id="customer-1",
        query="What language should I use?",
        categories=["preference"],
        agent_id=None,
        retrieved_memory_ids=[],
        result_count=0,
        top_relevance_score=None,
        included_in_prompt=False,
        cache_hit=False,
        quota_mode="full",
        is_degraded=False,
        metadata={"request_id": "req-1"},
    )

    assert event in session.added
    assert session.commits == 1
    assert event.not_found is True
    assert event.query_preview is None
    assert event.query_hash != "What language should I use?"


@pytest.mark.asyncio
async def test_log_retrieval_marks_low_relevance() -> None:
    session = FakeSession()
    service = RetrievalFeedbackService(session=session)
    event = await service.log_retrieval(
        tenant_id=str(uuid4()),
        proxy_user_id=str(uuid4()),
        external_user_id="customer-1",
        query="billing",
        categories=[],
        agent_id=None,
        retrieved_memory_ids=[str(uuid4())],
        result_count=1,
        top_relevance_score=LOW_RELEVANCE_THRESHOLD - 0.01,
        included_in_prompt=True,
        cache_hit=True,
        quota_mode="full",
        is_degraded=False,
    )

    assert event.low_relevance is True
    assert event.not_found is False


@pytest.mark.asyncio
async def test_user_correction_feedback_queues_retrospective_extraction_job() -> None:
    tenant_id = uuid4()
    proxy_user_id = uuid4()
    retrieval = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        proxy_user_id=proxy_user_id,
        external_user_id="customer-1",
    )
    session = FakeSession(retrieval_event=retrieval)
    memory_service = SimpleNamespace(queue_memory_add=AsyncMock(return_value={"job_id": str(uuid4())}))

    feedback = await RetrievalFeedbackService(session=session).record_feedback(
        tenant_id=str(tenant_id),
        retrieval_id=str(retrieval.id),
        outcome="user_corrected",
        used_memory_ids=[str(uuid4())],
        correction="Actually I prefer Hindi, not English.",
        agent_confidence=0.7,
        metadata={},
        memory_service=memory_service,
        api_key_id="api-key-id",
    )

    assert session.flushes == 1
    assert session.commits == 1
    assert feedback.correction_job_id is not None
    memory_service.queue_memory_add.assert_awaited_once()
    kwargs = memory_service.queue_memory_add.await_args.kwargs
    assert kwargs["tenant_id"] == str(tenant_id)
    assert kwargs["external_user_id"] == "customer-1"
    assert kwargs["source"]["service"] == "retrieval-feedback"
    assert kwargs["messages"][-1]["content"] == "Actually I prefer Hindi, not English."

@pytest.mark.asyncio
async def test_clarification_feedback_with_correction_queues_retrospective_extraction_job() -> None:
    tenant_id = uuid4()
    proxy_user_id = uuid4()
    retrieval = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        proxy_user_id=proxy_user_id,
        external_user_id="customer-1",
    )
    session = FakeSession(retrieval_event=retrieval)
    memory_service = SimpleNamespace(queue_memory_add=AsyncMock(return_value={"job_id": str(uuid4())}))

    feedback = await RetrievalFeedbackService(session=session).record_feedback(
        tenant_id=str(tenant_id),
        retrieval_id=str(retrieval.id),
        outcome="clarification_needed",
        used_memory_ids=[],
        correction="The missing fact is that I prefer Hindi for support replies.",
        agent_confidence=0.4,
        metadata={"reason": "retrieval_miss"},
        memory_service=memory_service,
        api_key_id="api-key-id",
    )

    assert feedback.correction_job_id is not None
    memory_service.queue_memory_add.assert_awaited_once()
    kwargs = memory_service.queue_memory_add.await_args.kwargs
    assert kwargs["metadata"]["retrospective_extraction"] is True
    assert kwargs["metadata"]["outcome"] == "clarification_needed"
    assert "retrieval miss" in kwargs["messages"][0]["content"]
    assert kwargs["messages"][-1]["content"] == "The missing fact is that I prefer Hindi for support replies."


@pytest.mark.asyncio
async def test_clarification_feedback_without_correction_only_records_signal() -> None:
    tenant_id = uuid4()
    proxy_user_id = uuid4()
    retrieval = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        proxy_user_id=proxy_user_id,
        external_user_id="customer-1",
    )
    session = FakeSession(retrieval_event=retrieval)
    memory_service = SimpleNamespace(queue_memory_add=AsyncMock())

    feedback = await RetrievalFeedbackService(session=session).record_feedback(
        tenant_id=str(tenant_id),
        retrieval_id=str(retrieval.id),
        outcome="clarification_needed",
        used_memory_ids=[],
        correction=None,
        agent_confidence=0.2,
        metadata={"reason": "agent_asked_user"},
        memory_service=memory_service,
    )

    assert feedback.correction_job_id is None
    memory_service.queue_memory_add.assert_not_awaited()
    assert session.commits == 1

