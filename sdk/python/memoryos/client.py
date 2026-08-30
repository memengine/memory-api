from __future__ import annotations

import time
import warnings
from datetime import datetime
from typing import Any, Self
from urllib.parse import quote

import httpx

from memoryos.errors import MemoryOSError, map_sdk_error
from memoryos.types import (
    AddEnvelope,
    AddRequest,
    AddResult,
    ConversationMessage,
    DeleteEnvelope,
    EdTechMemoryProfile,
    EdTechProfileEnvelope,
    ExportEnvelope,
    MemoryExport,
    MemoryJobStatus,
    MemoryJobStatusEnvelope,
    MemoryListEnvelope,
    MemoryPage,
    MemorySource,
    QuotaMode,
    RetrievalFeedbackEnvelope,
    RetrievalFeedbackRequest,
    RetrievalFeedbackResult,
    RetrieveEnvelope,
    RetrieveResult,
)


class Memory:
    DEFAULT_BASE_URL = "https://api.memoryo.dev"
    DEFAULT_TIMEOUT = 30.0
    MAX_RETRIES = 3

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be empty.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={
                "Authorization": f"ApiKey {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "memoryos-python/0.1.0",
            },
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _resolve_external_user_id(
        external_user_id: str | None,
        kwargs: dict[str, Any],
    ) -> str:
        deprecated_user_id = kwargs.pop("user_id", None)
        if deprecated_user_id is not None:
            warnings.warn(
                "user_id is deprecated, use external_user_id",
                DeprecationWarning,
                stacklevel=2,
            )
            if external_user_id is None:
                external_user_id = str(deprecated_user_id)
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Unexpected keyword argument(s): {unexpected}")
        if external_user_id is None:
            raise TypeError("external_user_id is required")
        return external_user_id

    @staticmethod
    def source(
        service: str,
        *,
        event_id: str | None = None,
        observed_at: Any | None = None,
        scope: dict[str, Any] | None = None,
        evidence: list[Any] | None = None,
    ) -> MemorySource:
        """Create source metadata for multi-service memory writes.

        Most apps can omit source in add(). Use this helper when Billing,
        Support, CRM, or another service needs explicit provenance.
        """
        return MemorySource.for_service(
            service,
            event_id=event_id,
            observed_at=observed_at,
            scope=scope,
            evidence=evidence,
        )

    def add(
        self,
        messages: list[dict[str, str]] | list[ConversationMessage],
        external_user_id: str,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        source: MemorySource | dict[str, Any] | None = None,
    ) -> AddResult:
        payload = AddRequest(
            external_user_id=external_user_id,
            agent_id=agent_id,
            messages=[item if isinstance(item, ConversationMessage) else ConversationMessage.model_validate(item) for item in messages],
            metadata=metadata or {},
            source=(
                source
                if isinstance(source, MemorySource)
                else MemorySource.model_validate(source)
                if source is not None
                else None
            ),
        )
        response = self._request_response(
            "POST",
            "/v1/memories/add",
            json=payload.model_dump(mode="json", exclude_none=True),
            headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )
        parsed = AddEnvelope.model_validate(self._parse_json(response))
        return AddResult(
            job_id=parsed.job_id,
            status=parsed.status,
            blocked_reason=parsed.blocked_reason,
            retry_after_seconds=parsed.retry_after_seconds,
            budget_remaining_pct=parsed.budget_remaining_pct,
            quota_mode=self._quota_mode_from_response(response),
            processing_eta_seconds=parsed.processing_eta_seconds,
            processing_status=self._processing_status_from_response(response, parsed.processing_status),
            circuit_status=self._circuit_status_from_response(response),
            nothing_to_extract=parsed.nothing_to_extract,
        )

    def get(
        self,
        query: str,
        external_user_id: str,
        limit: int = 10,
        categories: list[str] | None = None,
        agent_id: str | None = None,
        time_filter_days: int | None = None,
        format: str = "bullets",
        context_max_tokens: int = 500,
        as_of: datetime | None = None,
    ) -> RetrieveResult:
        body: dict[str, Any] = {
            "query": query,
            "external_user_id": external_user_id,
            "limit": limit,
            "categories": categories or [],
            "format": format,
            "context_max_tokens": context_max_tokens,
        }
        if agent_id is not None:
            body["agent_id"] = agent_id
        if time_filter_days is not None:
            body["time_filter_days"] = time_filter_days
        if as_of is not None:
            if as_of.tzinfo is None:
                raise ValueError("as_of must include a timezone")
            body["as_of"] = as_of.isoformat()
        response = self._request_response(
            "POST",
            "/v1/memories/retrieve",
            json=body,
        )
        parsed = RetrieveEnvelope.model_validate(self._parse_json(response))
        quota_mode = parsed.quota_mode or self._quota_mode_from_response(response)
        return RetrieveResult(
            retrieval_id=parsed.retrieval_id,
            items=parsed.data,
            cached=parsed.cached,
            system_prompt_addition=parsed.system_prompt_addition,
            context_token_count=parsed.context_token_count,
            memories_from_hot_tier=parsed.memories_from_hot_tier,
            quota_mode=quota_mode,
            is_passthrough=quota_mode == "PASSTHROUGH",
            is_degraded=quota_mode == "DEGRADED_RETRIEVE",
            circuit_status=self._circuit_status_from_response(response),
            clarification_question=parsed.clarification_question,
        )

    def get_job_status(self, job_id: str) -> MemoryJobStatus:
        """Return the current state of an asynchronous extraction job."""
        if not str(job_id or "").strip():
            raise ValueError("job_id must not be empty")
        response = self._request("GET", f"/v1/memories/jobs/{quote(job_id, safe='')}")
        return MemoryJobStatusEnvelope.model_validate(response).data

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 120.0,
        poll_interval: float = 1.0,
    ) -> MemoryJobStatus:
        """Poll until extraction completes or fails, raising on timeout."""
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job_status(job_id)
            if job.status in {"completed", "failed", "dead_letter", "dead_lettered", "cancelled"}:
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(f"MemoryOS job {job_id} did not finish within {timeout:g} seconds")
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    def feedback(
        self,
        retrieval_id: str,
        outcome: str,
        used_memory_ids: list[str] | None = None,
        correction: str | None = None,
        agent_confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RetrievalFeedbackResult:
        payload = RetrievalFeedbackRequest(
            retrieval_id=retrieval_id,
            outcome=outcome,  # type: ignore[arg-type]
            used_memory_ids=used_memory_ids or [],
            correction=correction,
            agent_confidence=agent_confidence,
            metadata=metadata or {},
        )
        response = self._request_response(
            "POST",
            "/v1/memories/retrieval-feedback",
            json=payload.model_dump(mode="json", exclude_none=True),
        )
        parsed = RetrievalFeedbackEnvelope.model_validate(self._parse_json(response))
        return parsed.data

    def delete(
        self,
        memory_id: str,
        external_user_id: str | None = None,
        hard_delete: bool = False,
        **kwargs: Any,
    ) -> bool:
        # Deletion is tenant-scoped by memory_id. external_user_id/user_id is
        # retained only for compatibility with older SDK call sites.
        _ = external_user_id
        _ = kwargs.pop("user_id", None)
        if kwargs:
            raise TypeError(f"Unexpected keyword arguments: {', '.join(sorted(kwargs))}")
        response = self._request(
            "DELETE",
            f"/v1/memories/{memory_id}",
            params={"hard_delete": str(hard_delete).lower()},
        )
        return DeleteEnvelope.model_validate(response).data.deleted

    def list(
        self,
        external_user_id: str | None = None,
        page_cursor: str | None = None,
        limit: int = 50,
        **kwargs: Any,
    ) -> MemoryPage:
        resolved_external_user_id = self._resolve_external_user_id(external_user_id, kwargs)
        params: dict[str, Any] = {
            "external_user_id": resolved_external_user_id,
            "limit": limit,
        }
        if page_cursor:
            params["cursor"] = page_cursor
        response = self._request("GET", "/v1/memories", params=params)
        parsed = MemoryListEnvelope.model_validate(response)
        return MemoryPage(
            items=parsed.data,
            next_cursor=parsed.pagination.next_cursor,
            limit=parsed.pagination.limit,
            total=parsed.pagination.total,
        )

    def export(self, external_user_id: str | None = None, **kwargs: Any) -> MemoryExport:
        resolved_external_user_id = self._resolve_external_user_id(external_user_id, kwargs)
        response = self._request(
            "GET",
            f"/v1/users/{quote(resolved_external_user_id, safe='')}/export",
        )
        return ExportEnvelope.model_validate(response).data

    def get_edtech_profile(self, external_user_id: str) -> EdTechMemoryProfile | None:
        """Return the structured EdTech profile for a user, if enabled."""

        response = self._request(
            "GET",
            "/v1/memories/edtech-profile",
            params={"external_user_id": external_user_id},
        )
        return EdTechProfileEnvelope.model_validate(response).data

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self._request_response(method, path, json=json, params=params, headers=headers)
        return self._parse_json(response)

    def _request_response(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        last_error: MemoryOSError | None = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = self._client.request(method, path, json=json, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise MemoryOSError(f"MemoryOS request failed: {exc}") from exc

            if response.status_code < 400:
                return response

            error = self._error_from_response(response)
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.MAX_RETRIES:
                last_error = error
                time.sleep(2**attempt)
                continue
            raise error

        raise last_error or MemoryOSError("Request failed after retries.")

    @staticmethod
    def _parse_json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise MemoryOSError("MemoryOS returned a non-JSON response.", status_code=response.status_code) from exc
        if not isinstance(payload, dict):
            raise MemoryOSError("MemoryOS returned an unexpected response payload.", status_code=response.status_code)
        return payload

    def _error_from_response(self, response: httpx.Response) -> MemoryOSError:
        try:
            payload = self._parse_json(response)
        except MemoryOSError:
            return map_sdk_error(
                status_code=response.status_code,
                message=f"MemoryOS request failed with status {response.status_code}.",
            )

        error_payload = payload.get("error", "request_failed")
        message = str(error_payload).replace("_", " ")
        return map_sdk_error(
            status_code=response.status_code,
            message=message,
            code=str(payload.get("code")) if payload.get("code") else None,
            request_id=str(payload.get("request_id")) if payload.get("request_id") else None,
            details=payload.get("details"),
        )

    @staticmethod
    def _quota_mode_from_response(response: httpx.Response) -> QuotaMode:
        raw_mode = response.headers.get("X-MemoryOS-Quota-Mode", "FULL").upper()
        if raw_mode in {"FULL", "PASSTHROUGH", "DEGRADED_RETRIEVE", "BLOCKED"}:
            return raw_mode  # type: ignore[return-value]
        return "FULL"

    @staticmethod
    def _circuit_status_from_response(response: httpx.Response) -> str:
        raw_status = response.headers.get("X-MemoryOS-Circuit-Status", "HEALTHY").upper()
        if raw_status in {"HEALTHY", "DEGRADED", "CRITICAL"}:
            return raw_status
        return "HEALTHY"

    @staticmethod
    def _processing_status_from_response(response: httpx.Response, fallback: str = "normal") -> str:
        raw_status = response.headers.get("X-MemoryOS-Processing", fallback).lower()
        if raw_status in {"normal", "delayed"}:
            return raw_status
        return "normal"
