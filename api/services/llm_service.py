from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from enum import Enum
from typing import Any

import httpx
import redis

from api.infra.circuit_breaker import CircuitBreaker
from api.settings import get_settings


LOGGER = logging.getLogger(__name__)


class LLMProvider(str, Enum):
    GEMINI = "gemini"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


@dataclass(slots=True)
class LLMResponse:
    content: str
    provider_used: str
    model_used: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: int


@dataclass(slots=True)
class ProviderConfig:
    provider: LLMProvider
    api_key: str
    model: str
    timeout_seconds: int


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderUnavailableError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderAuthError(ProviderError):
    pass


class AllProvidersFailedError(RuntimeError):
    def __init__(self, message: str, *, providers_tried: list[str], errors: list[str]) -> None:
        super().__init__(f"{message}; providers_tried={providers_tried}; errors={errors}")
        self.providers_tried = providers_tried
        self.errors = errors


def _build_state_client() -> redis.Redis | None:
    redis_url = (get_settings().redis_url or "").strip()
    if not redis_url:
        return None
    try:
        return redis.Redis.from_url(redis_url, decode_responses=True)
    except Exception:
        return None


class LLMService:
    """Multi-provider text completion with provider-level circuit breakers."""

    def __init__(
        self,
        *,
        provider_clients: dict[LLMProvider | str, Any] | None = None,
        state_client: redis.Redis | None = None,
        require_provider: bool = True,
        use_state_store: bool = True,
    ) -> None:
        self.settings = get_settings()
        self._provider_clients: dict[LLMProvider, Any] = {
            self._coerce_provider(provider): client
            for provider, client in (provider_clients or {}).items()
        }
        self._state_client = state_client if state_client is not None else (_build_state_client() if use_state_store else None)
        self._configs = self._build_provider_configs()
        self._available_providers = [config.provider for config in self._configs]
        self._circuit_breakers = {
            provider: CircuitBreaker(
                name=f"llm_{provider.value}",
                failure_threshold=3,
                window_seconds=60,
                recovery_timeout_seconds=60,
                state_client=self._state_client,
            )
            for provider in LLMProvider
        }

        if require_provider and not self._available_providers:
            raise RuntimeError("No LLM providers configured. Set at least one provider API key.")

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int = 2000,
        response_format: str = "json",
    ) -> LLMResponse:
        tried: list[str] = []
        errors: list[str] = []

        for provider in list(self._available_providers):
            config = self._config_for(provider)
            if config is None:
                continue

            breaker = self._circuit_breakers[provider]
            if breaker.current_state() == "OPEN":
                LOGGER.warning(
                    "llm_provider_skipped",
                    extra={"event": "llm_provider_skipped", "provider": provider.value, "reason": "circuit_open"},
                )
                continue

            tried.append(provider.value)
            started = time.perf_counter()
            try:
                response = await self._call_provider(
                    provider,
                    system_prompt,
                    user_message,
                    temperature,
                    max_tokens,
                    response_format,
                )
                breaker._record_success()
                LOGGER.info(
                    "llm_call_success",
                    extra={
                        "event": "llm_call_success",
                        "provider": provider.value,
                        "tokens": response.total_tokens,
                        "latency_ms": response.latency_ms,
                    },
                )
                return response
            except (asyncio.TimeoutError, TimeoutError, ProviderUnavailableError) as error:
                breaker._record_failure()
                status_code = getattr(error, "status_code", None)
                errors.append(f"{provider.value}: {error}")
                LOGGER.warning(
                    "llm_provider_failed",
                    extra={
                        "event": "llm_provider_failed",
                        "provider": provider.value,
                        "error": str(error),
                        "status_code": status_code,
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                    },
                )
                continue
            except ProviderRateLimitError as error:
                errors.append(f"{provider.value}: {error}")
                LOGGER.warning(
                    "llm_rate_limited",
                    extra={"event": "llm_rate_limited", "provider": provider.value},
                )
                continue
            except ProviderAuthError as error:
                breaker.force_open()
                errors.append(f"{provider.value}: {error}")
                LOGGER.error(
                    "llm_auth_failed",
                    extra={"event": "llm_auth_failed", "provider": provider.value},
                )
                self._available_providers = [candidate for candidate in self._available_providers if candidate != provider]
                continue

        raise AllProvidersFailedError(
            "All LLM providers failed or unavailable",
            providers_tried=tried,
            errors=errors,
        )

    def complete_sync(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int = 2000,
        response_format: str = "json",
    ) -> LLMResponse:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.complete(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
            )

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.complete(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
            )
        finally:
            loop.close()

    async def _call_provider(
        self,
        provider: LLMProvider,
        system_prompt: str,
        user_message: str,
        temperature: float,
        max_tokens: int,
        response_format: str,
    ) -> LLMResponse:
        if provider == LLMProvider.GEMINI:
            return await self._call_gemini(system_prompt, user_message, temperature, max_tokens, response_format)
        if provider == LLMProvider.OPENAI:
            return await self._call_openai(system_prompt, user_message, temperature, max_tokens, response_format)
        if provider == LLMProvider.ANTHROPIC:
            return await self._call_anthropic(system_prompt, user_message, temperature, max_tokens, response_format)
        raise ProviderUnavailableError(f"Unsupported provider: {provider.value}")

    async def _call_gemini(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float,
        max_tokens: int,
        response_format: str,
    ) -> LLMResponse:
        config = self._require_config(LLMProvider.GEMINI)
        started = time.perf_counter()

        async def invoke() -> Any:
            client = self._provider_clients.get(LLMProvider.GEMINI)
            if client is not None:
                from google.genai import types

                return await asyncio.to_thread(
                    client.models.generate_content,
                    model=config.model,
                    contents=f"{system_prompt}\n\n{user_message}",
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                        response_mime_type="application/json" if response_format == "json" else "text/plain",
                    ),
                )

            from google import genai
            from google.genai import types

            genai_client = genai.Client(api_key=config.api_key)
            return await genai_client.aio.models.generate_content(
                model=config.model,
                contents=f"{system_prompt}\n\n{user_message}",
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                    response_mime_type="application/json" if response_format == "json" else "text/plain",
                ),
            )

        try:
            response = await asyncio.wait_for(invoke(), timeout=config.timeout_seconds)
        except Exception as error:
            self._raise_mapped_provider_error(error)
        usage = getattr(response, "usage_metadata", None)
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        total_tokens = int(getattr(usage, "total_token_count", input_tokens + output_tokens) or 0)
        return LLMResponse(
            content=str(getattr(response, "text", "") or "{}"),
            provider_used=LLMProvider.GEMINI.value,
            model_used=config.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _call_openai(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float,
        max_tokens: int,
        response_format: str,
    ) -> LLMResponse:
        config = self._require_config(LLMProvider.OPENAI)
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": config.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}

        try:
            response = await asyncio.wait_for(
                self._post_json(
                    "https://api.openai.com/v1/chat/completions",
                    config.api_key,
                    payload,
                ),
                timeout=config.timeout_seconds,
            )
        except Exception as error:
            self._raise_mapped_provider_error(error)

        choice = (response.get("choices") or [{}])[0]
        usage = response.get("usage") or {}
        return LLMResponse(
            content=str(((choice.get("message") or {}).get("content")) or "{}"),
            provider_used=LLMProvider.OPENAI.value,
            model_used=config.model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _call_anthropic(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float,
        max_tokens: int,
        response_format: str,
    ) -> LLMResponse:
        config = self._require_config(LLMProvider.ANTHROPIC)
        started = time.perf_counter()
        if response_format == "json":
            system_prompt = f"{system_prompt}\n\nRespond with valid JSON only. No markdown, no explanation."
        payload = {
            "model": config.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        }
        headers = {
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                http_response = await asyncio.wait_for(
                    client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload),
                    timeout=config.timeout_seconds,
                )
            self._raise_for_status(http_response)
            response = http_response.json()
        except Exception as error:
            self._raise_mapped_provider_error(error)

        content_blocks = response.get("content") or []
        text = "".join(str(block.get("text") or "") for block in content_blocks if isinstance(block, dict))
        usage = response.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        return LLMResponse(
            content=text or "{}",
            provider_used=LLMProvider.ANTHROPIC.value,
            model_used=config.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _post_json(self, url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
                json=payload,
            )
        self._raise_for_status(response)
        return response.json()

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        message = response.text[:500]
        if response.status_code in {401, 403}:
            raise ProviderAuthError(message, status_code=response.status_code)
        if response.status_code == 429:
            raise ProviderRateLimitError(message, status_code=response.status_code)
        if response.status_code >= 500:
            raise ProviderUnavailableError(message, status_code=response.status_code)
        raise ProviderUnavailableError(message, status_code=response.status_code)

    @staticmethod
    def _raise_mapped_provider_error(error: Exception) -> None:
        if isinstance(error, (ProviderAuthError, ProviderRateLimitError, ProviderUnavailableError)):
            raise error
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            raise TimeoutError("provider request timed out") from error
        status_code = getattr(error, "status_code", None) or getattr(error, "code", None)
        message = str(error)
        message_lower = message.lower()
        if status_code in {401, 403}:
            raise ProviderAuthError(message, status_code=int(status_code)) from error
        if any(
            token in message_lower
            for token in (
                "api key not valid",
                "invalid api key",
                "invalid api_key",
                "permission denied",
                "unauthenticated",
                "authentication",
                "unauthorized",
            )
        ):
            raise ProviderAuthError(message) from error
        if status_code == 429:
            raise ProviderRateLimitError(message, status_code=429) from error
        if status_code is not None and int(status_code) >= 500:
            raise ProviderUnavailableError(message, status_code=int(status_code)) from error
        if any(token in message for token in ("503", "500", "502", "504")):
            raise ProviderUnavailableError(message, status_code=503) from error
        raise ProviderUnavailableError(message) from error

    def _build_provider_configs(self) -> list[ProviderConfig]:
        configs = {
            LLMProvider.GEMINI: ProviderConfig(
                provider=LLMProvider.GEMINI,
                api_key=self.settings.gemini_api_key,
                model=self.settings.gemini_model or self.settings.extraction_model or "gemini-2.5-flash",
                timeout_seconds=int(self.settings.gemini_timeout_seconds or 30),
            ),
            LLMProvider.OPENAI: ProviderConfig(
                provider=LLMProvider.OPENAI,
                api_key=self.settings.openai_api_key,
                model=self.settings.extraction_model or self.settings.openai_model or "gpt-4o-mini",
                timeout_seconds=int(self.settings.openai_timeout_seconds or 30),
            ),
            LLMProvider.ANTHROPIC: ProviderConfig(
                provider=LLMProvider.ANTHROPIC,
                api_key=self.settings.anthropic_api_key,
                model=self.settings.anthropic_model or "claude-haiku-4-5-20251001",
                timeout_seconds=int(self.settings.anthropic_timeout_seconds or 30),
            ),
        }

        ordered: list[ProviderConfig] = []
        for provider_name in self._provider_order():
            provider = self._coerce_provider(provider_name)
            config = configs[provider]
            if config.api_key or provider in self._provider_clients:
                ordered.append(config)
        return ordered

    def _provider_order(self) -> list[str]:
        raw_order = self.settings.llm_provider_order or "openai"
        provider_names = [name.strip().lower() for name in raw_order.split(",") if name.strip()]
        return provider_names or ["openai"]

    def _config_for(self, provider: LLMProvider) -> ProviderConfig | None:
        return next((config for config in self._configs if config.provider == provider), None)

    def _require_config(self, provider: LLMProvider) -> ProviderConfig:
        config = self._config_for(provider)
        if config is None:
            raise ProviderUnavailableError(f"{provider.value} is not configured")
        return config

    @staticmethod
    def _coerce_provider(provider: LLMProvider | str) -> LLMProvider:
        return provider if isinstance(provider, LLMProvider) else LLMProvider(str(provider).lower())


def get_llm_provider_health() -> list[dict[str, object]]:
    settings = get_settings()
    configured_providers = {
        LLMProvider.GEMINI: bool((settings.gemini_api_key or "").strip()),
        LLMProvider.OPENAI: bool((settings.openai_api_key or "").strip()),
        LLMProvider.ANTHROPIC: bool((settings.anthropic_api_key or "").strip()),
    }
    state_client = _build_state_client()
    health: list[dict[str, object]] = []
    for provider in LLMProvider:
        if not configured_providers[provider]:
            health.append(
                {
                    "name": provider.value,
                    "state": "CLOSED",
                    "failures": 0,
                    "configured": False,
                    "last_failure_at": None,
                }
            )
            continue

        breaker = CircuitBreaker(
            name=f"llm_{provider.value}",
            failure_threshold=3,
            window_seconds=60,
            recovery_timeout_seconds=60,
            state_client=state_client,
        )
        snapshot = breaker.snapshot()
        opened_at = float(snapshot.get("opened_at") or 0.0)
        last_failure_at = (
            datetime.fromtimestamp(opened_at, tz=UTC)
            if opened_at > 0
            else None
        )
        health.append(
            {
                "name": provider.value,
                "state": snapshot["state"],
                "failures": snapshot["failure_count"],
                "configured": True,
                "last_failure_at": last_failure_at,
            }
        )
    return health


__all__ = [
    "AllProvidersFailedError",
    "LLMProvider",
    "LLMResponse",
    "LLMService",
    "ProviderAuthError",
    "ProviderRateLimitError",
    "ProviderUnavailableError",
    "get_llm_provider_health",
]
