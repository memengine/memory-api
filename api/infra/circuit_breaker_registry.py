from __future__ import annotations

import os
from typing import Any

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from api.infra.circuit_breaker import CircuitBreaker
from api.infra.circuit_config import CIRCUIT_CONFIGS
from api.settings import get_settings


class CircuitBreakerRegistry:
    _instance: "CircuitBreakerRegistry" | None = None

    def __init__(self, *, state_client: Any | None = None, redis_url: str | None = None) -> None:
        resolved_redis_url = redis_url or os.getenv("REDIS_URL") or get_settings().redis_url
        self.state_client = state_client or (
            redis.Redis.from_url(
                resolved_redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=0.05,
                socket_timeout=0.05,
                retry=Retry(NoBackoff(), 0),
                retry_on_timeout=False,
            )
            if resolved_redis_url
            else None
        )
        self.redis_cb = self._build_breaker("redis")
        self.gemini_embed_cb = self._build_breaker("gemini_embed")
        self.gemini_extract_cb = self._build_breaker("gemini_extract")
        self.qdrant_cb = self._build_breaker("qdrant")
        self.postgres_cb = self._build_breaker("postgres")
        self._breakers = {
            "redis": self.redis_cb,
            "gemini_embed": self.gemini_embed_cb,
            "gemini_extract": self.gemini_extract_cb,
            "qdrant": self.qdrant_cb,
            "postgres": self.postgres_cb,
        }

    @classmethod
    def initialize(
        cls,
        *,
        state_client: Any | None = None,
        redis_url: str | None = None,
    ) -> "CircuitBreakerRegistry":
        if cls._instance is None:
            cls._instance = cls(state_client=state_client, redis_url=redis_url)
        return cls._instance

    @classmethod
    def reset(
        cls,
        *,
        state_client: Any | None = None,
        redis_url: str | None = None,
    ) -> "CircuitBreakerRegistry":
        cls._instance = cls(state_client=state_client, redis_url=redis_url)
        return cls._instance

    @classmethod
    def get_instance(cls) -> "CircuitBreakerRegistry":
        return cls.initialize()

    def _build_breaker(self, name: str) -> CircuitBreaker:
        config = CIRCUIT_CONFIGS[name]
        return CircuitBreaker(
            name=name,
            failure_threshold=config.failure_threshold,
            window_seconds=config.window_seconds,
            recovery_timeout_seconds=config.recovery_timeout_seconds,
            state_client=self.state_client,
        )

    def get_health(self) -> dict[str, str]:
        health: dict[str, str] = {}
        use_local_only = False
        for name, breaker in self._breakers.items():
            if use_local_only:
                health[name] = breaker.local_state()
                continue

            health[name] = breaker.current_state()
            use_local_only = breaker.is_state_store_disabled()
        return health

    def get_local_health(self) -> dict[str, str]:
        return {name: breaker.local_state() for name, breaker in self._breakers.items()}

    def overall_status(self) -> str:
        return self._overall_status_from_health(self.get_health())

    def overall_status_local(self) -> str:
        return self._overall_status_from_health(self.get_local_health())

    @staticmethod
    def _overall_status_from_health(health: dict[str, str]) -> str:
        open_breakers = {name for name, state in health.items() if state == "OPEN"}
        if "postgres" in open_breakers or len(open_breakers) >= 3:
            return "CRITICAL"
        if open_breakers:
            return "DEGRADED"
        return "HEALTHY"
