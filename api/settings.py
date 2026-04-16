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
    database_url: str = Field(default="", alias="DATABASE_URL")
    redis_url: str = Field(default="", alias="REDIS_URL")
    qdrant_url: str = Field(default="", alias="QDRANT_URL")
    qdrant_api_key: str = Field(default="", alias="QDRANT_API_KEY")
    celery_broker_url: str = Field(default="", alias="CELERY_BROKER_URL")
    celery_result_backend: str = Field(default="", alias="CELERY_RESULT_BACKEND")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    local_embedding_endpoint: str = Field(default="", alias="LOCAL_EMBEDDING_ENDPOINT")
    extraction_model: str = Field(default="", alias="EXTRACTION_MODEL")
    embedding_model: str = Field(default="", alias="EMBEDDING_MODEL")
    embedding_dimensions: str = Field(default="", alias="EMBEDDING_DIMENSIONS")
    qdrant_collection: str = Field(default="", alias="QDRANT_COLLECTION")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
