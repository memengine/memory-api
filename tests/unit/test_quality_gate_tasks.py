from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from api.tasks import quality_gate_tasks


def test_quality_gate_session_factory_is_reused_per_worker_process(monkeypatch) -> None:
    first_engine = MagicMock()
    second_engine = MagicMock()
    engines = iter([first_engine, second_engine])
    create_calls: list[int] = []

    quality_gate_tasks.dispose_quality_gate_session_factory()
    monkeypatch.setattr(quality_gate_tasks.os, "getpid", lambda: 101)
    monkeypatch.setattr(
        quality_gate_tasks,
        "create_engine",
        lambda *_args, **_kwargs: create_calls.append(1) or next(engines),
    )
    monkeypatch.setattr(quality_gate_tasks, "instrument_engine", lambda engine, **_kwargs: engine)
    monkeypatch.setattr(
        quality_gate_tasks,
        "sessionmaker",
        lambda *, bind, expire_on_commit: SimpleNamespace(
            kw={"bind": bind}, expire_on_commit=expire_on_commit
        ),
    )

    first_factory = quality_gate_tasks.build_quality_gate_session_factory()
    assert quality_gate_tasks.build_quality_gate_session_factory() is first_factory
    assert len(create_calls) == 1

    monkeypatch.setattr(quality_gate_tasks.os, "getpid", lambda: 202)
    second_factory = quality_gate_tasks.build_quality_gate_session_factory()
    assert second_factory is not first_factory
    assert len(create_calls) == 2
    first_engine.dispose.assert_called_once_with()

    quality_gate_tasks.dispose_quality_gate_session_factory()
    second_engine.dispose.assert_called_once_with()


def test_quality_gate_session_factory_shutdown_is_idempotent(monkeypatch) -> None:
    engine = MagicMock()
    quality_gate_tasks._QUALITY_GATE_SESSION_FACTORY = SimpleNamespace(kw={"bind": engine})
    quality_gate_tasks._QUALITY_GATE_SESSION_FACTORY_PID = 101

    quality_gate_tasks.dispose_quality_gate_session_factory()
    quality_gate_tasks.dispose_quality_gate_session_factory()

    engine.dispose.assert_called_once_with()
    assert quality_gate_tasks._QUALITY_GATE_SESSION_FACTORY is None
    assert quality_gate_tasks._QUALITY_GATE_SESSION_FACTORY_PID is None
