from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis
from qdrant_client import QdrantClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.db.cache import CacheService
from api.infra.redis_benchmark import benchmark_async_redis_from_url
from api.db.database import build_async_session_factory
from api.db.database import build_sync_session_factory
from api.db.database import get_database_url
from api.db.models import Region


LOGGER = logging.getLogger("memoryos.region_pool")
DEFAULT_REGION_ID = "IN1"
LOCAL_SECRET_ENV_MAP = {
    "postgres": ["REGION_{region_id}_DATABASE_URL", "DATABASE_URL"],
    "qdrant": ["REGION_{region_id}_QDRANT_URL", "QDRANT_URL"],
    "redis": ["REGION_{region_id}_REDIS_URL", "REDIS_URL"],
}


@dataclass(slots=True)
class RegionResources:
    id: str
    aws_region: str
    session_factory: async_sessionmaker[AsyncSession]
    qdrant_client: QdrantClient
    redis_client: redis.Redis
    cache_service: CacheService


class RegionConnectionPool:
    def __init__(
        self,
        *,
        bootstrap_database_url: str | None = None,
        app_env: str | None = None,
        secrets_client: Any | None = None,
        region_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.bootstrap_database_url = bootstrap_database_url or get_database_url()
        self.app_env = (app_env or os.getenv("APP_ENV", "development")).strip().lower()
        self.secrets_client = secrets_client
        self._region_rows = region_rows
        self._resources: dict[str, RegionResources] = {}
        self._secret_cache: dict[tuple[str, str], Any] = {}

    def initialize(self) -> None:
        region_rows = self._region_rows or self._load_active_regions()
        if not region_rows:
            raise RuntimeError("No active regions configured.")
        for row in region_rows:
            self._initialize_region(row)

    def get_db(self, region_id: str) -> AsyncSession:
        resources = self._require_region(region_id)
        LOGGER.info(
            "region_db_session_selected region_id=%s aws_region=%s",
            resources.id,
            resources.aws_region,
        )
        return resources.session_factory()

    def get_qdrant(self, region_id: str) -> QdrantClient:
        resources = self._require_region(region_id)
        LOGGER.info(
            "region_qdrant_client_selected region_id=%s aws_region=%s",
            resources.id,
            resources.aws_region,
        )
        return resources.qdrant_client

    def get_redis(self, region_id: str) -> redis.Redis:
        resources = self._require_region(region_id)
        LOGGER.info(
            "region_redis_client_selected region_id=%s aws_region=%s",
            resources.id,
            resources.aws_region,
        )
        return resources.redis_client

    def get_cache_service(self, region_id: str) -> CacheService:
        resources = self._require_region(region_id)
        LOGGER.info(
            "region_cache_service_selected region_id=%s aws_region=%s",
            resources.id,
            resources.aws_region,
        )
        return resources.cache_service

    async def lookup_tenant_region(self, tenant_id: str) -> str:
        async with self.get_db(DEFAULT_REGION_ID) as session:
            result = await session.execute(
                text("SELECT region_id FROM tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            region_id = result.scalar_one_or_none()
            return str(region_id or DEFAULT_REGION_ID)

    def _initialize_region(self, row: dict[str, Any]) -> None:
        region_id = str(row["id"])
        if region_id in self._resources:
            return

        aws_region = str(row["aws_region"])
        postgres_secret = str(row["postgres_url_secret"])
        qdrant_secret = str(row["qdrant_url_secret"])
        redis_secret = str(row["redis_url_secret"])

        postgres_value = self._resolve_secret(postgres_secret, aws_region=aws_region, kind="postgres", region_id=region_id)
        qdrant_value = self._resolve_secret(qdrant_secret, aws_region=aws_region, kind="qdrant", region_id=region_id)
        redis_value = self._resolve_secret(redis_secret, aws_region=aws_region, kind="redis", region_id=region_id)

        postgres_url = str(self._extract_connection_value(postgres_value, kind="postgres"))
        qdrant_url = str(self._extract_connection_value(qdrant_value, kind="qdrant"))
        redis_url = str(self._extract_connection_value(redis_value, kind="redis"))
        qdrant_api_key = self._extract_api_key(qdrant_value, region_id=region_id)

        redis_client = benchmark_async_redis_from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            client_role="cache",
        )
        cache_service = CacheService(client=redis_client, use_direct_breaker=False)

        self._resources[region_id] = RegionResources(
            id=region_id,
            aws_region=aws_region,
            session_factory=build_async_session_factory(postgres_url),
            qdrant_client=QdrantClient(url=qdrant_url, api_key=qdrant_api_key),
            redis_client=redis_client,
            cache_service=cache_service,
        )
        LOGGER.info(
            "region_resources_initialized region_id=%s aws_region=%s postgres_secret=%s qdrant_secret=%s redis_secret=%s",
            region_id,
            aws_region,
            postgres_secret,
            qdrant_secret,
            redis_secret,
        )

    def _load_active_regions(self) -> list[dict[str, Any]]:
        sync_session_factory = build_sync_session_factory(self.bootstrap_database_url)
        session = sync_session_factory()
        try:
            result = session.execute(
                text(
                    """
                    SELECT
                        id,
                        aws_region,
                        postgres_url_secret,
                        qdrant_url_secret,
                        redis_url_secret
                    FROM regions
                    WHERE is_active = TRUE
                    ORDER BY id
                    """
                )
            )
            return [dict(row._mapping) for row in result]
        finally:
            session.close()

    def _resolve_secret(self, secret_name: str, *, aws_region: str, kind: str, region_id: str) -> Any:
        cache_key = (aws_region, secret_name)
        if cache_key in self._secret_cache:
            return self._secret_cache[cache_key]

        secret_value = self._fetch_secret(secret_name, aws_region=aws_region)
        if secret_value is None:
            secret_value = self._development_fallback(kind=kind, region_id=region_id)
        if secret_value is None:
            raise RuntimeError(f"Unable to resolve {kind} secret for region {region_id}.")

        self._secret_cache[cache_key] = secret_value
        return secret_value

    def _fetch_secret(self, secret_name: str, *, aws_region: str) -> Any | None:
        client = self.secrets_client or self._build_secrets_client(aws_region)
        if client is None:
            return None
        try:
            response = client.get_secret_value(SecretId=secret_name)
        except Exception as exc:
            if self.app_env == "production":
                raise RuntimeError(f"Failed to load secret {secret_name}") from exc
            LOGGER.warning("Region secret lookup failed for %s: %s", secret_name, exc)
            return None

        raw_secret = response.get("SecretString")
        if raw_secret is None:
            return None
        try:
            return json.loads(raw_secret)
        except json.JSONDecodeError:
            return raw_secret

    @staticmethod
    def _build_secrets_client(aws_region: str):
        try:
            import boto3  # type: ignore
        except Exception:
            return None
        return boto3.client("secretsmanager", region_name=aws_region)

    def _development_fallback(self, *, kind: str, region_id: str) -> str | None:
        if self.app_env == "production":
            return None
        for env_name_template in LOCAL_SECRET_ENV_MAP.get(kind, []):
            env_name = env_name_template.format(region_id=region_id)
            env_value = os.getenv(env_name)
            if env_value:
                return env_value
        if kind == "postgres":
            return get_database_url()
        if kind == "qdrant":
            return os.getenv("QDRANT_URL")
        if kind == "redis":
            return os.getenv("REDIS_URL")
        return None

    @staticmethod
    def _extract_connection_value(secret_value: Any, *, kind: str) -> str:
        if isinstance(secret_value, str):
            return secret_value
        if isinstance(secret_value, dict):
            for key in (
                "url",
                f"{kind}_url",
                "connection_url",
                "endpoint",
                "value",
            ):
                value = secret_value.get(key)
                if value:
                    return str(value)
        raise RuntimeError(f"Malformed {kind} secret payload.")

    @staticmethod
    def _extract_api_key(secret_value: Any, *, region_id: str) -> str | None:
        if isinstance(secret_value, dict):
            api_key = secret_value.get("api_key")
            if api_key:
                return str(api_key)
        return os.getenv(f"REGION_{region_id}_QDRANT_API_KEY") or os.getenv("QDRANT_API_KEY")

    def _require_region(self, region_id: str) -> RegionResources:
        normalized_region = str(region_id or DEFAULT_REGION_ID)
        resources = self._resources.get(normalized_region)
        if resources is not None:
            return resources
        default_resources = self._resources.get(DEFAULT_REGION_ID)
        if default_resources is None:
            raise RuntimeError(f"Region {normalized_region} is not initialized.")
        return default_resources


__all__ = ["DEFAULT_REGION_ID", "RegionConnectionPool", "RegionResources"]
