from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices
from pydantic import Field
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    app_version: str = Field(
        default="dev",
        validation_alias=AliasChoices("APP_VERSION", "API_VERSION"),
    )
    sentry_dsn: str = Field(default="", alias="SENTRY_DSN")
    sentry_send_default_pii: bool = Field(default=False, alias="SENTRY_SEND_DEFAULT_PII")
    database_url: str = Field(default="", alias="DATABASE_URL")
    redis_url: str = Field(default="", alias="REDIS_URL")
    qdrant_url: str = Field(default="", alias="QDRANT_URL")
    qdrant_api_key: str = Field(default="", alias="QDRANT_API_KEY")
    celery_broker_url: str = Field(default="", alias="CELERY_BROKER_URL")
    celery_result_backend: str = Field(default="", alias="CELERY_RESULT_BACKEND")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    gemini_timeout_seconds: int = Field(default=30, alias="GEMINI_TIMEOUT_SECONDS")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    openai_timeout_seconds: int = Field(default=30, alias="OPENAI_TIMEOUT_SECONDS")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-haiku-4-5-20251001", alias="ANTHROPIC_MODEL")
    anthropic_timeout_seconds: int = Field(default=30, alias="ANTHROPIC_TIMEOUT_SECONDS")
    llm_provider_order: str = Field(default="openai", alias="LLM_PROVIDER_ORDER")
    local_embedding_endpoint: str = Field(default="", alias="LOCAL_EMBEDDING_ENDPOINT")
    embedding_provider: str = Field(default="openai", alias="EMBEDDING_PROVIDER")
    extraction_model: str = Field(default="", alias="EXTRACTION_MODEL")
    importance_shadow_enabled: bool = Field(default=False, alias="IMPORTANCE_SHADOW_ENABLED")
    importance_shadow_review_dir: str = Field(default="", alias="IMPORTANCE_SHADOW_REVIEW_DIR")
    embedding_model: str = Field(default="", alias="EMBEDDING_MODEL")
    embedding_model_id: str = Field(default="", alias="EMBEDDING_MODEL_ID")
    embedding_dimensions: str = Field(default="", alias="EMBEDDING_DIMENSIONS")
    qdrant_collection: str = Field(default="", alias="QDRANT_COLLECTION")
    extraction_payload_retention_days: int = Field(
        default=30,
        ge=1,
        le=3650,
        alias="EXTRACTION_PAYLOAD_RETENTION_DAYS",
    )
    oauth_credential_encryption_key: str = Field(
        default="",
        alias="OAUTH_CREDENTIAL_ENCRYPTION_KEY",
    )
    data_encryption_provider: str = Field(
        default="disabled",
        alias="DATA_ENCRYPTION_PROVIDER",
    )
    data_encryption_kms_key_id: str = Field(
        default="",
        alias="DATA_ENCRYPTION_KMS_KEY_ID",
    )
    data_encryption_write_mode: str = Field(
        default="disabled",
        alias="DATA_ENCRYPTION_WRITE_MODE",
    )
    vector_payload_include_content: bool = Field(
        default=True,
        alias="VECTOR_PAYLOAD_INCLUDE_CONTENT",
    )
    retrieval_redis_cache_write_enabled: bool = Field(
        default=True,
        alias="RETRIEVAL_REDIS_CACHE_WRITE_ENABLED",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
