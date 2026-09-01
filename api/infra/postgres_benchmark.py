from __future__ import annotations

import json
import inspect
import logging
import os
import sys
import time
from itertools import count
from typing import Any

from sqlalchemy import event


LOGGER = logging.getLogger("memoryos.postgres_benchmark")
_ENGINE_IDS = count(1)
_OWNER_COUNTS: dict[tuple[int, str, str], int] = {}


def _application_callsite() -> str:
    for frame in inspect.stack(context=0)[2:18]:
        module = str(frame.frame.f_globals.get("__name__", "unknown"))
        if (
            module.startswith(("sqlalchemy.", "asyncio."))
            or module == __name__
            or module in {"api.db.database", "api.infra.circuit_breaker"}
        ):
            continue
        if module.startswith("api."):
            return f"{module}.{frame.function}:{frame.lineno}"
    return "unknown"


def _statement_shape(statement: Any) -> str:
    words = " ".join(str(statement or "").split()).split()
    return " ".join(words[:12])[:240]


def _pool_snapshot(pool: Any) -> dict[str, int | None]:
    def _value(name: str) -> int | None:
        member = getattr(pool, name, None)
        try:
            return int(member()) if callable(member) else None
        except Exception:
            return None

    return {
        "pool_size": _value("size"),
        "checked_out": _value("checkedout"),
        "checked_in": _value("checkedin"),
        "overflow": _value("overflow"),
        "max_overflow": getattr(pool, "_max_overflow", None),
    }


def _driver_state(dbapi_connection: Any) -> dict[str, Any]:
    driver = getattr(dbapi_connection, "driver_connection", None)
    if driver is None:
        return {"backend_pid": None, "driver_in_transaction": None, "driver_closed": None}
    try:
        backend_pid = int(driver.get_server_pid())
    except Exception:
        backend_pid = None
    try:
        in_transaction = bool(driver.is_in_transaction())
    except Exception:
        in_transaction = None
    try:
        closed = bool(driver.is_closed())
    except Exception:
        closed = None
    return {
        "backend_pid": backend_pid,
        "driver_in_transaction": in_transaction,
        "driver_closed": closed,
    }


def postgres_benchmark_enabled() -> bool:
    return (
        os.getenv("APP_ENV", "").strip().lower() == "benchmark"
        and os.getenv("MEMORYOS_SCALE_DEDICATED") == "1"
        and os.getenv("MEMORYOS_BENCHMARK_PROVIDER") == "deterministic"
    )


def session_factory_owner() -> str:
    frame = sys._getframe(1)
    for _ in range(12):
        module = str(frame.f_globals.get("__name__", "unknown"))
        if module not in {__name__, "api.db.database"}:
            return f"{module}.{frame.f_code.co_name}:{frame.f_lineno}"
        if frame.f_back is None:
            break
        frame = frame.f_back
    return "unknown"


def instrument_engine(engine: Any, *, kind: str, owner: str) -> Any:
    if not postgres_benchmark_enabled():
        return engine

    engine_id = next(_ENGINE_IDS)
    pool = engine.pool
    process_role = os.getenv("MEMORYOS_PROCESS_ROLE", "unknown").strip() or "unknown"
    application_name = f"mosb:{process_role}:{os.getpid()}:{kind[0]}:{engine_id}"
    owner_key = (os.getpid(), kind, owner)
    owner_count = _OWNER_COUNTS.get(owner_key, 0) + 1
    _OWNER_COUNTS[owner_key] = owner_count
    pool_event_count = count(1)
    sql_event_count = count(1)
    transaction_event_count = count(1)
    if owner_count <= 5 or owner_count % 25 == 0:
        LOGGER.warning(json.dumps({
            "event": "postgres_benchmark_engine_created",
            "engine_id": engine_id,
            "kind": kind,
            "owner": owner,
            "owner_engine_count": owner_count,
            "application_name": application_name,
            "pid": os.getpid(),
            "process_role": process_role,
            "pool_class": type(pool).__name__,
            "pool_size": getattr(pool, "size", lambda: None)(),
            "max_overflow": getattr(pool, "_max_overflow", None),
        }, sort_keys=True))

    @event.listens_for(pool, "connect")
    def _connection_opened(dbapi_connection, _connection_record) -> None:
        label_outcome = "ok"
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute(f"SET application_name TO '{application_name}'")
            cursor.close()
            dbapi_connection.commit()
        except Exception as exc:
            label_outcome = type(exc).__name__
        LOGGER.warning(json.dumps({
            "event": "postgres_benchmark_connection_opened",
            "engine_id": engine_id,
            "kind": kind,
            "owner": owner,
            "application_name": application_name,
            "label_outcome": label_outcome,
            "pid": os.getpid(),
            "process_role": process_role,
            **_driver_state(dbapi_connection),
        }, sort_keys=True))

    @event.listens_for(pool, "close")
    def _connection_closed(_dbapi_connection, _connection_record) -> None:
        LOGGER.warning(json.dumps({
            "event": "postgres_benchmark_connection_closed",
            "engine_id": engine_id,
            "kind": kind,
            "owner": owner,
            "pid": os.getpid(),
        }, sort_keys=True))

    @event.listens_for(pool, "checkout")
    def _connection_checked_out(_dbapi_connection, _connection_record, _connection_proxy) -> None:
        snapshot = _pool_snapshot(pool)
        capacity = (snapshot["pool_size"] or 0) + max(0, snapshot["max_overflow"] or 0)
        near_capacity = bool(capacity and (snapshot["checked_out"] or 0) >= capacity * 0.8)
        if next(pool_event_count) % 20 == 0 or near_capacity:
            LOGGER.warning(json.dumps({
                "event": "postgres_benchmark_pool",
                "action": "checkout",
                "engine_id": engine_id,
                "kind": kind,
                "owner": owner,
                "pid": os.getpid(),
                "process_role": process_role,
                **_driver_state(_dbapi_connection),
                **snapshot,
            }, sort_keys=True))

    @event.listens_for(pool, "checkin")
    def _connection_checked_in(_dbapi_connection, _connection_record) -> None:
        driver_state = _driver_state(_dbapi_connection)
        if next(pool_event_count) % 20 == 0 or driver_state["driver_in_transaction"]:
            LOGGER.warning(json.dumps({
                "event": "postgres_benchmark_pool",
                "action": "checkin",
                "engine_id": engine_id,
                "kind": kind,
                "owner": owner,
                "pid": os.getpid(),
                "process_role": process_role,
                **driver_state,
                **_pool_snapshot(pool),
            }, sort_keys=True))

    @event.listens_for(pool, "reset")
    def _connection_reset(dbapi_connection, _connection_record, reset_state) -> None:
        driver_state = _driver_state(dbapi_connection)
        if driver_state["driver_in_transaction"] or next(pool_event_count) % 20 == 0:
            LOGGER.warning(json.dumps({
                "event": "postgres_benchmark_pool",
                "action": "reset",
                "engine_id": engine_id,
                "kind": kind,
                "owner": owner,
                "pid": os.getpid(),
                "process_role": process_role,
                "terminate_only": bool(getattr(reset_state, "terminate_only", False)),
                "transaction_was_reset": bool(getattr(reset_state, "transaction_was_reset", False)),
                **driver_state,
                **_pool_snapshot(pool),
            }, sort_keys=True))

    @event.listens_for(pool, "invalidate")
    def _connection_invalidated(dbapi_connection, _connection_record, exception) -> None:
        LOGGER.warning(json.dumps({
            "event": "postgres_benchmark_pool",
            "action": "invalidate",
            "engine_id": engine_id,
            "kind": kind,
            "owner": owner,
            "pid": os.getpid(),
            "process_role": process_role,
            "reason": type(exception).__name__ if exception else None,
            **_driver_state(dbapi_connection),
            **_pool_snapshot(pool),
        }, sort_keys=True))

    @event.listens_for(engine, "before_cursor_execute")
    def _before_cursor_execute(_conn, _cursor, statement, _parameters, context, _executemany) -> None:
        context._memoryos_benchmark_started_at = time.perf_counter()
        context._memoryos_benchmark_operation = str(statement).lstrip().split(None, 1)[0].upper() if statement else "UNKNOWN"
        context._memoryos_benchmark_callsite = _application_callsite()
        context._memoryos_benchmark_statement_shape = _statement_shape(statement)
        _conn.info.setdefault("memoryos_benchmark_first_sql_callsite", context._memoryos_benchmark_callsite)
        _conn.info["memoryos_benchmark_last_sql_callsite"] = context._memoryos_benchmark_callsite
        _conn.info["memoryos_benchmark_last_statement_shape"] = context._memoryos_benchmark_statement_shape

    @event.listens_for(engine, "after_cursor_execute")
    def _after_cursor_execute(_conn, _cursor, _statement, _parameters, context, _executemany) -> None:
        started_at = getattr(context, "_memoryos_benchmark_started_at", None)
        if started_at is None:
            return
        latency_ms = (time.perf_counter() - started_at) * 1000
        if latency_ms >= 50 or next(sql_event_count) % 50 == 0:
            LOGGER.warning(json.dumps({
                "event": "postgres_benchmark_sql",
                "engine_id": engine_id,
                "kind": kind,
                "owner": owner,
                "pid": os.getpid(),
                "operation": getattr(context, "_memoryos_benchmark_operation", "UNKNOWN"),
                "callsite": getattr(context, "_memoryos_benchmark_callsite", "unknown"),
                "statement_shape": getattr(context, "_memoryos_benchmark_statement_shape", "unknown"),
                "latency_ms": round(latency_ms, 3),
                "outcome": "ok",
            }, sort_keys=True))

    @event.listens_for(engine, "handle_error")
    def _handle_error(exception_context) -> None:
        context = getattr(exception_context, "execution_context", None)
        started_at = getattr(context, "_memoryos_benchmark_started_at", None)
        LOGGER.warning(json.dumps({
            "event": "postgres_benchmark_sql",
            "engine_id": engine_id,
            "kind": kind,
            "owner": owner,
            "pid": os.getpid(),
            "operation": getattr(context, "_memoryos_benchmark_operation", "CONNECT"),
            "callsite": getattr(context, "_memoryos_benchmark_callsite", "unknown"),
            "statement_shape": getattr(context, "_memoryos_benchmark_statement_shape", "unknown"),
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 3) if started_at else None,
            "outcome": "error",
            "reason": type(exception_context.original_exception).__name__,
        }, sort_keys=True))

    @event.listens_for(engine, "begin")
    def _transaction_began(connection) -> None:
        connection.info["memoryos_benchmark_transaction_started_at"] = time.perf_counter()
        connection.info["memoryos_benchmark_transaction_callsite"] = _application_callsite()
        connection.info.pop("memoryos_benchmark_first_sql_callsite", None)
        connection.info.pop("memoryos_benchmark_last_sql_callsite", None)
        connection.info.pop("memoryos_benchmark_last_statement_shape", None)

    def _transaction_finished(connection, outcome: str) -> None:
        started_at = connection.info.pop("memoryos_benchmark_transaction_started_at", None)
        if started_at is None:
            return
        latency_ms = (time.perf_counter() - started_at) * 1000
        if latency_ms >= 50 or next(transaction_event_count) % 20 == 0:
            LOGGER.warning(json.dumps({
                "event": "postgres_benchmark_transaction",
                "engine_id": engine_id,
                "kind": kind,
                "owner": owner,
                "pid": os.getpid(),
                "latency_ms": round(latency_ms, 3),
                "outcome": outcome,
                "transaction_callsite": connection.info.pop("memoryos_benchmark_transaction_callsite", "unknown"),
                "first_sql_callsite": connection.info.pop("memoryos_benchmark_first_sql_callsite", "unknown"),
                "last_sql_callsite": connection.info.pop("memoryos_benchmark_last_sql_callsite", "unknown"),
                "last_statement_shape": connection.info.pop("memoryos_benchmark_last_statement_shape", "unknown"),
            }, sort_keys=True))
        else:
            connection.info.pop("memoryos_benchmark_transaction_callsite", None)
            connection.info.pop("memoryos_benchmark_first_sql_callsite", None)
            connection.info.pop("memoryos_benchmark_last_sql_callsite", None)
            connection.info.pop("memoryos_benchmark_last_statement_shape", None)

    @event.listens_for(engine, "commit")
    def _transaction_committed(connection) -> None:
        _transaction_finished(connection, "commit")

    @event.listens_for(engine, "rollback")
    def _transaction_rolled_back(connection) -> None:
        _transaction_finished(connection, "rollback")

    return engine


__all__ = ["instrument_engine", "postgres_benchmark_enabled", "session_factory_owner"]
