from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.infra.fallbacks import on_postgres_open
from api.infra.postgres_benchmark import instrument_engine
from api.infra.postgres_benchmark import postgres_benchmark_enabled
from api.infra.postgres_benchmark import session_factory_owner
from api.settings import get_settings


SESSION_LOGGER = logging.getLogger("memoryos.postgres_session_benchmark")


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL") or get_settings().database_url
    if not database_url:
        raise RuntimeError("DATABASE_URL is required.")
    return database_url


def get_sync_database_url(database_url: str | None = None) -> str:
    url = database_url or get_database_url()
    if url.partition("://")[0] == "postgresql+asyncpg":
        return "postgresql+psycopg2://" + url.partition("://")[2]
    return url


class CircuitBreakerAsyncSession(AsyncSession):
    async def __aenter__(self):
        if postgres_benchmark_enabled():
            self.info["memoryos_benchmark_session_id"] = uuid.uuid4().hex[:12]
            self.info["memoryos_benchmark_session_owner"] = session_factory_owner()
            self.info["memoryos_benchmark_session_started_at"] = time.perf_counter()
            SESSION_LOGGER.warning(json.dumps({
                "event": "postgres_benchmark_session",
                "action": "enter",
                "session_id": self.info["memoryos_benchmark_session_id"],
                "owner": self.info["memoryos_benchmark_session_owner"],
                "task_id": id(asyncio.current_task()),
            }, sort_keys=True))
        return await super().__aenter__()

    async def __aexit__(self, exc_type, exc_value, traceback):
        session_id = self.info.get("memoryos_benchmark_session_id")
        started_at = self.info.get("memoryos_benchmark_session_started_at")
        if session_id:
            SESSION_LOGGER.warning(json.dumps({
                "event": "postgres_benchmark_session",
                "action": "exit_start",
                "session_id": session_id,
                "owner": self.info.get("memoryos_benchmark_session_owner", "unknown"),
                "task_id": id(asyncio.current_task()),
                "cancelled": bool(exc_type and issubclass(exc_type, asyncio.CancelledError)),
                "in_transaction": bool(self.in_transaction()),
                "age_ms": round((time.perf_counter() - started_at) * 1000, 3) if started_at else None,
            }, sort_keys=True))
        try:
            return await super().__aexit__(exc_type, exc_value, traceback)
        finally:
            if session_id:
                SESSION_LOGGER.warning(json.dumps({
                    "event": "postgres_benchmark_session",
                    "action": "exit_complete",
                    "session_id": session_id,
                    "owner": self.info.get("memoryos_benchmark_session_owner", "unknown"),
                    "task_id": id(asyncio.current_task()),
                    "in_transaction": bool(self.in_transaction()),
                }, sort_keys=True))

    async def execute(self, *args, **kwargs):  # type: ignore[override]
        breaker = CircuitBreakerRegistry.get_instance().postgres_cb
        return await breaker.call(super().execute, *args, fallback=on_postgres_open, **kwargs)

    async def commit(self) -> None:  # type: ignore[override]
        breaker = CircuitBreakerRegistry.get_instance().postgres_cb
        await breaker.call(super().commit, fallback=on_postgres_open)

    async def flush(self, *args, **kwargs) -> None:  # type: ignore[override]
        breaker = CircuitBreakerRegistry.get_instance().postgres_cb
        await breaker.call(super().flush, *args, fallback=on_postgres_open, **kwargs)

    async def get(self, *args, **kwargs):  # type: ignore[override]
        breaker = CircuitBreakerRegistry.get_instance().postgres_cb
        return await breaker.call(super().get, *args, fallback=on_postgres_open, **kwargs)

    async def refresh(self, *args, **kwargs) -> None:  # type: ignore[override]
        breaker = CircuitBreakerRegistry.get_instance().postgres_cb
        await breaker.call(super().refresh, *args, fallback=on_postgres_open, **kwargs)

    async def scalar(self, *args, **kwargs):  # type: ignore[override]
        breaker = CircuitBreakerRegistry.get_instance().postgres_cb
        return await breaker.call(super().scalar, *args, fallback=on_postgres_open, **kwargs)

    async def scalars(self, *args, **kwargs):  # type: ignore[override]
        breaker = CircuitBreakerRegistry.get_instance().postgres_cb
        return await breaker.call(super().scalars, *args, fallback=on_postgres_open, **kwargs)


class CircuitBreakerSyncSession(Session):
    def execute(self, *args, **kwargs):  # type: ignore[override]
        breaker = CircuitBreakerRegistry.get_instance().postgres_cb
        return breaker.call_sync(super().execute, *args, fallback=on_postgres_open, **kwargs)

    def commit(self) -> None:  # type: ignore[override]
        breaker = CircuitBreakerRegistry.get_instance().postgres_cb
        breaker.call_sync(super().commit, fallback=on_postgres_open)

    def flush(self, *args, **kwargs) -> None:  # type: ignore[override]
        breaker = CircuitBreakerRegistry.get_instance().postgres_cb
        breaker.call_sync(super().flush, *args, fallback=on_postgres_open, **kwargs)

    def get(self, *args, **kwargs):  # type: ignore[override]
        breaker = CircuitBreakerRegistry.get_instance().postgres_cb
        return breaker.call_sync(super().get, *args, fallback=on_postgres_open, **kwargs)

    def refresh(self, *args, **kwargs) -> None:  # type: ignore[override]
        breaker = CircuitBreakerRegistry.get_instance().postgres_cb
        breaker.call_sync(super().refresh, *args, fallback=on_postgres_open, **kwargs)


def build_async_engine(database_url: str | None = None):
    resolved_url = database_url or get_database_url()
    if resolved_url.startswith("sqlite"):
        return create_async_engine(resolved_url, pool_pre_ping=True)
    async_engine = create_async_engine(
        resolved_url,
        pool_size=int(os.getenv("DB_POOL_SIZE", "20")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "30")),
        pool_timeout=int(os.getenv("DB_POOL_TIMEOUT_SECONDS", "30")),
        pool_pre_ping=True,
    )
    instrument_engine(async_engine.sync_engine, kind="async", owner=session_factory_owner())
    return async_engine


def build_async_session_factory(database_url: str | None = None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        build_async_engine(database_url),
        expire_on_commit=False,
        class_=CircuitBreakerAsyncSession,
    )


engine = build_async_engine()
SessionLocal = build_async_session_factory()


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    app_state = getattr(request.app, "state", None)
    pool = getattr(app_state, "region_pool", None) if app_state is not None else None
    region_id = getattr(request.state, "region_id", None) or "IN1"
    if pool is not None:
        async with pool.get_db(region_id) as session:
            yield session
        return
    async with SessionLocal() as session:
        yield session


def build_sync_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    sync_engine = create_engine(get_sync_database_url(database_url), pool_pre_ping=True)
    instrument_engine(sync_engine, kind="sync", owner=session_factory_owner())
    return sessionmaker(bind=sync_engine, expire_on_commit=False, class_=CircuitBreakerSyncSession)
