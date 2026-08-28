from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self

import httpx

from benchmarks.public.longmemeval.contract import (
    LongMemEvalCase,
    parse_longmemeval_datetime,
)

TERMINAL_JOB_STATES = {"completed", "dead", "blocked"}


@dataclass(frozen=True, slots=True)
class RetrievedEvidence:
    memory_id: str
    content: str
    relevance_score: float
    source_event_id: str | None
    provenance: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    evidence: list[RetrievedEvidence]
    system_prompt_addition: str
    context_token_count: int
    latency_ms: float


class MemoryOSLongMemEvalAdapter:
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

    def external_user_id(self, case: LongMemEvalCase) -> str:
        return f"longmemeval-{self.run_id}-{case.question_id}"

    async def ingest_case(
        self,
        case: LongMemEvalCase,
        *,
        completed_sessions: dict[str, dict[str, Any]] | None = None,
        on_session_completed: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        completed_sessions = completed_sessions or {}
        for session_id, observed_at, turns in zip(
            case.haystack_session_ids,
            case.haystack_dates,
            case.haystack_sessions,
            strict=True,
        ):
            if session_id in completed_sessions:
                outcomes.append(dict(completed_sessions[session_id]))
                continue
            event_id = self._event_id(case.question_id, session_id)
            normalized_turns = [
                {"role": turn.role, "content": turn.content.strip()}
                for turn in turns
                if turn.content.strip()
            ]
            canonical = json.dumps(
                normalized_turns,
                sort_keys=True,
                separators=(",", ":"),
            )
            payload = {
                "external_user_id": self.external_user_id(case),
                "agent_id": self.agent_id,
                "messages": normalized_turns,
                "metadata": {
                    "public_benchmark": "longmemeval",
                    "run_id": self.run_id,
                },
                "source": {
                    "event_id": event_id,
                    "service": "longmemeval",
                    "observed_at": parse_longmemeval_datetime(observed_at).isoformat(),
                    "scope": {"benchmark_session_id": session_id},
                    "evidence": [
                        {
                            "source_type": "longmemeval-session",
                            "reference": session_id,
                            "content_hash": hashlib.sha256(
                                canonical.encode()
                            ).hexdigest(),
                        }
                    ],
                },
            }
            started = time.perf_counter()
            body = await self._queue_session(payload=payload, event_id=event_id)
            job_id = body.get("job_id")
            if not job_id:
                raise RuntimeError(
                    f"session {session_id} did not queue: {body.get('status', 'unknown')}"
                )
            job = await self._wait_for_job(str(job_id))
            if job.get("status") != "completed":
                diagnostic = {
                    "job_id": str(job_id),
                    "status": job.get("status"),
                    "attempts": job.get("attempts"),
                    "error": job.get("error"),
                    "error_summary": job.get("error_summary"),
                    "queue_name": job.get("queue_name"),
                    "extraction_metadata": job.get("extraction_metadata") or {},
                }
                raise RuntimeError(
                    f"session {session_id} failed: "
                    + json.dumps(diagnostic, sort_keys=True, default=str)
                )
            outcome = {
                "session_id": session_id,
                "event_id": event_id,
                "job_id": str(job_id),
                "memories_created": int(job.get("memories_created", 0)),
                "attempts": int(job.get("attempts", 0)),
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            outcomes.append(outcome)
            if on_session_completed is not None:
                on_session_completed(session_id, outcome)
        return outcomes

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

    async def retrieve(
        self,
        case: LongMemEvalCase,
        *,
        limit: int,
        context_max_tokens: int,
    ) -> RetrievalOutcome:
        started = time.perf_counter()
        payload = {
                "external_user_id": self.external_user_id(case),
                "agent_id": self.agent_id,
                "query": case.question,
                "limit": limit,
                "format": "bullets",
                "context_max_tokens": context_max_tokens,
            }
        for attempt in range(3):
            response = await self.client.post("/v1/memories/retrieve", json=payload)
            if response.status_code != 503 or not self._is_embedding_unavailable(response):
                break
            if attempt == 2:
                break
            await asyncio.sleep(2**attempt)
        response.raise_for_status()
        body = response.json()
        return RetrievalOutcome(
            evidence=[
                RetrievedEvidence(
                    memory_id=str(item["id"]),
                    content=str(item["content"]),
                    relevance_score=float(item.get("relevance_score", 0.0)),
                    source_event_id=item.get("source_event_id"),
                    provenance=item.get("provenance"),
                )
                for item in body.get("data", [])
            ],
            system_prompt_addition=str(body.get("system_prompt_addition", "")),
            context_token_count=int(body.get("context_token_count", 0)),
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )

    @staticmethod
    def _is_embedding_unavailable(response: httpx.Response) -> bool:
        try:
            return response.json().get("code") == "EMB_503"
        except (TypeError, ValueError):
            return False

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

    def _event_id(self, question_id: str, session_id: str) -> str:
        digest = hashlib.sha256(
            f"{self.run_id}:{question_id}:{session_id}".encode()
        ).hexdigest()[:32]
        return f"lme-{digest}"


def evidence_session_ids(
    case: LongMemEvalCase, evidence: list[RetrievedEvidence]
) -> list[str]:
    # The public session id is carried only in source provenance. The database
    # source_event_id is intentionally opaque and cannot be used as a label leak.
    found: list[str] = []
    for item in evidence:
        scope = (item.provenance or {}).get("scope") or {}
        session_id = scope.get("benchmark_session_id")
        if session_id in case.haystack_session_ids and session_id not in found:
            found.append(str(session_id))
            continue
    return found
