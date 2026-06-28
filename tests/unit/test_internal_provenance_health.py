from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.routers.internal import PROVENANCE_HEALTH_CACHE_SECONDS
from api.routers.internal import _get_provenance_health
from api.schemas.internal_schemas import ProvenanceHealthResponse


class _Result:
    def __init__(self, row: dict[str, int]) -> None:
        self._row = row

    def mappings(self) -> _Result:
        return self

    def one(self) -> dict[str, int]:
        return self._row


class _Session:
    def __init__(self, row: dict[str, int]) -> None:
        self.row = row
        self.calls = 0
        self.statement = ""

    async def execute(self, statement) -> _Result:
        self.calls += 1
        self.statement = str(statement)
        return _Result(self.row)


class _Redis:
    def __init__(self, cached: str | None = None, *, fail: bool = False) -> None:
        self.cached = cached
        self.fail = fail
        self.set_calls: list[tuple[str, str, int]] = []

    async def get(self, _key: str) -> str | None:
        if self.fail:
            raise ConnectionError("redis unavailable")
        return self.cached

    async def set(self, key: str, value: str, *, ex: int) -> None:
        if self.fail:
            raise ConnectionError("redis unavailable")
        self.set_calls.append((key, value, ex))


def _healthy_row() -> dict[str, int]:
    return {
        "tenant_memories_total": 80,
        "tenant_memories_with_provenance": 80,
        "passport_memories_total": 20,
        "passport_memories_with_provenance": 20,
        "tenant_claims_disputed": 0,
        "passport_claims_disputed": 0,
        "revoked_grant_memories": 0,
        "missing_service_writers": 0,
        "tenant_legacy_unknown_memories": 0,
        "missing_passport_sources": 0,
        "failed_backfills_30d": 0,
    }


def _healthy_response() -> ProvenanceHealthResponse:
    return ProvenanceHealthResponse(
        memories_total=100,
        memories_with_provenance=100,
        coverage_pct=100.0,
        tenant_memories_total=80,
        tenant_memories_with_provenance=80,
        tenant_coverage_pct=100.0,
        passport_memories_total=20,
        passport_memories_with_provenance=20,
        passport_coverage_pct=100.0,
        tenant_claims_disputed=0,
        passport_claims_disputed=0,
        revoked_grant_memories=0,
        missing_service_writers=0,
        tenant_legacy_unknown_memories=0,
        missing_passport_sources=0,
        failed_backfills_30d=0,
        status="HEALTHY",
        generated_at="2026-06-22T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_provenance_health_uses_cached_snapshot_without_database_query() -> None:
    expected = _healthy_response()
    session = _Session(_healthy_row())
    redis = _Redis(expected.model_dump_json())

    result = await _get_provenance_health(session, SimpleNamespace(client=redis))

    assert result == expected
    assert session.calls == 0


@pytest.mark.asyncio
async def test_provenance_health_splits_tenant_and_passport_coverage() -> None:
    row = _healthy_row()
    row["tenant_memories_with_provenance"] = 70
    session = _Session(row)

    result = await _get_provenance_health(
        session,
        SimpleNamespace(client=_Redis(fail=True)),
    )

    assert session.calls == 1
    assert result.coverage_pct == 90.0
    assert result.tenant_coverage_pct == 87.5
    assert result.passport_coverage_pct == 100.0
    assert result.status == "CRITICAL"
    assert "backfill_universal_provenance%" in session.statement
    assert "backfill_tenant_provenance%" in session.statement
    assert "e.writer_id IS NULL" in session.statement


@pytest.mark.asyncio
async def test_provenance_health_caches_database_snapshot_for_sixty_seconds() -> None:
    session = _Session(_healthy_row())
    redis = _Redis()

    result = await _get_provenance_health(session, SimpleNamespace(client=redis))

    assert result.status == "HEALTHY"
    assert len(redis.set_calls) == 1
    assert redis.set_calls[0][2] == PROVENANCE_HEALTH_CACHE_SECONDS