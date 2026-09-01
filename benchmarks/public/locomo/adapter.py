from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Self

import httpx

from benchmarks.public.locomo.contract import LoCoMoQuestion, LoCoMoSample

TERMINAL_JOB_STATES = {"completed", "dead", "blocked"}


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    memory_id: str
    content: str
    relevance_score: float
    source_event_id: str | None
    provenance: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    memories: list[RetrievedMemory]
    context_token_count: int
    latency_ms: float


class MemoryOSLoCoMoAdapter:
    """Transport LoCoMo conversations through the normal MemoryOS API path."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        run_id: str,
        agent_id: str | None = None,
        request_timeout_seconds: float = 30.0,
        job_timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        if not api_key.strip():
            raise ValueError("benchmark API key is required")
        self.run_id = run_id
        self.agent_id = agent_id
        self.job_timeout_seconds = job_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"ApiKey {api_key}"},
            timeout=request_timeout_seconds,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.client.aclose()

    def external_user_id(self, sample: LoCoMoSample) -> str:
        return f"locomo-{self.run_id}-{sample.sample_id}"

    def session_payload(
        self,
        sample: LoCoMoSample,
        *,
        session_number: int,
    ) -> tuple[dict[str, Any], str]:
        sessions = {
            number: (observed_at, turns)
            for number, observed_at, turns in sample.conversation.sessions()
        }
        if session_number not in sessions:
            raise ValueError(f"unknown LoCoMo session: {session_number}")
        observed_at, turns = sessions[session_number]
        event_id = self._event_id(sample.sample_id, session_number)
        dialog_ids = [turn.dia_id for turn in turns]
        messages = [
            {
                "role": "user"
                if turn.speaker == sample.conversation.speaker_a
                else "assistant",
                "content": f"[{turn.speaker}; dialog {turn.dia_id}] {turn.text.strip()}",
            }
            for turn in turns
        ]
        canonical = json.dumps(
            messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        payload = {
            "external_user_id": self.external_user_id(sample),
            "agent_id": self.agent_id,
            "messages": messages,
            "metadata": {
                "public_benchmark": "locomo",
                "run_id": self.run_id,
                "sample_id": sample.sample_id,
            },
            "source": {
                "event_id": event_id,
                "service": "locomo",
                "observed_at": observed_at.isoformat(),
                "scope": {
                    "benchmark_sample_id": sample.sample_id,
                    "benchmark_session_number": session_number,
                    # These identify candidate source turns for the event. They are
                    # not claimed as exact per-memory attribution.
                    "benchmark_dialog_ids": dialog_ids,
                },
                "evidence": [
                    {
                        "source_type": "locomo-session",
                        "reference": f"{sample.sample_id}:session_{session_number}",
                        "content_hash": hashlib.sha256(
                            canonical.encode("utf-8")
                        ).hexdigest(),
                    }
                ],
            },
        }
        return payload, event_id

    async def ingest_sample(self, sample: LoCoMoSample) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for session_number, _observed_at, _turns in sample.conversation.sessions():
            payload, event_id = self.session_payload(
                sample, session_number=session_number
            )
            started = time.perf_counter()
            body = await self._queue_session(payload=payload, event_id=event_id)
            job_id = body.get("job_id")
            if not job_id:
                raise RuntimeError(f"session_{session_number} did not return a job_id")
            job = await self._wait_for_job(str(job_id))
            if job.get("status") != "completed":
                raise RuntimeError(
                    f"session_{session_number} failed: "
                    + json.dumps(
                        {
                            "job_id": str(job_id),
                            "status": job.get("status"),
                            "attempts": job.get("attempts"),
                            "error": job.get("error"),
                            "error_summary": job.get("error_summary"),
                        },
                        sort_keys=True,
                        default=str,
                    )
                )
            outcomes.append(
                {
                    "session_number": session_number,
                    "event_id": event_id,
                    "job_id": str(job_id),
                    "memories_created": int(job.get("memories_created", 0)),
                    "attempts": int(job.get("attempts", 0)),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
        return outcomes

    async def retrieve(
        self,
        sample: LoCoMoSample,
        question: LoCoMoQuestion,
        *,
        limit: int,
        context_max_tokens: int,
    ) -> RetrievalOutcome:
        started = time.perf_counter()
        response = await self.client.post(
            "/v1/memories/retrieve",
            json={
                "external_user_id": self.external_user_id(sample),
                "agent_id": self.agent_id,
                "query": question.question,
                "limit": limit,
                "format": "bullets",
                "context_max_tokens": context_max_tokens,
            },
        )
        response.raise_for_status()
        body = response.json()
        return RetrievalOutcome(
            memories=[
                RetrievedMemory(
                    memory_id=str(item["id"]),
                    content=str(item["content"]),
                    relevance_score=float(item.get("relevance_score", 0.0)),
                    source_event_id=item.get("source_event_id"),
                    provenance=item.get("provenance"),
                )
                for item in body.get("data", [])
            ],
            context_token_count=int(body.get("context_token_count", 0)),
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )

    async def _queue_session(
        self, *, payload: dict[str, Any], event_id: str
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.job_timeout_seconds
        while time.monotonic() < deadline:
            response = await self.client.post(
                "/v1/memories/add",
                json=payload,
                headers={"Idempotency-Key": event_id},
            )
            response.raise_for_status()
            body = response.json()
            if body.get("job_id"):
                return body
            if body.get("status") == "L1" and body.get("retry_after_seconds"):
                await asyncio.sleep(max(1, int(body["retry_after_seconds"])))
                continue
            raise RuntimeError(
                "session did not queue: "
                + json.dumps(
                    {
                        "status": body.get("status"),
                        "blocked_reason": body.get("blocked_reason"),
                        "retry_after_seconds": body.get("retry_after_seconds"),
                    },
                    sort_keys=True,
                )
            )
        raise TimeoutError("session queueing exceeded the benchmark deadline")

    async def _wait_for_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.job_timeout_seconds
        while time.monotonic() < deadline:
            response = await self.client.get(f"/v1/memories/jobs/{job_id}")
            response.raise_for_status()
            job = response.json().get("data", {})
            if job.get("status") in TERMINAL_JOB_STATES:
                return job
            await asyncio.sleep(self.poll_interval_seconds)
        raise TimeoutError(f"extraction job {job_id} exceeded the benchmark deadline")

    def _event_id(self, sample_id: str, session_number: int) -> str:
        digest = hashlib.sha256(
            f"{self.run_id}:{sample_id}:{session_number}".encode()
        ).hexdigest()[:32]
        return f"locomo-{digest}"


def candidate_dialog_ids(memories: list[RetrievedMemory]) -> list[str]:
    found: list[str] = []
    for memory in memories:
        scope = (memory.provenance or {}).get("scope") or {}
        for dialog_id in scope.get("benchmark_dialog_ids") or []:
            value = str(dialog_id)
            if value not in found:
                found.append(value)
    return found
