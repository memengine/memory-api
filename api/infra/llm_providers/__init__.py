from api.infra.llm_providers.base import LLMProvider


def __getattr__(name: str):
    if name == "AnthropicProvider":
        from api.infra.llm_providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider
    if name == "CohereProvider":
        from api.infra.llm_providers.cohere_provider import CohereProvider

        return CohereProvider
    if name == "GeminiProvider":
        from api.infra.llm_providers.gemini_provider import GeminiProvider

        return GeminiProvider
    if name == "LocalProvider":
        from api.infra.llm_providers.local_provider import LocalProvider

        return LocalProvider
    if name == "OpenAIProvider":
        from api.infra.llm_providers.openai_provider import OpenAIProvider

        return OpenAIProvider
    raise AttributeError(name)


__all__ = [
    "AnthropicProvider",
    "CohereProvider",
    "GeminiProvider",
    "LLMProvider",
    "LocalProvider",
    "OpenAIProvider",
]
