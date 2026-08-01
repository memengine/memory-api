from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Awaitable
from typing import Any
from typing import Callable
from urllib.parse import urlsplit


class CircuitOpenError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"Circuit '{name}' is open.")
        self.name = name


@dataclass(slots=True)
class CircuitState:
    state: str = "CLOSED"
    failure_count: int = 0
    window_started_at: float = 0.0
    opened_at: float = 0.0


class CircuitBreaker:
    def __init__(
        self,
        *,
        name: str,
        failure_threshold: int,
        window_seconds: int,
        recovery_timeout_seconds: int,
        state_client: Any | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.state_client = state_client
        self.state_key = f"cb:{name}:state"
        self._lock = threading.RLock()
        self._local_state = CircuitState()
        self._state_store_disabled_until = 0.0
        self._next_refresh_at = 0.0
        self._refresh_interval_seconds = 0.1
        self._execution_timeout_seconds = 0.2 if name == "redis" else None
        self._redis_probe_executor = (
            concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="memoryos-redis-probe")
            if name == "redis"
            else None
        )
        self._redis_endpoint = self._parse_redis_endpoint() if name == "redis" else None
        self._last_probe_ok = True
        self._last_probe_checked_at = 0.0
        self._probe_cache_seconds = 0.25

    async def call(
        self,
        fn: Callable[..., Any],
        *args: Any,
        fallback: Callable[[], Any | Awaitable[Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._before_call()
        except CircuitOpenError:
            return await self._run_async_fallback(fallback)
        if self.name == "redis":
            preflight_ok = await self._redis_connectivity_preflight()
            if not preflight_ok:
                try:
                    self.force_open()
                except Exception:
                    pass
                return await self._run_async_fallback(fallback)
        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                if self._execution_timeout_seconds is not None:
                    result = await self._await_with_fast_timeout(
                        result,
                        timeout=self._execution_timeout_seconds,
                    )
                else:
                    result = await result
        except Exception:
            self._record_failure()
            raise
        self._record_success()
        return result

    def call_sync(
        self,
        fn: Callable[..., Any],
        *args: Any,
        fallback: Callable[[], Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._before_call()
        except CircuitOpenError:
            return self._run_sync_fallback(fallback)
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise
        self._record_success()
        return result

    def current_state(self) -> str:
        state = self._coerce_recovery_state(self._load_state())
        if self.name == "redis" and self.is_state_store_disabled() and state.state == "CLOSED":
            return "OPEN"
        return state.state

    def snapshot(self) -> dict[str, float | int | str]:
        state = self._coerce_recovery_state(self._load_state())
        state_name = state.state
        if self.name == "redis" and self.is_state_store_disabled() and state_name == "CLOSED":
            state_name = "OPEN"
        return {
            "state": state_name,
            "failure_count": state.failure_count,
            "window_started_at": state.window_started_at,
            "opened_at": state.opened_at,
        }

    def force_open(self) -> None:
        now = time.time()
        self._save_state(
            CircuitState(
                state="OPEN",
                failure_count=self.failure_threshold,
                window_started_at=now,
                opened_at=now,
            )
        )

    def local_state(self) -> str:
        with self._lock:
            if self.name == "redis" and time.time() < self._state_store_disabled_until:
                return "OPEN"
            return self._local_state.state

    def is_state_store_disabled(self) -> bool:
        return time.time() < self._state_store_disabled_until

    def _before_call(self) -> None:
        state = self._coerce_recovery_state(self._load_state())
        if self.name == "redis" and self.is_state_store_disabled():
            raise CircuitOpenError(self.name)
        if state.state == "OPEN":
            raise CircuitOpenError(self.name)

    def _coerce_recovery_state(self, state: CircuitState) -> CircuitState:
        if state.state != "OPEN":
            return state

        now = time.time()
        if now - state.opened_at < self.recovery_timeout_seconds:
            return state

        half_open_state = CircuitState(
            state="HALF_OPEN",
            failure_count=state.failure_count,
            window_started_at=state.window_started_at or now,
            opened_at=state.opened_at,
        )
        self._save_state(half_open_state)
        return half_open_state

    def _record_success(self) -> None:
        with self._lock:
            if self._local_state.state == "CLOSED" and self._local_state.failure_count == 0:
                return
        self._save_state(CircuitState(state="CLOSED", failure_count=0, window_started_at=0.0, opened_at=0.0))

    def _record_failure(self) -> None:
        now = time.time()
        state = self._load_state()

        if state.state == "HALF_OPEN":
            self._save_state(
                CircuitState(
                    state="OPEN",
                    failure_count=self.failure_threshold,
                    window_started_at=now,
                    opened_at=now,
                )
            )
            return

        if state.window_started_at == 0.0 or now - state.window_started_at > self.window_seconds:
            state.failure_count = 0
            state.window_started_at = now

        state.failure_count += 1

        if state.failure_count >= self.failure_threshold:
            state.state = "OPEN"
            state.opened_at = now
        else:
            state.state = "CLOSED"

        self._save_state(state)

    def _load_state(self, *, force_refresh: bool = False) -> CircuitState:
        with self._lock:
            now = time.time()
            if self.name == "redis":
                return self._local_state
            if self.state_client is None or now < self._state_store_disabled_until:
                return self._local_state
            if not force_refresh and now < self._next_refresh_at:
                return self._local_state
            try:
                raw = self.state_client.get(self.state_key)
                if raw:
                    payload = json.loads(raw)
                    self._local_state = CircuitState(
                        state=str(payload.get("state", "CLOSED")),
                        failure_count=int(payload.get("failure_count", 0) or 0),
                        window_started_at=float(payload.get("window_started_at", 0.0) or 0.0),
                        opened_at=float(payload.get("opened_at", 0.0) or 0.0),
                    )
                self._next_refresh_at = now + self._refresh_interval_seconds
            except Exception:
                self._state_store_disabled_until = now + 30.0
                if self.name == "redis":
                    self._local_state = CircuitState(
                        state="OPEN",
                        failure_count=self.failure_threshold,
                        window_started_at=now,
                        opened_at=now,
                    )
                return self._local_state
            return self._local_state

    def _save_state(self, state: CircuitState) -> None:
        with self._lock:
            self._local_state = state
            now = time.time()
            self._next_refresh_at = now + self._refresh_interval_seconds
            if self.name == "redis":
                return
            if self.state_client is None or now < self._state_store_disabled_until:
                return
            try:
                self.state_client.set(
                    self.state_key,
                    json.dumps(
                        {
                            "state": state.state,
                            "failure_count": state.failure_count,
                            "window_started_at": state.window_started_at,
                            "opened_at": state.opened_at,
                        }
                    )
                )
            except Exception:
                self._state_store_disabled_until = now + 30.0
                return

    async def _run_async_fallback(
        self,
        fallback: Callable[[], Any | Awaitable[Any]] | None,
    ) -> Any:
        if fallback is None:
            raise CircuitOpenError(self.name)
        result = fallback()
        if inspect.isawaitable(result):
            return await result
        return result

    def _run_sync_fallback(self, fallback: Callable[[], Any] | None) -> Any:
        if fallback is None:
            raise CircuitOpenError(self.name)
        return fallback()

    async def _redis_connectivity_preflight(self) -> bool:
        endpoint = self._redis_endpoint
        if endpoint is None:
            return True

        with self._lock:
            now = time.time()
            if now - self._last_probe_checked_at <= self._probe_cache_seconds:
                return self._last_probe_ok

        loop = asyncio.get_running_loop()
        try:
            is_reachable = await asyncio.wait_for(
                loop.run_in_executor(
                    self._redis_probe_executor,
                    self._probe_redis_endpoint_sync,
                    endpoint[0],
                    endpoint[1],
                ),
                timeout=0.1,
            )
        except Exception:
            is_reachable = False

        with self._lock:
            self._last_probe_checked_at = time.time()
            self._last_probe_ok = bool(is_reachable)
        return bool(is_reachable)

    @staticmethod
    def _probe_redis_endpoint_sync(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.05):
                return True
        except OSError:
            return False

    @staticmethod
    def _parse_redis_endpoint() -> tuple[str, int] | None:
        import os
        from api.settings import get_settings

        redis_url = os.getenv("REDIS_URL") or get_settings().redis_url
        if not redis_url:
            return None
        parsed = urlsplit(redis_url)
        host = parsed.hostname
        port = parsed.port or 6379
        if not host:
            return None
        return (host, int(port))

    async def _await_with_fast_timeout(self, awaitable: Awaitable[Any], *, timeout: float) -> Any:
        task = asyncio.ensure_future(awaitable)
        done, pending = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            return await task

        task.cancel()
        task.add_done_callback(self._consume_background_exception)
        raise asyncio.TimeoutError

    @staticmethod
    def _consume_background_exception(task: asyncio.Future[Any]) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            return None
