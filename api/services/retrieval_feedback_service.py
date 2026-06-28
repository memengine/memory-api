from __future__ import annotations

import hashlib
import uuid
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import RetrievalEvent
from api.db.models import RetrievalFeedbackEvent
from api.errors import APIError
from api.services.memory_service import MemoryService


LOW_RELEVANCE_THRESHOLD = 0.25
RETRIEVAL_FEEDBACK_SERVICE = "retrieval-feedback"
RETROSPECTIVE_EXTRACTION_OUTCOMES = {
    "user_corrected",
    "clarification_needed",
    "not_useful",
}


class RetrievalFeedbackService:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def query_hash(query: str) -> str:
        normalized = " ".join(query.strip().lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    async def log_retrieval(
        self,
        *,
        tenant_id: str,
        proxy_user_id: str,
        external_user_id: str,
        query: str,
        categories: list[str],
        agent_id: str | None,
        retrieved_memory_ids: list[str],
        result_count: int,
        top_relevance_score: float | None,
        included_in_prompt: bool,
        cache_hit: bool,
        quota_mode: str | None,
        is_degraded: bool,
        metadata: dict[str, Any] | None = None,
    ) -> RetrievalEvent:
        event = RetrievalEvent(
            tenant_id=uuid.UUID(str(tenant_id)),
            proxy_user_id=uuid.UUID(str(proxy_user_id)),
            external_user_id=external_user_id,
            query_hash=self.query_hash(query),
            query_preview=None,
            categories=[str(category) for category in categories],
            agent_id=uuid.UUID(str(agent_id)) if agent_id else None,
            retrieved_memory_ids=[uuid.UUID(str(memory_id)) for memory_id in retrieved_memory_ids],
            result_count=result_count,
            top_relevance_score=top_relevance_score,
            low_relevance=top_relevance_score is not None and top_relevance_score < LOW_RELEVANCE_THRESHOLD,
            not_found=result_count == 0,
            included_in_prompt=included_in_prompt,
            cache_hit=cache_hit,
            quota_mode=quota_mode,
            is_degraded=is_degraded,
            metadata_json=dict(metadata or {}),
        )
        self.session.add(event)
        await self.session.commit()
        await self.session.refresh(event)
        return event

    async def record_feedback(
        self,
        *,
        tenant_id: str,
        retrieval_id: str,
        outcome: str,
        used_memory_ids: list[str],
        correction: str | None,
        agent_confidence: float | None,
        metadata: dict[str, Any] | None,
        memory_service: MemoryService,
        api_key_id: str | None = None,
    ) -> RetrievalFeedbackEvent:
        retrieval = await self.session.get(RetrievalEvent, uuid.UUID(str(retrieval_id)))
        if retrieval is None or str(retrieval.tenant_id) != str(tenant_id):
            raise APIError(status_code=404, code="RET_404", error="retrieval_not_found")

        feedback = RetrievalFeedbackEvent(
            retrieval_event_id=retrieval.id,
            tenant_id=retrieval.tenant_id,
            proxy_user_id=retrieval.proxy_user_id,
            outcome=outcome,
            used_memory_ids=[uuid.UUID(str(memory_id)) for memory_id in used_memory_ids],
            correction=correction.strip() if correction else None,
            agent_confidence=agent_confidence,
            metadata_json=dict(metadata or {}),
        )
        self.session.add(feedback)
        await self.session.flush()

        if self._should_queue_retrospective_extraction(outcome=outcome, correction=feedback.correction):
            feedback.correction_job_id = await self._queue_retrospective_extraction(
                retrieval=retrieval,
                feedback=feedback,
                outcome=outcome,
                used_memory_ids=used_memory_ids,
                memory_service=memory_service,
                api_key_id=api_key_id,
            )

        await self.session.commit()
        await self.session.refresh(feedback)
        return feedback

    @staticmethod
    def _should_queue_retrospective_extraction(*, outcome: str, correction: str | None) -> bool:
        return outcome in RETROSPECTIVE_EXTRACTION_OUTCOMES and bool(correction)

    async def _queue_retrospective_extraction(
        self,
        *,
        retrieval: RetrievalEvent,
        feedback: RetrievalFeedbackEvent,
        outcome: str,
        used_memory_ids: list[str],
        memory_service: MemoryService,
        api_key_id: str | None,
    ) -> uuid.UUID | None:
        if not feedback.correction:
            return None

        correction_job = await memory_service.queue_memory_add(
            requested_user_id=retrieval.external_user_id,
            authenticated_user_id=None,
            agent_id=None,
            messages=self._correction_messages(
                correction=feedback.correction,
                used_memory_ids=used_memory_ids,
                outcome=outcome,
            ),
            metadata={
                "source": "retrieval_feedback",
                "retrieval_id": str(retrieval.id),
                "feedback_id": str(feedback.id),
                "outcome": outcome,
                "retrospective_extraction": True,
            },
            tenant_id=str(retrieval.tenant_id),
            external_user_id=retrieval.external_user_id,
            proxy_user_id=str(retrieval.proxy_user_id),
            api_key_id=api_key_id,
            source={
                "event_id": f"feedback-{feedback.id}",
                "service": RETRIEVAL_FEEDBACK_SERVICE,
                "observed_at": datetime.now(UTC),
                "scope": {
                    "retrieval_id": str(retrieval.id),
                    "feedback_id": str(feedback.id),
                    "outcome": outcome,
                },
                "evidence": [
                    {
                        "source_type": "retrieval_feedback",
                        "reference": str(retrieval.id),
                    }
                ],
            },
        )
        job_id = correction_job.get("job_id")
        return uuid.UUID(str(job_id)) if job_id else None

    @staticmethod
    def _correction_messages(*, correction: str, used_memory_ids: list[str], outcome: str) -> list[dict[str, str]]:
        if outcome == "user_corrected":
            context = "The user corrected a memory retrieved earlier."
        elif outcome == "clarification_needed":
            context = "A retrieval miss forced the agent to ask for clarification. Re-extract the durable user fact from the clarification."
        else:
            context = "The retrieved memory was not useful. Re-extract any durable user fact from the provided feedback."
        if used_memory_ids:
            context += " Retrieved memory ids: " + ", ".join(used_memory_ids[:10]) + "."
        return [
            {"role": "assistant", "content": context},
            {"role": "user", "content": correction},
        ]


__all__ = ["RetrievalFeedbackService", "LOW_RELEVANCE_THRESHOLD"]