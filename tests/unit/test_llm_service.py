from __future__ import annotations

import pytest

from api.services.llm_service import AllProvidersFailedError
from api.services.llm_service import LLMProvider
from api.services.llm_service import LLMResponse
from api.services.llm_service import LLMService
from api.services.llm_service import ProviderAuthError
from api.services.llm_service import ProviderUnavailableError
from api.services.llm_service import get_llm_provider_health
from api.settings import get_settings


class ScriptedLLMService(LLMService):
    def __init__(self, script):
        self.script = script
        super().__init__(
            provider_clients={provider: object() for provider in LLMProvider},
            state_client=None,
            require_provider=True,
            use_state_store=False,
        )

    async def _call_provider(
        self,
        provider,
        system_prompt,
        user_message,
        temperature,
        max_tokens,
        response_format,
    ):
        next_result = self.script[provider].pop(0)
        if isinstance(next_result, Exception):
            raise next_result
        return LLMResponse(
            content=next_result,
            provider_used=provider.value,
            model_used=f"{provider.value}-model",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            latency_ms=1,
        )


@pytest.fixture(autouse=True)
def clear_settings(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "gemini,openai,anthropic")
    monkeypatch.delenv("REDIS_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_complete_falls_back_after_5xx_provider_failure():
    service = ScriptedLLMService(
        {
            LLMProvider.GEMINI: [ProviderUnavailableError("gemini 503", status_code=503)],
            LLMProvider.OPENAI: ['{"ok": true}'],
            LLMProvider.ANTHROPIC: ['{"unused": true}'],
        }
    )

    response = await service.complete("system", "user")

    assert response.provider_used == "openai"
    assert response.content == '{"ok": true}'


@pytest.mark.asyncio
async def test_complete_skips_open_circuit_provider():
    service = ScriptedLLMService(
        {
            LLMProvider.GEMINI: [
                ProviderUnavailableError("fail 1", status_code=503),
                ProviderUnavailableError("fail 2", status_code=503),
                ProviderUnavailableError("fail 3", status_code=503),
                '{"should_not_run": true}',
            ],
            LLMProvider.OPENAI: [
                ProviderUnavailableError("openai fail 1", status_code=503),
                ProviderUnavailableError("openai fail 2", status_code=503),
                ProviderUnavailableError("openai fail 3", status_code=503),
                '{"openai": true}',
            ],
            LLMProvider.ANTHROPIC: [
                '{"anthropic": true}',
                '{"anthropic_2": true}',
                '{"anthropic_3": true}',
                '{"anthropic_again": true}',
            ],
        }
    )

    for _ in range(3):
        await service.complete("system", "user")

    response = await service.complete("system", "user")

    assert response.provider_used == "anthropic"
    assert response.content == '{"anthropic_again": true}'


@pytest.mark.asyncio
async def test_auth_error_disables_provider_and_tries_next():
    service = ScriptedLLMService(
        {
            LLMProvider.GEMINI: [ProviderAuthError("bad key", status_code=401)],
            LLMProvider.OPENAI: ['{"fallback": true}'],
            LLMProvider.ANTHROPIC: ['{"unused": true}'],
        }
    )

    response = await service.complete("system", "user")

    assert response.provider_used == "openai"
    assert LLMProvider.GEMINI not in service._available_providers
    assert service._circuit_breakers[LLMProvider.GEMINI].current_state() == "OPEN"


@pytest.mark.parametrize(
    "message",
    [
        "API key not valid. Please pass a valid API key.",
        "invalid api key",
        "unauthenticated request",
        "permission denied",
    ],
)
def test_auth_like_provider_errors_are_mapped_to_auth_errors(message):
    with pytest.raises(ProviderAuthError):
        LLMService._raise_mapped_provider_error(RuntimeError(message))


def test_provider_health_marks_missing_keys_as_not_configured(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()

    health = {provider["name"]: provider for provider in get_llm_provider_health()}

    assert health["gemini"]["configured"] is True
    assert health["openai"]["configured"] is False
    assert health["openai"]["state"] == "CLOSED"
    assert health["openai"]["failures"] == 0
    assert health["anthropic"]["configured"] is False


@pytest.mark.asyncio
async def test_all_providers_failed_reports_tried_providers():
    service = ScriptedLLMService(
        {
            LLMProvider.GEMINI: [ProviderUnavailableError("gemini down", status_code=503)],
            LLMProvider.OPENAI: [ProviderUnavailableError("openai down", status_code=503)],
            LLMProvider.ANTHROPIC: [ProviderUnavailableError("anthropic down", status_code=503)],
        }
    )

    with pytest.raises(AllProvidersFailedError) as exc_info:
        await service.complete("system", "user")

    assert exc_info.value.providers_tried == ["gemini", "openai", "anthropic"]
    assert len(exc_info.value.errors) == 3
