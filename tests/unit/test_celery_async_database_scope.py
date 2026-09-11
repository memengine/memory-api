from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.db import database


@pytest.mark.asyncio
async def test_scoped_async_session_factory_disposes_engine_after_task_loop(monkeypatch) -> None:
    engine = SimpleNamespace(dispose=AsyncMock())
    factory = object()
    monkeypatch.setattr(database, "build_async_engine", lambda _url=None: engine)
    monkeypatch.setattr(database, "async_sessionmaker", lambda *_args, **_kwargs: factory)

    async with database.scoped_async_session_factory() as resolved_factory:
        assert resolved_factory is factory

    engine.dispose.assert_awaited_once()
