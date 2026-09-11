from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.db.database import probe_async_session_factory


@pytest.mark.asyncio
async def test_probe_async_session_factory_uses_engine_without_request_session() -> None:
    connection = SimpleNamespace(execute=AsyncMock())

    class _ConnectionContext:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *_args):
            return None

    session_factory = SimpleNamespace(kw={"bind": SimpleNamespace(connect=lambda: _ConnectionContext())})

    await probe_async_session_factory(session_factory)

    connection.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_async_session_factory_rejects_missing_engine() -> None:
    with pytest.raises(RuntimeError, match="no database engine"):
        await probe_async_session_factory(SimpleNamespace(kw={}))
