from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel
from pydantic import Field

from memoryos.errors import MemoryOSError
from memoryos.errors import map_sdk_error
from memoryos.types import AddEnvelope
from memoryos.types import AddResult
from memoryos.types import CircuitStatus
from memoryos.types import ConversationMessage
from memoryos.types import MemoryResult as SDKMemoryResult
from memoryos.types import QuotaMode
from memoryos.types import RetrieveResult


class UniversalRetrieveEnvelope(BaseModel):
    request_id: str
    timestamp: datetime
    data: list[SDKMemoryResult]
    cached: bool
    system_prompt_addition: str
    permission_error: str | None = None
    categories_available: list[str] = Field(default_factory=list)
    is_passthrough: bool = False


class UniversalRetrieveResult(RetrieveResult):
    permission_status: str | None = None
    categories_available: list[str] = Field(default_factory=list)


class UniversalMemory:
    DEFAULT_BASE_URL = "https://api.memoryos.io"
    DEFAULT_CONSENT_BASE_URL = "https://consent.memoryos.io"
    DEFAULT_TIMEOUT = 30.0
    MAX_RETRIES = 3

    def __init__(
        self,
        agent_api_key: str,
        uui_token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int | float = DEFAULT_TIMEOUT,
    ) -> None:
        if not agent_api_key.strip():
            raise ValueError("agent_api_key must not be empty.")
        if not uui_token.strip():
            raise ValueError("uui_token must not be empty.")
        self.agent_api_key = agent_api_key
        self.uui_token = uui_token
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={
                "Authorization": f"ApiKey {self.agent_api_key}",
                "X-MemoryOS-UUI": self.uui_token,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "memoryos-python/0.1.0",
            },
        )

    def __enter__(self) -> UniversalMemory:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def add(
        self,
        messages: list[dict[str, str]] | list[ConversationMessage],
        metadata: dict[str, Any] | None = None,
    ) -> AddResult:
        response = self._request_response(
            "POST",
            "/v1/universal/memories/add",
            json={
                "messages": [
                    item.model_dump(mode="json")
                    if isinstance(item, ConversationMessage)
                    else ConversationMessage.model_validate(item).model_dump(mode="json")
                    for item in messages
                ],
                "metadata": metadata or {},
            },
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
        )

    def get(self, query: str, limit: int = 10) -> UniversalRetrieveResult:
        response = self._request_response(
            "POST",
            "/v1/universal/memories/retrieve",
            json={
                "query": query,
                "limit": limit,
                "format": "bullets",
            },
        )
        parsed = UniversalRetrieveEnvelope.model_validate(self._parse_json(response))
        quota_mode = self._quota_mode_from_response(response)
        return UniversalRetrieveResult(
            items=parsed.data,
            cached=parsed.cached,
            system_prompt_addition=parsed.system_prompt_addition,
            quota_mode=quota_mode,
            is_passthrough=parsed.is_passthrough or quota_mode == "PASSTHROUGH",
            is_degraded=quota_mode == "DEGRADED_RETRIEVE",
            circuit_status=self._circuit_status_from_response(response),
            permission_status=parsed.permission_error,
            categories_available=parsed.categories_available,
        )

    @staticmethod
    def consent_url(
        agent_id: str,
        redirect_uri: str | None = None,
        state: str | None = None,
        categories: list[str] | None = None,
        link_token: str | None = None,
    ) -> str:
        if not str(agent_id or "").strip():
            raise ValueError("agent_id must not be empty.")

        query = {"agent_id": agent_id}
        if redirect_uri is not None and str(redirect_uri).strip():
            query["redirect_uri"] = redirect_uri
        if state is not None:
            query["state"] = state
        if link_token is not None and str(link_token).strip():
            query["link_token"] = link_token
        if categories:
            cleaned_categories = list(dict.fromkeys(str(category).strip() for category in categories if str(category).strip()))
            if cleaned_categories:
                query["categories"] = ",".join(cleaned_categories)
        return f"{UniversalMemory.DEFAULT_CONSENT_BASE_URL}/consent?{urlencode(query)}"

    def _request_response(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        last_error: MemoryOSError | None = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = self._client.request(method, path, json=json, params=params)
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
    def _circuit_status_from_response(response: httpx.Response) -> CircuitStatus:
        raw_status = response.headers.get("X-MemoryOS-Circuit-Status", "HEALTHY").upper()
        if raw_status in {"HEALTHY", "DEGRADED", "CRITICAL"}:
            return raw_status  # type: ignore[return-value]
        return "HEALTHY"

    @staticmethod
    def _processing_status_from_response(response: httpx.Response, fallback: str = "normal") -> str:
        raw_status = response.headers.get("X-MemoryOS-Processing", fallback).lower()
        if raw_status in {"normal", "delayed"}:
            return raw_status
        return "normal"
